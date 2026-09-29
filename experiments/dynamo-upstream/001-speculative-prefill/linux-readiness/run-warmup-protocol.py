"""Exercise installed frontend warmup using scripted CPU worker output."""
import json
import shutil
import os
from pathlib import Path
import subprocess
import time
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

root = Path('/work')
output = Path(os.environ.get('PROTOCOL_OUTPUT', '/work/protocol-output'))
output.mkdir(exist_ok=True)
used=output/'used-scripts'
used.mkdir(exist_ok=True)
for saved_script in [Path(__file__), Path('/work/warmup-worker.py'), Path('/work/stock_probe.py')]:
    shutil.copyfile(saved_script, used/saved_script.name)
env = os.environ.copy()
env.update(DYN_FILE_KV=str(output / 'discovery'), DYN_TCP_RESPONSE_STREAM_HOST='lo', DYN_LOGGING_JSONL='1',
           DYN_LOG='info,dynamo_llm::preprocessor=trace,dynamo_runtime::pipeline::network::ingress::push_handler=trace',
           DYN_TOKENIZER_BACKEND='default', DYN_TOKENIZER_CACHE='0',
           DYN_SYSTEM_PORT='-1', DYN_SELF_HOST_METADATA='0', DYN_COMPUTE_THREADS='2')
python = '/work/venvs/backend/bin/python'
frontend_python = os.environ.get('FRONTEND_PYTHON', python)
handles=[]; processes=[]
try:
    commands=[('worker', [python, '/work/warmup-worker.py']),
              ('frontend', [frontend_python, '-m', 'dynamo.frontend', '--discovery-backend', 'file', '--request-plane', 'tcp', '--http-port', '18000', '--enable-anthropic-api'])]
    for name,command in commands:
        handle=(output/f'{name}.log').open('w');handles.append(handle)
        component_env=env.copy()
        component_env.update(DYN_TCP_RPC_PORT='20000' if name=='worker' else '20002',
                             DYN_TCP_RESPONSE_STREAM_PORT='20001' if name=='worker' else '20003')
        processes.append(subprocess.Popen(command, env=component_env,stdout=handle,stderr=subprocess.STDOUT))
    deadline=time.monotonic()+90
    while True:
        if any(p.poll() is not None for p in processes): raise RuntimeError('Frontend or worker exited before readiness')
        try:
            with urlopen('http://127.0.0.1:18000/v1/models',timeout=2) as response:
                models=json.load(response)
            if any(m['id']=='Qwen/Qwen3-8B' for m in models.get('data',[])):break
        except (URLError,TimeoutError):pass
        if time.monotonic()>deadline:raise TimeoutError('Model discovery readiness timeout')
        time.sleep(0.5)
    (output/'models.json').write_text(json.dumps(models,indent=2))
    import httpx
    from stock_probe import request_pair_start, parse_response, followup
    case=os.environ['WARMUP_CASE']
    enabled=os.environ['WARMUP_HINT']=='on'
    request=request_pair_start(case,enabled,'linux-installed-wheel-smoke')
    if os.environ.get('WARMUP_THINKING')=='disabled':
        request['chat_template_kwargs']={'enable_thinking':False}
    responses=[]
    with httpx.Client(timeout=30) as client:
        for turn in range(2):
            (output/f'request-{turn}.json').write_text(json.dumps(request,indent=2))
            with client.stream('POST','http://127.0.0.1:18000/v1/chat/completions',json=request) as response:
                response.raise_for_status()
                lines=list(response.iter_lines())
            (output/f'response-{turn}.sse').write_text('\n'.join(lines))
            parsed=parse_response(lines)
            responses.append(parsed)
            (output/f'parsed-{turn}.json').write_text(json.dumps(parsed,indent=2))
            if turn==0:
                request=followup(request,parsed,case)
                time.sleep(2)
    time.sleep(0.2)
    records=[json.loads(p.read_text()) for p in sorted(output.glob('backend-*.json'))]
    warmups=[r for r in records if r['stop_conditions'].get('max_tokens')==1]
    normal=[r for r in records if r['stop_conditions'].get('max_tokens')!=1]
    assert len(normal)==2
    expected_warmups=int(enabled and (os.environ['WARMUP_VARIANT']=='stock' or case=='tool'))
    assert len(warmups)==expected_warmups, (len(warmups), expected_warmups)
    result={'case':case,'hint':enabled,'frontend_python':frontend_python,
            'normal_requests':len(normal),'warmup_requests':len(warmups),
            'normal_input_token_counts':[len(r['token_ids']) for r in normal],
            'scripted_worker':True,'gpu_inference':False}
    if warmups:
        candidate=warmups[0]['token_ids'];actual=normal[1]['token_ids']
        common=next((i for i,(a,b) in enumerate(zip(candidate,actual)) if a!=b),min(len(candidate),len(actual)))
        result.update(warmup_tokens=len(candidate),common_prefix_tokens=common,
                      prepared_full_block_tokens=len(candidate)//16*16,
                      matched_full_block_tokens=common//16*16,
                      all_prepared_full_blocks_match=common//16==len(candidate)//16)
    (output/'outcomes.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))

finally:
    for p in processes:
        p.terminate()
    for p in processes:
        try:p.wait(timeout=10)
        except subprocess.TimeoutExpired:p.kill();p.wait(timeout=10)
    for h in handles:h.close()
