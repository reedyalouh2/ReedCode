"""CPU wire capture; this does not load a model or implement inference."""
import asyncio
import json
import os
from pathlib import Path
import uvloop
from dynamo.llm import ModelInput, ModelType, WorkerType, ModelRuntimeConfig, register_model
from dynamo.runtime import DistributedRuntime

OUTPUT = Path(os.environ.get('PROTOCOL_OUTPUT', '/work/protocol-output'))
OUTPUT.mkdir(exist_ok=True)

async def generate(request, context):
    index = len(list(OUTPUT.glob('backend-*.json'))) + 1
    (OUTPUT / f'backend-{index}.json').write_text(json.dumps(request, indent=2))
    yield {'token_ids': [151668, 198, 151645], 'tokens': [], 'text': None,
           'cum_log_probs': None, 'log_probs': None, 'top_logprobs': None,
           'finish_reason': 'stop', 'index': 0}

async def main():
    runtime = DistributedRuntime(asyncio.get_running_loop(), 'file', 'tcp')
    endpoint = runtime.endpoint('readiness.capture.generate')
    config = ModelRuntimeConfig()
    config.context_length = 32768
    config.tool_call_parser = 'hermes'
    config.reasoning_parser = 'qwen3'
    config.set_engine_specific('default_thinking_mode', 'true')
    await register_model(ModelInput.Tokens, ModelType.Chat, endpoint,
                         '/work/model', model_name='Qwen/Qwen3-8B',
                         kv_cache_block_size=16, runtime_config=config,
                         worker_type=WorkerType.Aggregated, ignore_weights=True)
    await endpoint.serve_endpoint(generate)

if __name__ == '__main__':
    uvloop.run(main())
