"""Request a cache reset from one idle Dynamo worker; event confirmation is separate."""

import argparse
import asyncio
from datetime import datetime, timezone
import json


async def call_worker(runtime, endpoint):
    client = await runtime.endpoint(endpoint).client()
    instances = await client.wait_for_instances()
    if len(instances) != 1:
        raise ValueError("This study requires exactly one cache-reset endpoint instance")
    stream = await client.direct({}, instances[0], annotated=False)
    results = [item async for item in stream]
    if results != [{"status": "success", "message": "KV cache cleared"}]:
        raise ValueError("Worker did not report a successful reset: " + json.dumps(results))
    return {"endpoint": endpoint, "instance_id": instances[0],
            "response": results, "backend_reset_reported": True,
            "router_reset_verified": False, "kv_clear_event_verified": False,
            "timestamp_utc": datetime.now(timezone.utc).isoformat()}


async def reset(endpoint, timeout):
    from dynamo.runtime import dynamo_worker

    @dynamo_worker()
    async def call(runtime):
        print(json.dumps(await call_worker(runtime, endpoint), indent=2))

    await asyncio.wait_for(call(), timeout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.endpoint.endswith(".clear_kv_blocks") or not 0 < args.timeout <= 60:
        parser.error("Use the deployed clear_kv_blocks endpoint and a timeout in (0, 60]")
    if args.execute:
        asyncio.run(reset(args.endpoint, args.timeout))
    else:
        print(json.dumps({"endpoint": args.endpoint, "execute": False,
                          "note": "No reset sent. Requires an idle backend and live event confirmation."}, indent=2))
