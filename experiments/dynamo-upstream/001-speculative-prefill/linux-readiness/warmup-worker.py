"""Return scripted model tokens to exercise the frontend's real warmup path."""
import asyncio
import json
import os
from pathlib import Path
import uvloop
from tokenizers import Tokenizer
from dynamo.llm import ModelInput, ModelType, WorkerType, ModelRuntimeConfig, register_model
from dynamo.runtime import DistributedRuntime

output=Path(os.environ['PROTOCOL_OUTPUT'])
output.mkdir(exist_ok=True)
tokenizer=Tokenizer.from_file('/work/model/tokenizer.json')
normal_calls=0

async def generate(request, context):
    global normal_calls
    index=len(list(output.glob('backend-*.json'))) + 1
    (output/f'backend-{index}.json').write_text(json.dumps(request,indent=2))
    warmup=request['stop_conditions'].get('max_tokens')==1
    if warmup:
        ids=[151645]
    else:
        normal_calls+=1
        if normal_calls==1 and os.environ['WARMUP_CASE']=='tool':
            text='<tool_call>\n{"name": "record_value", "arguments": {"value": "ready"}}\n</tool_call>'
        else:
            text='ready.' if normal_calls==1 else 'done.'
        ids=tokenizer.encode(text,add_special_tokens=False).ids+[151645]
    yield {'token_ids':ids,'tokens':[],'text':None,'cum_log_probs':None,
           'log_probs':None,'top_logprobs':None,'finish_reason':'stop','index':0}

async def main():
    runtime=DistributedRuntime(asyncio.get_running_loop(),'file','tcp')
    endpoint=runtime.endpoint('readiness.capture.generate')
    config=ModelRuntimeConfig()
    config.context_length=32768
    config.tool_call_parser='hermes'
    config.reasoning_parser='qwen3'
    config.set_engine_specific('default_thinking_mode',json.dumps('disabled'))
    await register_model(ModelInput.Tokens,ModelType.Chat,endpoint,'/work/model',
                         model_name='Qwen/Qwen3-8B',kv_cache_block_size=16,
                         runtime_config=config,worker_type=WorkerType.Aggregated,
                         ignore_weights=True)
    await endpoint.serve_endpoint(generate)

if __name__=='__main__':
    uvloop.run(main())
