"""Write a complete local replay record using a mock HTTP transport."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from openai import AsyncOpenAI

from dynamo_fixture import FixtureTransport
from dynamo_replay import execute
from run_dynamo import build_plan


async def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = ROOT / "runs" / f"dynamo-local-check-{stamp}"
    output.mkdir(parents=True)
    workload = json.loads((ROOT / "experiments/dynamo/workload.json").read_text())
    (output / "workload.json").write_text(json.dumps(workload, indent=2) + "\n")
    manifest = {"study_kind": "dynamo_replay", "schema_version": 1, "state": "planned",
                "environment": "mock", "model": "fixture", "copies": 2, "horizon_s": 5,
                "plan": [{k: p[k] for k in ("run_id", "repeat", "position", "condition")}
                         for p in build_plan(repeats=2)], "epochs": [],
                "workload_sha256": hashlib.sha256((output / "workload.json").read_bytes()).hexdigest(),
                "cache_policy": "carry_between_epochs", "hint_application": "unverified"}
    args = SimpleNamespace(model="fixture", copies=2, horizon_seconds=5,
                           metrics_url=None, metrics_identity_url=None, sample_interval=0.1)
    fixture = FixtureTransport()
    async with AsyncOpenAI(base_url="http://fixture/v1", api_key="fixture", max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(fixture.handle))) as client:
        await execute(args, workload, output, manifest, client)
    assert len(fixture.requests) == 24
    assert all(e["status"] == "finished" for e in manifest["epochs"])
    print(f"Local transport check passed: 4 epochs, 8 workflows, 24 requests including cleanup.\n{output}")
    print("Mock responses only. No Dynamo server, model inference, or GPU measurements.")


if __name__ == "__main__":
    asyncio.run(main())
