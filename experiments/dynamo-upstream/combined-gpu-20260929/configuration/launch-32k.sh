#!/usr/bin/env bash
set -euo pipefail
study_dir="${1:?Usage: launch-on-pod.sh STUDY_DIR prefill|parity backend|image|stock|fixed}"
[[ "$study_dir" = /* ]] || { echo "STUDY_DIR must be absolute" >&2; exit 2; }
study="${2:?Missing study}"
component="${3:?Missing component}"
epoch="${4:-$(date -u +%Y%m%dT%H%M%S)-$$}"
[[ "$epoch" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "Invalid epoch" >&2; exit 2; }
case "$study" in
  prefill) thinking=disabled ;;
  parity) thinking=enabled ;;
  *) echo 'Study must be prefill or parity' >&2; exit 2 ;;
esac
case "$component" in backend|image|stock|fixed) ;; *) exit 2 ;; esac
if [[ "$study" == parity && "$component" != backend && "$component" != image ]]; then
  echo 'Parity uses the unmodified image frontend' >&2
  exit 2
fi
run_dir="$study_dir/$study"
mkdir -p "$run_dir"
cd "$run_dir"
export HF_HOME="$study_dir/hf"
export DYN_FILE_KV="$run_dir/discovery"
export DYN_DISCOVERY_BACKEND=file
export DYN_REQUEST_PLANE=tcp
export DYN_EVENT_PLANE=zmq
export DYN_TOKENIZER_BACKEND=default
export DYN_TOKENIZER_CACHE=0
export DYN_LOGGING_CONSOLE_FORMAT=jsonl
export DYN_LOG='info,dynamo_llm::preprocessor=trace,dynamo_kv_router::indexer::kv_indexer=trace,dynamo_llm::kv_router::indexer::kv_indexer=trace'
export DYN_SELF_HOST_METADATA=0
export DYN_TCP_RESPONSE_STREAM_HOST=lo
export DYN_REQUEST_TRACE=1
export DYN_REQUEST_TRACE_SINKS=file
export DYN_REQUEST_TRACE_FILE_FORMAT=jsonl
export DYN_REQUEST_TRACE_RECORDS=request_end,request_payload
export DYN_REQUEST_TRACE_FILE_PATH="$run_dir/$component-$epoch-trace.jsonl"
if [[ "$component" == backend ]]; then
  export DYN_SYSTEM_PORT=8081
  export DYN_TCP_RPC_PORT=20000
  export DYN_TCP_RESPONSE_STREAM_PORT=20001
  exec python3 -m dynamo.vllm \
    --model Qwen/Qwen3-8B --revision b968826d9c46dd6066d109eabc6255188de91218 \
    --discovery-backend file --request-plane tcp \
    --dtype bfloat16 --kv-cache-dtype auto --tensor-parallel-size 1 \
    --max-model-len 32768 --gpu-memory-utilization 0.80 \
    --enable-prefix-caching --block-size 16 \
    --kv-events-config '{"enable_kv_cache_events":true,"publisher":"zmq","topic":"kv-events","endpoint":"tcp://*:5557"}' \
    --dyn-tool-call-parser hermes --dyn-reasoning-parser qwen3 \
    --dyn-default-thinking-mode "$thinking"
fi
export DYN_SYSTEM_PORT=8082
export DYN_TCP_RPC_PORT=0
export DYN_TCP_RESPONSE_STREAM_PORT=20003
if [[ "$component" == image ]]; then
  frontend_python=python3
else
  frontend_python="$study_dir/frontend-$component/bin/python"
fi
harness_flags=(--enable-anthropic-api)
if [[ "$study" == parity ]]; then
  harness_flags+=(--strip-anthropic-preamble --enable-streaming-tool-dispatch)
fi
exec "$frontend_python" -m dynamo.frontend \
  --discovery-backend file --request-plane tcp --router-mode kv \
  --http-host 127.0.0.1 --http-port 8000 "${harness_flags[@]}"
