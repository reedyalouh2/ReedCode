# Six concurrent coding sessions

I ran six ReedCode sessions against one Dynamo worker, each with a repository task and two follow-up messages. The 64 model calls used 447,919 input tokens and reused **85.0%** from cache. Every input matched Qwen's official chat template. There was no unexplained cache shortfall against earlier completed requests.

## Serving results

| Measurement | Result |
| --- | ---: |
| Model calls | 64 |
| Tool calls | 64 |
| Follow-up user requests | 12 |
| Input tokens | 447,919 |
| Cached input tokens | 380,512 (85.0%) |
| Generated tokens | 90,665 |
| Official-template matches | 64/64 |
| Requests below the completed-history cache reference | 0 |
| Engine preemptions | 0 |
| Output-limit hits | 1 |

For 62 calls, cached tokens exactly matched the available block-aligned prefix from completed requests. Two calls each reused another 368 tokens while other sessions were running. The reference excludes unfinished requests.

| Request phase | Calls | Input tokens | Cached tokens |
| --- | ---: | ---: | ---: |
| Initial | 6 | 4,585 | 1,840 |
| Within a user turn | 46 | 377,045 | 343,056 |
| First call after a follow-up | 12 | 66,289 | 35,616 |

All 12 follow-ups preserved the earlier message objects. Their rendered prefixes changed because Qwen's template removes earlier assistant reasoning when a new user message arrives. Dynamo matched that template in every case. The KV capture had 5,243 consecutive batches and no block-removal or cache-clear events.

## Coding outcomes

The agents did poorly on the tasks. All five held-out suites that ran failed. The Requests agent replaced its test file, preventing the held-out patch from applying; 13 of its remaining 18 tests failed.

| Task | Model calls | Peak input tokens | Held-out result |
| --- | ---: | ---: | --- |
| Requests | 6 | 10,070 | Patch conflict |
| Flask | 6 | 5,836 | 1 failed, 59 passed |
| pytest | 12 | 9,754 | 1 failed, 88 passed, 1 xfailed |
| Sphinx | 15 | 11,308 | 2 failed, 7 passed |
| SymPy | 11 | 20,504 | 1 failed, 10 passed |
| Django | 14 | 5,005 | 1 failed, 9 passed |

The trajectories exposed two harness problems: 17 file calls rejected valid `/workspace/...` paths, and five responses without visible content or parsed tools were counted as completed turns. I corrected both in the capture harness. CPU checks covered 47 Docker file-tool invocations and passed 17 tests. The GPU results above use the earlier harness.

## Setup and data

One A100-SXM4-80GB served Qwen3-8B in BF16, with thinking enabled, prefix caching, 16-token blocks and a 32,768-token context. The worker used Dynamo 1.5.0 and vLLM 0.28.0; the native frontend used revision `6e4221b` in an unoptimized build. [Exact settings and source hashes](manifest.json), [request metrics](requests.csv) and [task commits and outcomes](tasks.json) are included.

Check the published table arithmetic from the repository root:

```sh
python3 experiments/dynamo-concurrent-20261007/verify.py
```

This validates the saved metrics and outcomes. It makes no model requests.

## Limitations

This is one cohort with ample cache capacity, a small model and unsuccessful coding attempts. It does not test eviction pressure or establish productive-agent throughput. The debug frontend and active capture limit latency conclusions. The task checks cover focused regressions; they are not an official SWE-bench score. The published files contain derived metrics rather than the full capture.
