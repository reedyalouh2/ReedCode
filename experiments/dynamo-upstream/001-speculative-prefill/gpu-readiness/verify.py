"""Verify the frozen replay packet, optionally rerunning both native builders."""

import argparse
import gzip
import json
from pathlib import Path

from prepare import FINDING, ROOT, common_prefix, json_bytes, native, sha


def read_gzip(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def verify(directory=ROOT / "fixtures", stock_binary=None, fixed_binary=None):
    manifest = json.loads((directory / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or sha(path.read_bytes()) != expected:
            raise ValueError("Frozen file changed: " + name)
    for name, expected in manifest["patch_sha256"].items():
        if sha((FINDING / "fix" / name).read_bytes()) != expected:
            raise ValueError("Patch differs from frozen workload: " + name)
    inputs = read_gzip(directory / "input.json.gz")
    stock = {row["case"]: row for row in read_gzip(directory / "stock-native.json.gz")}
    fixed = {row["case"]: row for row in read_gzip(directory / "fixed-native.json.gz")}
    expected_names = [case["name"] for case in inputs["cases"]]
    if list(stock) != expected_names or list(fixed) != expected_names:
        raise ValueError("Native cases differ from frozen inputs")
    checks = []
    for case in inputs["cases"]:
        a, b = stock[case["name"]], fixed[case["name"]]
        if a["original"]["token_ids"] != b["original_ids"] or a["followup"]["token_ids"] != b["followup_ids"]:
            raise ValueError("Normal tokens differ")
        if not b["prepared_ids"] or b["followup_ids"][:len(b["prepared_ids"])] != b["prepared_ids"]:
            raise ValueError("Fixed preparation is not an exact prefix")
        if common_prefix(a["prepared"]["token_ids"], b["followup_ids"]) == len(a["prepared"]["token_ids"]):
            raise ValueError("Expected stock mismatch did not reproduce")
        checks.append({"case": case["name"], "original_tokens": len(b["original_ids"]),
                       "followup_tokens": len(b["followup_ids"]),
                       "stock_warmup_tokens": len(a["prepared"]["token_ids"]),
                       "stock_first_difference": common_prefix(a["prepared"]["token_ids"], b["followup_ids"]),
                       "fixed_warmup_tokens": len(b["prepared_ids"]), "fixed_exact_prefix": True,
                       "normal_tokens_unchanged": True})
    if checks != manifest["checks"]:
        raise ValueError("Reported native checks differ from the token arrays")
    for name in ("short", "long"):
        plan = read_gzip(directory / f"{name}-replay.json.gz")
        for row in plan["sequence"]:
            ids = row["payload"]["prompt"]
            if sha(json_bytes(ids)) != row["tokens_sha256"]:
                raise ValueError("Replay token hash changed")
            if len(ids) + row["payload"]["max_tokens"] > 32768:
                raise ValueError("Replay exceeds server context")
            a, b = stock[row["case"]], fixed[row["case"]]
            if row["kind"] == "warmup":
                wanted = a["prepared"]["token_ids"] if row["condition"] == "stock" else b["prepared_ids"]
            else:
                wanted = a["original"]["token_ids"] if row["id"].endswith("/normal-0") else a["followup"]["token_ids"]
            if ids != wanted:
                raise ValueError("Replay differs from the selected native builder")
    if (stock_binary is None) != (fixed_binary is None):
        raise ValueError("Provide both native binaries or neither")
    if stock_binary:
        for label, binary, stored in (("stock", stock_binary, stock), ("fixed", fixed_binary, fixed)):
            if sha(binary.read_bytes()) != manifest[label + "_binary_sha256"]:
                raise ValueError("Native binary differs from frozen build: " + label)
            actual = {row["case"]: row for row in json.loads(native(binary, inputs))}
            if actual != stored:
                raise ValueError("Native rerun differs: " + label)
    return {"cases": len(inputs["cases"]), "fixed_exact_prefixes": len(fixed), "normal_tokens_unchanged": True,
            "hashes_verified": len(manifest["files"]), "native_builders_rerun": stock_binary is not None,
            "model_requests_made": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "fixtures")
    parser.add_argument("--stock-binary", type=Path)
    parser.add_argument("--fixed-binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory, args.stock_binary, args.fixed_binary), indent=2))
