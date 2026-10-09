"""Offline paired analysis. No requests, model execution, or tool execution."""
import argparse
import base64
from collections import Counter, defaultdict
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import statistics

try:
    import jsonschema
except ImportError:
    jsonschema = None

THINK, END_THINK, TOOL, END_TOOL, EOS = 151667, 151668, 151657, 151658, 151645
ASSISTANT = [151644, 77091, 198]
DISABLED = ASSISTANT + [THINK, 271, END_THINK, 271]


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def valid_ids(value):
    return isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value)


def sse_events(chunks):
    buffer, events, errors, last_at = b'', [], [], None
    for chunk in chunks:
        at = chunk['at_ns']
        if not isinstance(at, int) or (last_at is not None and at < last_at):
            errors.append('invalid_or_decreasing_chunk_timestamp')
        last_at = at
        buffer += base64.b64decode(chunk['data_base64'], validate=True)
        while match := re.search(br'\r?\n\r?\n', buffer):
            raw, buffer = buffer[:match.start()], buffer[match.end():]
            try:
                lines = raw.decode('utf-8').splitlines()
                data = '\n'.join(line[5:].lstrip(' ') for line in lines if line.startswith('data:'))
                label = next((line[6:].strip() for line in lines if line.startswith('event:')), None)
                if not data or data == '[DONE]':
                    continue
                value = json.loads(data)
                events.append({'at_ns': at, 'event': label, 'data': value})
            except (UnicodeError, ValueError) as error:
                errors.append('invalid_sse_event:' + str(error))
    if buffer.strip():
        errors.append('truncated_sse_event')
    return events, errors


def parse_message(events, body):
    blocks, errors, final_usage = {}, [], {}
    stop_reason, terminal, first_text, starts = None, False, None, 0
    tools = {t.get('name'): t.get('input_schema') for t in body.get('tools', [])}
    for event in events:
        value, at = event['data'], event['at_ns']
        if not isinstance(value, dict):
            errors.append('non_object_sse_event'); continue
        kind = value.get('type')
        if terminal:
            errors.append('event_after_message_stop'); continue
        if kind == 'error': errors.append('server_error:' + json.dumps(value.get('error')))
        elif kind == 'message_start': starts += 1
        elif kind == 'content_block_start':
            index, block = value.get('index'), value.get('content_block', {})
            if index in blocks: errors.append('duplicate_block_index')
            blocks[index] = {**block, 'partial_json': '', 'closed': False, 'closed_at_ns': None}
            if block.get('type') == 'text' and block.get('text') and first_text is None: first_text = at
        elif kind == 'content_block_delta':
            block, delta = blocks.get(value.get('index')), value.get('delta', {})
            if block is None or block['closed']:
                errors.append('delta_without_open_block'); continue
            dtype = delta.get('type')
            if dtype == 'text_delta':
                block['text'] = block.get('text', '') + delta.get('text', '')
                if delta.get('text') and first_text is None: first_text = at
            elif dtype == 'thinking_delta': block['thinking'] = block.get('thinking', '') + delta.get('thinking', '')
            elif dtype == 'input_json_delta': block['partial_json'] += delta.get('partial_json', '')
        elif kind == 'content_block_stop':
            block = blocks.get(value.get('index'))
            if block is None or block['closed']: errors.append('stop_without_open_block')
            else: block.update(closed=True, closed_at_ns=at)
        elif kind == 'message_delta':
            final_usage.update(value.get('usage') or {})
            stop_reason = (value.get('delta') or {}).get('stop_reason') or stop_reason
        elif kind == 'message_stop':
            if terminal: errors.append('duplicate_message_stop')
            terminal = True
    if starts != 1: errors.append('message_start_count:' + str(starts))
    if terminal and stop_reason not in ('end_turn', 'max_tokens', 'tool_use', 'stop_sequence'):
        errors.append('missing_or_unknown_final_stop_reason')
    if terminal and any(not b['closed'] for b in blocks.values()): errors.append('unclosed_block_at_message_stop')
    calls = []
    for block in blocks.values():
        if block.get('type') != 'tool_use': continue
        call = {'id': block.get('id'), 'name': block.get('name'), 'closed': block['closed'],
                'closed_at_ns': block['closed_at_ns'], 'arguments': None, 'structurally_valid': False, 'schema_valid': None}
        try:
            args = json.loads(block['partial_json']) if block['partial_json'] else block.get('input', {})
            call['arguments'] = args
            call['structurally_valid'] = block['closed'] and block.get('name') in tools and isinstance(args, dict)
            if call['structurally_valid'] and tools[block['name']] is not None and jsonschema is not None:
                try:
                    jsonschema.validate(args, tools[block['name']]); call['schema_valid'] = True
                except jsonschema.ValidationError: call['schema_valid'] = False
                except jsonschema.SchemaError: call['schema_valid'] = None
        except ValueError: pass
        calls.append(call)
    final_text = ''.join(b.get('text', '') for b in blocks.values() if b.get('type') == 'text')
    thinking = ''.join(b.get('thinking', '') for b in blocks.values() if b.get('type') == 'thinking')
    safe_terminal = terminal and stop_reason == 'tool_use' and not errors
    times = [c['closed_at_ns'] for c in calls if safe_terminal and c['structurally_valid'] and c['schema_valid'] is True]
    return {'final_text': final_text, 'thinking_text_present': bool(thinking), 'tool_calls': calls,
            'first_final_text_ns': first_text, 'first_valid_complete_tool_ns': min(times) if times else None,
            'stop_reason': stop_reason, 'final_usage': final_usage, 'message_stop': terminal, 'errors': errors}


def reasoning_spans(prompt, output):
    result = {'reasoning_tokens': None, 'reasoning_delimiter_tokens': None, 'eos_tokens': None,
              'open_reasoning_at_end': None, 'prompt_state': None, 'errors': []}
    if not valid_ids(prompt) or not valid_ids(output):
        result['errors'].append('native_ids_unavailable'); return result
    if prompt[-len(DISABLED):] == DISABLED: result['prompt_state'] = 'disabled_empty_closed_span'
    elif prompt[-len(ASSISTANT):] == ASSISTANT: result['prompt_state'] = 'enabled_or_default_outside_reasoning'
    else:
        result['errors'].append('unknown_generation_boundary'); return result
    inside, tool, count, delimiters, eos = False, False, 0, 0, 0
    for value in output:
        if value == EOS: eos += 1; continue
        if value in (THINK, END_THINK):
            delimiters += 1
            if tool: result['errors'].append('reasoning_marker_inside_tool'); continue
            if value == THINK:
                if inside: result['errors'].append('nested_reasoning_opener')
                inside = True
            else:
                if not inside: result['errors'].append('orphan_reasoning_closer')
                inside = False
        elif value == TOOL:
            if inside: result['errors'].append('tool_inside_unclosed_reasoning')
            if tool: result['errors'].append('nested_tool_marker')
            tool = True
        elif value == END_TOOL: tool = False
        elif inside: count += 1
    result.update(reasoning_delimiter_tokens=delimiters, eos_tokens=eos, open_reasoning_at_end=inside)
    if not result['errors']: result['reasoning_tokens'] = count
    return result


def delta_ns(a, b):
    return (b-a)/1e9 if isinstance(a, int) and isinstance(b, int) and b >= a else None


def analyze_row(entry, fixture, root, run, outcome, expected_prompts=None):
    folder = run / entry['request_id']
    row = {**entry, 'status': 'not_started', 'not_started_reason': (outcome or {}).get('not_started', 'no_capture'),
           'latency_s': None, 'observed_elapsed_s': None, 'first_output_s': None, 'output_tokens': None,
           'reasoning_tokens': None, 'answer_exact_match': None, 'answer_accepted': None,
           'cost_eligible': False, 'errors': []}
    transport = load(folder/'transport.json')
    if transport is None: return row
    row.pop('not_started_reason')
    body_path = folder/'request.body'
    body_hash = hashlib.sha256(body_path.read_bytes()).hexdigest()
    body = json.loads(body_path.read_bytes())
    row['fixture_hash_matches'] = body_hash == entry['body_sha256']
    records = load(folder/'engine-records.json', [])
    expected_salt = 'dynamo-cache-salt:' + entry['headers']['x-tenant-id']
    enters = [r for r in records if r.get('kind') == 'engine_enter' and r.get('cache_salt') == expected_salt]
    ends = [r for r in records if r.get('kind') == 'engine_result' and r.get('cache_salt') == expected_salt]
    joined = len(enters) == len(ends) == 1 and enters[0].get('request_id') == ends[0].get('request_id')
    engine = ends[0] if joined else {}
    chunks_path = folder/'response-chunks.jsonl'
    chunks = [json.loads(line) for line in chunks_path.read_text().splitlines()] if chunks_path.exists() else []
    events, parse_errors = sse_events(chunks)
    message = parse_message(events, body)
    ids = engine.get('output_ids')
    spans = reasoning_spans(engine.get('prompt_ids'), ids)
    usage = message['final_usage']
    complete_ids = joined and engine.get('output_ids_complete') is True and valid_ids(ids)
    count = len(ids) if valid_ids(ids) else None
    native_count = (engine.get('engine_stats') or {}).get('num_generation_tokens')
    expected = (expected_prompts or {}).get(entry['fixture_id'], {}).get(entry['arm'])
    actual_prompt = engine.get('prompt_ids')
    if isinstance(expected, list): prompt_matches = actual_prompt == expected
    elif isinstance(expected, str) and valid_ids(actual_prompt):
        prompt_matches = hashlib.sha256(json.dumps(actual_prompt,separators=(',',':')).encode()).hexdigest() == expected
    else: prompt_matches = None
    sampling = engine.get('sampling') or {}
    controls_match = sampling.get('temperature') == body.get('temperature') and sampling.get('max_tokens') == body.get('max_tokens') and sampling.get('n') == 1 and engine.get('output_kind') == 'DELTA'
    usage_matches = complete_ids and count == usage.get('output_tokens') == native_count
    normal_eof = transport.get('transport_status') == 'eof' and transport.get('http_status') == 200
    stream_ok = message['message_stop'] and not parse_errors and not message['errors']
    stop = message['stop_reason']
    if transport.get('transport_status') == 'timeout': status = 'timeout'
    elif transport.get('transport_status') == 'http_error': status = 'http_error'
    elif not normal_eof or not stream_ok: status = 'stream_error'
    elif stop == 'max_tokens' or engine.get('engine_finish_reason') == 'length': status = 'limit_hit'
    elif not message['final_text'] and not message['tool_calls']: status = 'reasoning_only' if message['thinking_text_present'] else 'empty_response'
    else: status = 'completed'
    censored = status in ('timeout', 'http_error', 'stream_error')
    first = [t for t in (message['first_final_text_ns'], message['first_valid_complete_tool_ns']) if t is not None]
    elapsed = delta_ns(transport.get('sent_ns'), transport.get('done_ns'))
    candidate_time = min(first) if first else None
    row.update(status=status, transport=transport, backend_request_id=engine.get('request_id'), correct_backend_join=joined,
               cold_cache=engine.get('cached_tokens') == 0 if joined else None, output_ids_count=count,
               native_generation_tokens=native_count, output_tokens=count if complete_ids else None,
               partial_output_ids_only=not complete_ids, usage_matches_output_ids=usage_matches,
               input_tokens=len(engine['prompt_ids']) if valid_ids(engine.get('prompt_ids')) else None,
               cached_tokens=engine.get('cached_tokens'), final_http_usage=usage, stop_reason=stop,
               engine_finish_reason=engine.get('engine_finish_reason'), engine_stop_reason=engine.get('engine_stop_reason'),
               sampling=engine.get('sampling'), reasoning=spans, reasoning_tokens=spans['reasoning_tokens'] if complete_ids else None,
               prompt_matches_cpu=prompt_matches, controls_match_request=controls_match,
               final_text=message['final_text'], tool_calls=message['tool_calls'], tools_executed=False,
               first_final_text_ns=message['first_final_text_ns'], first_valid_complete_tool_ns=message['first_valid_complete_tool_ns'],
               first_output_s=delta_ns(transport.get('sent_ns'), candidate_time) if not censored else None,
               latency_s=elapsed if not censored else None, observed_elapsed_s=elapsed, latency_censored=censored,
               engine_enter_to_first_output_s=delta_ns(engine.get('entered_monotonic_ns'),engine.get('first_output_monotonic_ns')),
               engine_enter_to_done_s=delta_ns(engine.get('entered_monotonic_ns'),engine.get('finished_monotonic_ns')),
               raw_evidence={'request':str(body_path),'chunks':str(chunks_path),'engine':str(folder/'engine-records.json')},
               errors=parse_errors + message['errors'] + spans['errors'] + engine.get('capture_errors', []))
    if fixture['category'] == 'coding_correctness':
        row['answer_exact_match'] = message['final_text'].strip() == fixture['acceptance']['expected_stdout']
        row['answer_accepted'] = (row['answer_exact_match'] and status == 'completed' and not message['tool_calls']
                                  and stop in ('end_turn', 'stop_sequence') and complete_ids)
    row['cost_eligible'] = (normal_eof and stream_ok and complete_ids and usage_matches and row['cold_cache'] is True
                            and engine.get('prompt_ids_match_final') is True and row['fixture_hash_matches'] and controls_match
                            and prompt_matches is True)
    return row


def summarize(rows):
    grouped, pairs = defaultdict(dict), []
    for row in rows: grouped[row['pair_id']][row['arm']] = row
    for pair_id, arms in grouped.items():
        a,b = arms.get('stock'),arms.get('patched')
        pair={'pair_id':pair_id,'fixture_id':(a or b)['fixture_id'],'repeat':(a or b)['repeat'],
              'stock_status':a['status'] if a else 'missing','patched_status':b['status'] if b else 'missing'}
        pair['both_cost_eligible']=bool(a and b and a['cost_eligible'] and b['cost_eligible'])
        for metric in ('output_tokens','reasoning_tokens','latency_s','first_output_s'):
            pair[metric+'_patched_minus_stock'] = b[metric]-a[metric] if pair['both_cost_eligible'] and a[metric] is not None and b[metric] is not None else None
        pair['answer_accepted_patched_minus_stock']=int(b['answer_accepted'])-int(a['answer_accepted']) if a and b and a['answer_accepted'] is not None and b['answer_accepted'] is not None else None
        pairs.append(pair)
    fixtures=[]
    for fixture_id in dict.fromkeys(r['fixture_id'] for r in rows):
        own=[p for p in pairs if p['fixture_id']==fixture_id]
        summary={'fixture_id':fixture_id,'pairs':own,'paired_differences':{}}
        for metric in ('output_tokens','reasoning_tokens','latency_s','first_output_s'):
            values=[p[metric+'_patched_minus_stock'] for p in own if p[metric+'_patched_minus_stock'] is not None]
            summary['paired_differences'][metric]={'values':values,'n':len(values),'median':statistics.median(values) if values else None,
                                                  'range':[min(values),max(values)] if values else None}
        fixtures.append(summary)
    categories=[]
    for category in dict.fromkeys(r['category'] for r in rows):
        categories.append({'category':category,'arms':{arm:dict(Counter(r['status'] for r in rows if r['category']==category and r['arm']==arm)) for arm in ('stock','patched')}})
    return {'fixtures':fixtures,'categories':categories,'direction':'paired differences are patched minus stock','population_interval':None}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--fixtures-root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--metadata',type=Path)
    parser.add_argument('--expected-prompts',type=Path,help='CPU map: fixture -> arm -> token IDs or SHA256 of compact JSON IDs')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.fixtures_root.resolve()
    manifest=load(root/'fixture-manifest.json')
    schedule_path=root/'fixtures/schedule.json'
    if hashlib.sha256(schedule_path.read_bytes()).hexdigest()!=manifest['schedule_sha256']: raise ValueError('schedule hash mismatch')
    schedule=load(schedule_path)
    fixtures={f['fixture_id']:f for f in manifest['fixtures']}
    for entry in schedule:
        if hashlib.sha256((root/entry['body_file']).read_bytes()).hexdigest()!=entry['body_sha256']: raise ValueError('fixture hash mismatch')
    outcomes=load(args.run/'outcomes.json',[])
    by_id={r.get('request_id') or r.get('schedule',{}).get('request_id'):r for r in outcomes}
    expected_prompts=load(args.expected_prompts) if args.expected_prompts else None
    rows=[]
    for entry in schedule:
        try:
            rows.append(analyze_row(entry,fixtures[entry['fixture_id']],root,args.run,by_id.get(entry['request_id']),expected_prompts))
        except (ValueError, TypeError, KeyError, OSError) as error:
            rows.append({**entry,'status':'analysis_error','cost_eligible':False,'answer_accepted':None,
                         'output_tokens':None,'reasoning_tokens':None,'latency_s':None,'first_output_s':None,
                         'errors':[type(error).__name__+': '+str(error)]})
    args.output.mkdir(parents=True,exist_ok=False)
    result={'rows':rows,'summary':summarize(rows),'metadata':load(args.metadata) if args.metadata else None,
            'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'fixture_manifest_sha256':hashlib.sha256((root/'fixture-manifest.json').read_bytes()).hexdigest(),
            'expected_prompts_sha256':hashlib.sha256(args.expected_prompts.read_bytes()).hexdigest() if args.expected_prompts else None,
            'jsonschema_version':importlib.metadata.version('jsonschema') if jsonschema is not None else None,
            'scope':'Eight fixed fixtures; snapshots are correlated points from one task; no population interval. Client and server clocks remain separate.'}
    (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    columns=['order','fixture_id','repeat','arm','status','output_tokens','reasoning_tokens','latency_s','first_output_s','answer_accepted','cost_eligible']
    lines=['\t'.join(columns)]+['\t'.join(str(r.get(k)) for k in columns) for r in rows]
    (args.output/'rows.tsv').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'rows':len(rows),'status_counts':dict(Counter(r['status'] for r in rows)),'output':str(args.output)}))


if __name__=='__main__': main()
