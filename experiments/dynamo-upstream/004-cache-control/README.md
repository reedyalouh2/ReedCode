# Cache-control documentation check

Read-only triage, September 28, 2026. Inspected Dynamo `v1.5.0` and `main` at `59ac36782c0f9662f1ab6123dce1559d1a3f377a`. No server request or GPU test was run.

The claimed contradiction is already resolved in these versions. The `nvext` reference no longer documents TTL pinning, and the agentic-inference article says `nvext.cache_control` is unsupported. Older v1.0.x pages still describe the former experimental feature. The exact page version matters.

## Versioned evidence

| Evidence | What it establishes |
| --- | --- |
| [v1.5.0 `nvext` reference](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/docs/fern/pages/developer-guide/additional-resources/nvidia-request-extensions-nvext.md) | No `cache_control` field or cache-pinning section. |
| [Current-main `nvext` reference](https://github.com/ai-dynamo/dynamo/blob/59ac36782c0f9662f1ab6123dce1559d1a3f377a/docs/fern/pages/developer-guide/additional-resources/nvidia-request-extensions-nvext.md) | Same absence. `cache_salt` controls cache identity and isolation; it is a different field. |
| [v1.5.0 agentic-inference article](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/docs/fern/pages/blog/2026/agentic-inference-optimizations.mdx#L101) | Explicitly says the self-hosted `nvext.cache_control` pinning API is unsupported. The inspected current-main passage agrees. |
| [v1.0.0 SGLang guide](https://docs.nvidia.com/dynamo/v1.0.0/backends/sg-lang/agentic-workloads) | Describes experimental pinning, its flags, and required development branches. This is an older versioned page. |
| [v1.5.0 `NvExt` type](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/lib/llm/src/protocols/common/extensions.rs#L182-L253) | No `cache_control` member; the type uses `deny_unknown_fields`. The inspected current-main type has the same property. |

The article's March publication date does not freeze its content. Its later edits and the versioned source establish what the page says for this release.

## Chat and Anthropic requests

For the normal typed Chat Completions path, `nvext.cache_control` is an unknown extension field. The source indicates deserialization should reject it. This audit did not exercise an HTTP endpoint or measure its response status.

Anthropic `/v1/messages` has separate compatibility fields. Its request types can carry `cache_control` on supported content, and the unified request can preserve block annotations as metadata. The [v1.5.0 annotation collector](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/lib/llm/src/protocols/unified.rs#L215-L258) records block positions. Parsing or preserving that metadata does not establish a pinning operation.

## Existing upstream work

| Change | Status | Meaning |
| --- | --- | --- |
| [#6213](https://github.com/ai-dynamo/dynamo/pull/6213) | Merged February 27, 2026 | Added the experimental `nvext.cache_control` TTL pinning path. |
| [#6629](https://github.com/ai-dynamo/dynamo/pull/6629) | Merged March 2, 2026 | Extended Anthropic compatibility and originally connected its cache controls to that path. |
| [#7790](https://github.com/ai-dynamo/dynamo/pull/7790) | Merged April 2, 2026 | Removed the `nvext` field and router pinning plumbing, kept Anthropic compatibility parsing, and removed the pinning section from the `nvext` documentation. |
| [#10659](https://github.com/ai-dynamo/dynamo/pull/10659) | Merged June 12, 2026 | Updated the agentic-inference article to describe the supported surface. |
| [#11534](https://github.com/ai-dynamo/dynamo/pull/11534) | Closed, unmerged | Proposed a later normalized retention contract. Its existence does not establish released support. |

The final [#7790 diff](https://github.com/ai-dynamo/dynamo/pull/7790/files) directly removes the reference's old pinning claim. This is a known feature removal with documentation changes, rather than evidence of a new backend defect.

Four repository searches returned their full result sets: [`"nvext.cache_control"`](https://github.com/search?type=issues&q=repo%3Aai-dynamo%2Fdynamo%20%22nvext.cache_control%22) (7), [`"cache_control" pinning`](https://github.com/search?type=issues&q=repo%3Aai-dynamo%2Fdynamo%20%22cache_control%22%20pinning) (12), [`"cache pinning" docs`](https://github.com/search?type=issues&q=repo%3Aai-dynamo%2Fdynamo%20%22cache%20pinning%22%20docs) (26), and [`"cache_control" unsupported`](https://github.com/search?type=issues&q=repo%3Aai-dynamo%2Fdynamo%20%22cache_control%22%20unsupported) (5).

## A CPU check if needed

Use the actual versioned Rust types to deserialize a Chat request containing `nvext.cache_control`, and assert rejection. Separately deserialize an Anthropic request containing cache annotations, convert it through the real adapter, and inspect the resulting request and routing fields. The expected result is accepted compatibility metadata without a TTL pin directive. Run both cases with otherwise identical requests and with the frontend extensions enabled.

These checks would confirm schema and conversion behavior. They would not demonstrate cache retention. They have not been run here. The source and removal history are sufficient to keep this item out of the new-bug queue unless a specific current page still makes the old claim.

`evidence-index.json` records hashes and search coverage. `source-evidence.tar.gz` preserves the inspected source, documentation, and report metadata.
