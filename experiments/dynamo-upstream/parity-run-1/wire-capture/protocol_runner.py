"""Send saved client bodies through shipped stock frontend into a CPU capture worker."""
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

root = Path('/work')
output = Path(os.environ.get('PROTOCOL_OUTPUT', '/work/protocol-output'))
output.mkdir(exist_ok=True)
env = os.environ.copy()
env.update(DYN_FILE_KV=str(output / 'discovery'), DYN_TCP_RESPONSE_STREAM_HOST='lo', DYN_LOGGING_JSONL='1',
           DYN_LOG='info,dynamo_llm::preprocessor=trace,dynamo_runtime::pipeline::network::ingress::push_handler=trace',
           DYN_TOKENIZER_BACKEND='default', DYN_TOKENIZER_CACHE='0',
           DYN_SYSTEM_PORT='-1', DYN_SELF_HOST_METADATA='0', DYN_COMPUTE_THREADS='2')
python = '/work/venvs/backend/bin/python'
handles=[]; processes=[]
try:
    commands=[('worker', [python, str(Path(__file__).with_name('capture_worker.py'))]),
              ('frontend', [python, '-m', 'dynamo.frontend', '--discovery-backend', 'file', '--request-plane', 'tcp', '--http-port', '18000', '--enable-anthropic-api'])]
    for name,command in commands:
        handle=(output/f'{name}.log').open('w');handles.append(handle)
        processes.append(subprocess.Popen(command, env=env,stdout=handle,stderr=subprocess.STDOUT))
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
    outcomes=[]
    client=os.environ.get('PROTOCOL_CLIENT', 'claude')
    endpoint='/v1/messages' if client=='claude' else '/v1/responses'
    for index in (1,2):
        raw=(root/'requests'/f'{client}-{index}.json').read_bytes()
        request_headers={'Content-Type':'application/json','anthropic-version':'2023-06-01'}
        if os.environ.get('WIRE_REQUEST_IDS') == '1':
            request_headers['X-Request-ID'] = f'wire-{client}-{index}'
        req=Request('http://127.0.0.1:18000'+endpoint,data=raw,headers=request_headers)
        started=time.monotonic()
        try:
            with urlopen(req,timeout=90) as response:
                status=response.status;body=response.read();headers=dict(response.headers)
        except HTTPError as error:
            status=error.code;body=error.read();headers=dict(error.headers)
        (output/f'{client}-{index}-response.body').write_bytes(body)
        outcomes.append({'request':f'{client}-{index}.json','status':status,'headers':headers,
                         'observer_x_request_id':request_headers.get('X-Request-ID'),
                         'wall_seconds':time.monotonic()-started})
    (output/'outcomes.json').write_text(json.dumps(outcomes,indent=2))
    print(json.dumps(outcomes,indent=2))
finally:
    for p in processes:
        p.terminate()
    for p in processes:
        try:p.wait(timeout=10)
        except subprocess.TimeoutExpired:p.kill();p.wait(timeout=10)
    for h in handles:h.close()
