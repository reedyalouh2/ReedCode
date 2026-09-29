"""Recompute the GPU validation summary from the saved records."""

import hashlib
import json
from pathlib import Path
from statistics import mean
import tarfile

ROOT = Path(__file__).resolve().parent


def summarize():
    with tarfile.open(ROOT / 'raw-records.tar.gz', 'r:gz') as archive:
        def read(name):
            return archive.extractfile(name).read()

        def document(name):
            return json.loads(read(name))

        for name, digest in document('sha256.json').items():
            if hashlib.sha256(read(name)).hexdigest() != digest:
                raise ValueError(f'Checksum mismatch: {name}')

        pilot = document('pilot/report.json')
        replay = document('replay/report.json')
        routing = [json.loads(line) for line in read('server/frontend.log').decode().splitlines()
                   if line.startswith('{')]
        routing = [r for r in routing if r.get('message') == '[ROUTING_INPUT] request local hashes']
        by_request = {r['request_id']: (i, r) for i, r in enumerate(routing) if r.get('request_id')}

        def hashes(row):
            return json.loads(row['local_hashes'])

        def common_blocks(a, b):
            count = 0
            for x, y in zip(hashes(a), hashes(b)):
                if x != y:
                    break
                count += 1
            return count

        checks = []
        for case in ('text', 'tool'):
            turns = document(f'prefix-check/{case}-on.json')
            first_index, first = by_request[turns[0]['response']['id'].removeprefix('chatcmpl-')]
            next_index, continuation = by_request[turns[1]['response']['id'].removeprefix('chatcmpl-')]
            candidates = routing[first_index + 1:next_index]
            # Speculative requests have no HTTP request ID in this server log.
            if len(candidates) != 1 or candidates[0].get('request_id'):
                raise ValueError('Expected one isolated speculative request between turns')
            prepared = candidates[0]
            block_size = prepared['block_size']
            off = document(f'prefix-check/{case}-off.json')
            checks.append({
                'case': case,
                'session_id': turns[0]['session_id'],
                'first_request_id': first['request_id'],
                'continuation_request_id': continuation['request_id'],
                'prepared_tokens': prepared['isl_tokens'],
                'continuation_tokens': continuation['isl_tokens'],
                'block_size': block_size,
                'prepared_full_blocks': prepared['num_blocks'],
                'matching_prepared_blocks': common_blocks(prepared, continuation),
                'matching_original_blocks': common_blocks(first, continuation),
                'cached_continuation_tokens_off': off[1]['response']['usage']['prompt_tokens_details']['cached_tokens'],
                'cached_continuation_tokens_on': turns[1]['response']['usage']['prompt_tokens_details']['cached_tokens'],
                'first_request_hashes': hashes(first),
                'prepared_hashes': hashes(prepared),
                'continuation_hashes': hashes(continuation),
            })

        summaries = {}
        for name, report, fields in (
            ('pilot', pilot, ('input_tokens', 'model_latency_ms', 'model_calls', 'tool_calls')),
            ('replay', replay, ('mean_capped_completion_ms', 'post_tool_first_output_p90_ms',
                                'client_requests', 'session_final_requests')),
        ):
            summaries[name] = {}
            for condition in ('off', 'on'):
                rows = [r for r in report['rows'] if r['condition'] == condition]
                summaries[name][condition] = {
                    'records': len(rows),
                    'completed': sum(r['completed'] for r in rows),
                    'means': {field: mean(r[field] for r in rows) for field in fields},
                    'output_limit_hits': sum(r.get('output_limit_hits', r.get('output_limit_hit', 0)) for r in rows),
                }
            summaries[name]['paired_comparisons'] = report['comparisons']

        metrics = []
        manifest = document('replay/manifest.json')
        for record in manifest['epochs']:
            path = record['directory'] + '/epoch.json'
            if hashlib.sha256(read('replay/' + path)).hexdigest() != record['sha256']:
                raise ValueError('Replay epoch checksum mismatch')
            epoch = document('replay/' + path)
            m = epoch['metrics']
            metrics.append({
                'run_id': record['run_id'], 'condition': record['condition'],
                'valid_boundaries': m['valid_boundaries'],
                'restart_observable': m['restart_observable'],
                'idle_before': m['idle_before'], 'drained': m['drained'], 'errors': m['errors'],
                'raw_counter_deltas': m['counter_deltas'],
                'raw_phase_totals': m['phase_totals'],
                'raw_sampled_kv_max_fraction': m['sampled_kv_max_fraction'],
            })
        billing = document('billing-after.json')
        return {
            'date_utc': '2026-09-28', 'deployment': document('setup/deployment.json'),
            **summaries, 'prefix_checks': checks,
            'replay_metrics': metrics,
            'replay_metrics_status': 'Rejected by the collector: process_start_time_seconds is missing. Raw values are retained for diagnosis.',
            'replay_message_mismatches': sum(r['recorded_message_mismatches'] for r in replay['rows']),
            'replay_failed_final_notifications': sum(r['failed_final_notifications'] for r in replay['rows']),
            'billing': {'initial_balance_usd': 10, **billing, 'balance_difference_usd': 10 - billing['clientBalance']},
        }


if __name__ == '__main__':
    print(json.dumps(summarize(), indent=2))
