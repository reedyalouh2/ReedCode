import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from dynamo_metrics import EpochMetrics

ROOT = Path.cwd()
OUTPUT = ROOT / 'runs/dynamo-prefix-20260928'
spec = importlib.util.spec_from_file_location('cache_probe', ROOT / 'experiments/dynamo-prefix/probe_gpu.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

async def main():
    metrics = EpochMetrics('http://127.0.0.1:18081/metrics', model_name='Qwen/Qwen3-8B',
                           sample_interval=0.25, identity_url='http://127.0.0.1:19099/identity')
    args = SimpleNamespace(tokenizer=Path('/tmp/reedcode-prefix-tokenizer/tokenizer.json'),
                           base_url='http://127.0.0.1:18000/v1',
                           server_identity='deployment.json: A100-SXM4-80GB, Dynamo1.5.0/vLLM0.28.0, Qwen3-8B b968826d9c46dd6066d109eabc6255188de91218',
                           block_size=16, cases=['tool','text','tool_long'], output=OUTPUT/'probe.json')
    try:
        async with metrics:
            good = await probe.main_async(args)
    finally:
        (OUTPUT/'metrics.json').write_text(json.dumps(metrics.result,indent=2)+'\n')
    print(json.dumps({'cache_checks_pass':good,'metrics_valid':metrics.result['valid_boundaries'],
                      'requests':metrics.result['counter_deltas']['requests_finished']},indent=2))
    if not good: raise SystemExit(1)

asyncio.run(main())
