"""Reproduce the constructed checks against downloaded, pinned family artifacts."""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jinja2 import Environment
from tokenizers import Tokenizer


def fixtures(family):
    plain = [{"role": "user", "content": "First question."},
             {"role": "assistant", "content": "<think>THOUGHT_MARKER</think>Answer."}]
    structured = [{"role": "user", "content": "First question."},
                  {"role": "assistant", "reasoning_content": "THOUGHT_MARKER", "content": "Answer."}]
    arguments = '{"path":"file.py"}' if family == "deepseek-r1" else {"path": "file.py"}
    tool = [{"role": "user", "content": "Read the file."},
            {"role": "assistant", "content": "<think>THOUGHT_MARKER</think>Calling now.", "tool_calls": [
                {"id": "call-one", "type": "function", "function": {"name": "read_file", "arguments": arguments}}]},
            {"role": "tool", "tool_call_id": "call-one", "content": "The file contents."}]
    follow = {"role": "user", "content": "Next question."}
    cases = []
    for name, messages in (("plain", plain), ("separate-field", structured), ("tool", tool)):
        cases.extend([(name + "-current", messages, {}), (name + "-next-user", messages + [follow], {})])
    if family == "nemotron-3-nano":
        for name, messages in (("plain", plain), ("tool", tool)):
            cases.append((name + "-next-user-keep", messages + [follow], {"truncate_history_thinking": False}))
    return deepcopy(cases)


def verify(artifacts):
    evidence = json.loads(Path(__file__).with_name("family-template-evidence.json").read_text())
    count = 0
    for model in evidence["models"]:
        folder = artifacts / model["family"]
        for artifact in model["artifacts"]:
            data = (folder / artifact["file"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                raise ValueError(f"Artifact differs from pin: {model['family']}/{artifact['file']}")
        config = json.loads((folder / "tokenizer_config.json").read_text())
        environment = Environment(trim_blocks=True, lstrip_blocks=True)
        environment.filters["tojson"] = lambda value: json.dumps(value, ensure_ascii=False)
        template = environment.from_string((folder / "chat_template.jinja").read_text())
        tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        special = {key: value["content"] if isinstance(value, dict) else value
                   for key, value in config.items() if key in ("bos_token", "eos_token", "pad_token", "unk_token")}
        expected = {case["case"]: case for case in model["constructed_checks"]}
        for name, messages, settings in fixtures(model["family"]):
            text = template.render(messages=messages, add_generation_prompt=True, **special, **settings)
            actual = {"case": name, "settings": settings, "thought_marker_retained": "THOUGHT_MARKER" in text,
                      "rendered_tokens": len(tokenizer.encode(text, add_special_tokens=False).ids),
                      "rendered_sha256": hashlib.sha256(text.encode()).hexdigest()}
            if actual != expected[name]:
                raise ValueError(f"Constructed rendering changed: {model['family']}/{name}")
            count += 1
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    print(f"Verified {verify(args.artifacts)} constructed family renderings.")
