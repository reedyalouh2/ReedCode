# Dynamo baseline record

Harbor pilot. Recorded counts are trials; completed counts are verifier passes.

Study state: finished. Hint application: unverified.

| Condition | Recorded | Completed |
| --- | ---: | ---: |
| off | 5 | 5 |
| on | 5 | 5 |

Paired values are on minus off. Tasks with observed pairs receive equal weight. Missing pairs remain listed in `report.json`.

| Measure | Pairs | Mean difference |
| --- | ---: | ---: |
| reward | 5 | 0.000 |
| input_tokens | 5 | -1581.200 |
| model_latency_ms | 5 | -312.916 |
| model_calls | 5 | -0.400 |
| tool_calls | 5 | -0.400 |
| output_limit_hit | 5 | 0.000 |

The server cache carries between epochs or trials. These differences have no confidence interval. Client timing includes network and parsing. Server counters cover all traffic, including preparation and cleanup. A model response completing says nothing about coding-task correctness in replay.
