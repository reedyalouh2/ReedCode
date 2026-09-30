# Local draft: DeepSeek-R1 historical tool calls fail in dynamo-renderer 5.1.0

Draft only. Current renderer and full-server behavior need checking before submission.

I have a small CPU reproduction using the published `dynamo-renderer=5.1.0` crate and DeepSeek-R1's official tokenizer config at revision `56d4cbbb4d29f4355bab4b9a39ccb717a14ad5ad`.

An assistant message with a historical tool call, followed by its tool result, renders successfully with the shipped template in Python. Passing the same OpenAI wire arguments through `PromptFormatter::from_parts` fails with:

```text
invalid operation: tried to use + operator on unsupported types string and map (in default:1)
```

The R1 template concatenates `tool['function']['arguments']` as a string. The renderer converts JSON strings into maps unless the selected template includes its recognized `arguments is string` branch. This template uses concatenation without that branch. The failure occurs with and without active tool schemas, and before or after a new user message.

The [reproduction instructions](README.md#reproduce) and [recorded cases](results.json) include the exact config, dependency and source hashes. The Qwen and Nemotron cases are controls for preserving their existing argument handling. All messages are authored fixtures. No model, GPU or full frontend was run.

This overlaps the argument-format work in Dynamo #12109, #12204 and #12332. The last of those also discusses a native DeepSeek V3.2 regression. This example concerns R1's Jinja path. I have not established whether the current renderer or a full-server preprocessor already handles it.

The proposed regression fixture checks R1's argument handling while keeping the Qwen and Nemotron cases unchanged.
