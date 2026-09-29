"""Summarize verified controlled replay results and derive full-block KV payload."""
import argparse
import hashlib
import gzip
import json
from pathlib import Path
import re
import statistics

parser=argparse.ArgumentParser()
parser.add_argument('--raw',type=Path,required=True)
parser.add_argument('--repo',type=Path,required=True)
parser.add_argument('--allocation-evidence',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
a=parser.parse_args(); raw=a.raw.resolve(); out=a.output.resolve()
report=json.loads((out/'report.json').read_text())
manifest=json.loads((raw/'snapshot-manifest.json').read_text())
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def verified(path):
    name=str(path.relative_to(raw))
    if sha(path)!=manifest['files'][name]:raise ValueError('Raw member changed: '+name)
    return path

epoch=raw/report['provenance']['selected_epoch']
source_manifest_path=a.allocation_evidence/'kv-allocation-source/manifest.json'
source_manifest=json.loads(source_manifest_path.read_text())
for item in source_manifest['sources']:
    path=a.allocation_evidence/'kv-allocation-source'/Path(item['path']).relative_to('/tmp/reedcode-gpu-evidence-02/kv-allocation-source')
    if sha(path)!=item['sha256']:raise ValueError('Allocation source changed')
model_path=a.repo/'experiments/dynamo-upstream/001-speculative-prefill/kv-footprint/model-config.json'
model=json.loads(model_path.read_text())
foundation_path=a.allocation_evidence/'kv-allocation-proof.json'
foundation=json.loads(foundation_path.read_text())
if sha(model_path)!=foundation['inputs']['matching_model_config']['sha256']:raise ValueError('Model geometry differs')
if sha(source_manifest_path)!=foundation['inputs']['source_manifest']['sha256']:raise ValueError('Source revision differs')
backend=verified(epoch/'backend.log'); backend_text=backend.read_text()
required=("model='Qwen/Qwen3-8B'",'revision=b968826d9c46dd6066d109eabc6255188de91218',
          'dtype=torch.bfloat16','tensor_parallel_size=1','pipeline_parallel_size=1','kv_cache_dtype=auto',
          'Using FLASH_ATTN attention backend','Using FlashAttention version 2')
if not all(value in backend_text for value in required):raise ValueError('Selected epoch does not match allocation geometry')
capacity=int(re.search(r'GPU KV cache size: ([\d,]+) tokens',backend_text)[1].replace(',',''))
rounded_pool=float(re.search(r'Available KV cache memory: ([\d.]+) GiB',backend_text)[1])
bytes_per_token=model['num_hidden_layers']*2*model['num_key_value_heads']*model['head_dim']*2
bytes_per_block=bytes_per_token*16
if bytes_per_block!=2359296:raise ValueError('Unexpected block payload')
cache_configs={}; rows=[]; groups={}; raw_requests=[]; peaks=[]
for trial in report['trials']:
    folder=raw/trial['directory']; requests=[json.loads(line) for line in verified(folder/'replay/requests.jsonl').read_text().splitlines()]
    for request in requests:
        for side in ('before','after'):
            names=[name for name in request['observations'][side]['files'] if name.endswith('.metrics.txt')]
            if len(names)!=1:raise ValueError('Ambiguous metrics observation')
            path=verified(folder/'replay'/names[0]); text=path.read_text()
            lines=[line for line in text.splitlines() if line.startswith('vllm:cache_config_info{')]
            if len(lines)!=1:raise ValueError('Ambiguous cache configuration')
            labels={key:json.loads('"'+value+'"') for key,value in re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"',lines[0])}
            for key,value in {'model_name':'Qwen/Qwen3-8B','block_size':'16','cache_dtype':'auto','engine':'0','enable_prefix_caching':'True','sliding_window':'None'}.items():
                if labels.get(key)!=value:raise ValueError('Cache geometry changed: '+key)
            if int(labels['kv_cache_size_tokens'])!=capacity or int(labels['num_gpu_blocks'])*16!=capacity:raise ValueError('Pool geometry differs')
            cache_configs[json.dumps(labels,sort_keys=True)]={'labels':labels,'example':str(path.relative_to(raw)),'sha256':sha(path)}
    terminal=trial['terminal']
    if not trial['attributable'] or trial['sequence_issues'] or terminal['canonical_prefixes']!=terminal['published_block_entries']:
        raise ValueError('Cannot derive unique retained-block payload')
    real=[row for row in trial['request_metrics'] if row['kind']=='real']; warm=[row for row in trial['request_metrics'] if row['kind']=='warmup']
    branch=terminal['counts']['warmup_only']; current=terminal['current_real_full_blocks']
    available=[item for item in trial['snapshots'] if item['attributable'] and item['warmup_only_to_current_full_blocks'] is not None]
    peak=max(available,key=lambda item:item['warmup_only_to_current_full_blocks'])
    peaks.append({'session':trial['session'],'condition':trial['condition'],'repeat':trial['repeat'],'snapshot':peak})
    row={'session':trial['session'],'condition':trial['condition'],'repeat':trial['repeat'],
         'total_scheduled_prefill_tokens':trial['scheduled_prefill_tokens'],
         'real_scheduled_prefill_tokens':sum(x['local_compute_prefill_tokens']['value'] for x in real),
         'warmup_scheduled_prefill_tokens':sum(x['local_compute_prefill_tokens']['value'] for x in warm),
         'followup_cached_tokens':sum(x['cached_tokens'] for x in real[1:]),
         'warmup_cached_tokens':sum(x['cached_tokens'] for x in warm),
         'followup_completion_seconds':sum(x['completion_seconds'] for x in real[1:]),
         'mean_followup_visible_ttft_seconds':statistics.mean(x['client_first_visible_text_seconds'] for x in real[1:]),
         'warmup_request_seconds':sum(x['completion_seconds'] for x in warm),
         'all_request_seconds':sum(x['completion_seconds'] for x in trial['request_metrics']),
         'replay_wall_seconds':max(x['ended_unix'] for x in requests)-min(x['started_unix'] for x in requests),
         'terminal_warmup_only_full_blocks':branch,'terminal_current_real_full_blocks':current,
         'terminal_current_real_tokens':terminal['current_real_tokens'],
         'terminal_older_real_blocks_outside_current':terminal['older_real_entries_outside_current_input'],
         'terminal_warmup_only_to_current_full_blocks':branch/current,
         'terminal_warmup_only_to_current_tokens':branch*16/terminal['current_real_tokens'],
         'derived_branch_payload_bytes':branch*bytes_per_block,
         'derived_current_full_context_payload_bytes':current*bytes_per_block}
    rows.append(row); groups.setdefault((row['session'],row['condition']),[]).append(row)
    for request,measured in zip(requests,trial['request_metrics']):
        if request['id']!=measured['fixture_id']:raise ValueError('Request order changed')
        raw_requests.append({'session':row['session'],'condition':row['condition'],'repeat':row['repeat'],
            'fixture_id':request['id'],'kind':request['kind'],'prompt_tokens':request['prompt_tokens'],
            'cached_tokens':request['cached_tokens'],'local_compute_tokens':measured['local_compute_prefill_tokens']['value'],
            'completion_seconds':request['completion_seconds'],'request_id':request['request_id']})
if len(cache_configs)!=1:raise ValueError('Cache/process identity changed across the measured study')
cache_config=next(iter(cache_configs.values()))
if round(int(cache_config['labels']['num_gpu_blocks'])*bytes_per_block/(1024**3),2)!=rounded_pool:raise ValueError('Rounded pool size inconsistent')
means=[]
for (session,condition),trials in sorted(groups.items()):
    if sorted(x['repeat'] for x in trials)!=[1,2,3]:raise ValueError('Incomplete repetition group')
    result={'session':session,'condition':condition,'repetitions':3}
    for key in trials[0]:
        if key not in ('session','condition','repeat'):
            result[key]={'mean':statistics.mean(x[key] for x in trials),'values_by_repeat':[x[key] for x in sorted(trials,key=lambda x:x['repeat'])]}
    means.append(result)
paired=[]
for session in ('short','long'):
 for repeat in (1,2,3):
  cell={row['condition']:row for row in rows if row['session']==session and row['repeat']==repeat}
  for before,after in (('off','stock'),('stock','fixed'),('off','fixed')):
   left,right=cell[before],cell[after]
   differences={key:right[key]-left[key] for key in left if key not in ('session','condition','repeat')}
   paired.append({'session':session,'repeat':repeat,'comparison':after+' minus '+before,'differences':differences,
      'scheduled_prefill_percent_change':100*differences['total_scheduled_prefill_tokens']/left['total_scheduled_prefill_tokens']})
previous=raw/'prefill/epochs/00748a31eacf496691698fb49d22e684'
previous_batch=json.loads(verified(previous/'prefill-batch.json').read_text())
previous_trial=json.loads(verified(previous/'trials/short-off-r1/trial.json').read_text())
previous_requests=verified(previous/'trials/short-off-r1/replay/requests.jsonl')
previous_stderr=verified(previous/'short-off-r1.stderr')
if previous_requests.read_bytes() or 'before = observe(' not in previous_stderr.read_text():
    raise ValueError('Earlier attempt is no longer a pre-dispatch observation failure')
previous_evidence={'epoch':previous.name,'status':previous_batch['status'],'completed_trials':previous_batch['completed_trials'],
    'failed_trial':{'session':previous_trial['session'],'condition':previous_trial['condition'],'repeat':previous_trial['repeat'],
                    'status':previous_trial['status'],'error_type':previous_trial['error_type'],'error':previous_trial['error']},
    'raw_trial_path':str(previous.relative_to(raw))+'/trials/short-off-r1/trial.json',
    'failure_origin':'measurement_identity_endpoint_before_first_replay_model_request',
    'replay_model_requests_sent':0,
    'explanation':'The measurement identity sidecar returned HTTP 503 after the backend launcher was reparented to PID 1. The before-request observation failed before model dispatch. Dynamo did not return a model-request 503.',
    'scope':'Generated-tool smokes and reset/canary requests occurred. No replay model request was sent and no measured replay trial completed. This epoch is excluded from the successful 18-cell study.',
    'smokes':{kind:json.loads(verified(previous/(kind+'-warmup-check.stdout')).read_text()) for kind in ('stock','fixed')}}
allocation={'epoch':epoch.name,'model_revision':'b968826d9c46dd6066d109eabc6255188de91218','model_config_sha256':sha(model_path),
    'backend_log':str(backend.relative_to(raw)),'backend_log_sha256':sha(backend),'required_backend_configuration_evidence':list(required),
    'cache_configuration':cache_config,'source_manifest_sha256':sha(source_manifest_path),'source_revision':source_manifest['revision'],
    'source_points':foundation['source_points'],'source_foundation_sha256':sha(foundation_path),'geometry':foundation['geometry'],
    'bytes_per_full_token':bytes_per_token,'bytes_per_full_block':bytes_per_block,'MiB_per_full_block':bytes_per_block/1024**2,
    'configured_pool_blocks':int(cache_config['labels']['num_gpu_blocks']),'derived_configured_pool_payload_bytes':int(cache_config['labels']['num_gpu_blocks'])*bytes_per_block,
    'direct_physical_allocator_measurement':False,
    'interpretation':'Payload represented by retained full published GPU blocks, derived from exact model geometry and selected-epoch dtype/backend/pool configuration. The pool is preallocated. This is not incremental GPU allocation, nvidia-smi growth, partial blocks, metadata or allocator overhead.',
    'limits':[value for value in foundation['limitations'][:-1] if not value.startswith('The observed pool counts')]}
summary={'epoch':epoch.name,'trials':rows,'condition_summaries':means,'paired_differences':paired,'requests':raw_requests,
 'allocation':allocation,'peak_published_branch':peaks,'previous_failed_attempt':previous_evidence,
 'provenance':{'report_sha256':sha(out/'report.json'),'wire_sha256':hashlib.sha256(gzip.decompress((out/'wire.json.gz').read_bytes())).hexdigest(),'snapshot_manifest_sha256':sha(raw/'snapshot-manifest.json'),'script_sha256':sha(Path(__file__))}}
(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
(out/'allocation.json').write_text(json.dumps(allocation,indent=2)+'\n')
(out/'previous-failed-attempt.json').write_text(json.dumps(previous_evidence,indent=2)+'\n')
print(json.dumps({'summary':str(out/'summary.json'),'epoch':epoch.name,'trials':len(rows),'requests':len(raw_requests),'pool_blocks':allocation['configured_pool_blocks'],'bytes_per_full_block':bytes_per_block}))
for row in means:
 print(row['session'],row['condition'],{key:row[key]['mean'] for key in ('followup_completion_seconds','all_request_seconds','replay_wall_seconds','warmup_request_seconds')})
for session in ('short','long'):
 print(session,'paired percentages',[(row['comparison'],row['scheduled_prefill_percent_change']) for row in paired if row['session']==session and row['repeat']==1])
