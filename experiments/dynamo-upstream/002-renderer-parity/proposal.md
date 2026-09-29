# Argument-format compatibility proposal

First rerun the R1 fixture against the renderer version selected by current Dynamo. If it passes, record the fixing version and stop. If it fails, reproduce the same request through the actual selected frontend path before describing server impact.

The renderer needs an explicit contract for the selected template's argument representation. The current 5.1.0 heuristic recognizes templates that test `arguments is string`, but a template can require strings without testing their type. R1's string concatenation demonstrates that case.

A small fix can add a template-specific JSON-string mode for the pinned R1 template, preserving the caller's original argument bytes. Keep the existing object mode for templates such as Nemotron's. Prefer a renderer configuration or existing capability over inferring a universal rule from another source-text substring. If only an exact-template allowlist is acceptable upstream, document its scope and leave custom templates configurable.

Do not disable normalization globally: the Nemotron fixture requires objects. Do not modify the official R1 template in the test to accept maps; that would change the reference being checked. Do not reserialize a string merely to recover its original type, because whitespace, numeric spelling and key order can change the prompt.

The regression suite should check R1 string concatenation, Qwen's string-aware branch, Nemotron's object iteration, and selected default versus tool-use variants. Assert exact rendered bytes for valid arguments, including whitespace and large numeric values. Add malformed-argument and empty-argument cases under the existing validation contract; avoid inventing replacement arguments. Run the same cases through ordinary and speculative paths if the fix changes both.

The existing 44-case CPU comparison supplies valid-input controls. It tests the standalone formatter with explicit settings. Backend selection, native formatters, tokenizer implementations and GPU cache reuse remain separate integration checks.
