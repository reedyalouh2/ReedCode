"""Check and run the warmup regressions in a patched Dynamo checkout."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess


ROOT = Path(__file__).resolve().parent
REVISION = "f5d3353e2167bb0f0d729085eb5bc9183bf4b222"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
    checkout = args.checkout.resolve()
    renderer = args.renderer.resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != REVISION:
        raise ValueError("Use the pinned Dynamo revision")
    sources = {
        "dynamo/preprocessor.rs": "lib/llm/src/preprocessor.rs",
        "dynamo/speculative_prefill.rs": "lib/llm/src/preprocessor/speculative_prefill.rs",
        "dynamo/accumulator.rs": "lib/llm/src/preprocessor/speculative_prefill_accumulator.rs",
        "dynamo/contract.rs": "lib/llm/src/preprocessor/speculative_prefill_contract.rs",
    }
    for local, target in sources.items():
        if sha(ROOT / local) != sha(checkout / target):
            raise ValueError("Patched source differs: " + target)
    subprocess.run(
        ["git", "apply", "--reverse", "--check", str(ROOT / "dynamo.patch")],
        cwd=checkout, check=True,
    )
    for name in ("speculative.rs", "qwen3-tool-prefix.jinja"):
        if sha(ROOT / "renderer" / name) != sha(renderer / "src" / name):
            raise ValueError("Patched renderer differs: " + name)
    # A reverse-application check covers the modified renderer trait and formatter too.
    subprocess.run(
        ["git", "apply", "--reverse", "--check", str(ROOT / "renderer.patch")],
        cwd=renderer, check=True,
    )
    changed = set(subprocess.check_output(
        ["git", "-C", str(checkout), "-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=cat",
         "-c", "filter.lfs.required=false", "diff", "--name-only", "HEAD"], text=True
    ).splitlines())
    if changed - set(sources.values()) - {"Cargo.lock"}:
        raise ValueError("Unexpected tracked changes in Dynamo checkout: " + str(changed))
    args.output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    common = ["--offline", "--no-default-features", "-p", "dynamo-llm", "--config",
              'patch.crates-io.dynamo-renderer.path=' + json.dumps(str(renderer))]
    commands = [
        ("integration-build.log", [str(args.cargo), "check", "--tests", *common]),
        ("integration-tests.log", [str(args.cargo), "test", "--lib",
                                   "preprocessor::speculative_prefill", *common]),
    ]
    report = {
        "revision": revision, "platform": platform.platform(),
        "rustc": subprocess.check_output([env.get("RUSTC", "rustc"), "-Vv"], text=True),
        "cargo": subprocess.check_output([str(args.cargo), "-V"], text=True).strip(),
        "checkout": str(checkout), "renderer": str(renderer),
        "environment": {key: env[key] for key in (
            "RUSTC", "RUSTUP_HOME", "CARGO_HOME", "CARGO_TARGET_DIR", "CARGO_BUILD_JOBS", "PROTOC",
            "SWAGGER_UI_DOWNLOAD_URL"
        ) if key in env},
        "gpu_used": False, "commands": [],
        "files": {name: sha(ROOT / name) for name in [
            *sources, "dynamo.patch", "renderer.patch", "integration_check.py", "adapt_lifecycle.py"
        ]},
        "upstream_cargo_config_sha256": sha(checkout / ".cargo/config.toml"),
        "patched_lock_sha256": sha(checkout / "Cargo.lock"),
    }
    for name, command in commands:
        with (args.output / name).open("w") as log:
            result = subprocess.run(command, cwd=checkout, env=env, stdout=log, stderr=subprocess.STDOUT)
        report["commands"].append({"argv": command, "exit_code": result.returncode,
                                   "log": name, "sha256": sha(args.output / name)})
        (args.output / "integration-results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"{name}: exit {result.returncode}", flush=True)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--renderer", type=Path, default=Path("/tmp/reedcode-prefill-fix/renderer"))
    parser.add_argument("--cargo", type=Path, default=Path("/tmp/reedcode-rustup/toolchains/1.96.1-aarch64-apple-darwin/bin/cargo"))
    parser.add_argument("--output", type=Path, default=ROOT)
    run(parser.parse_args())
