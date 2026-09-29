# Dynamo baseline record

Fixed-request replay. Recorded counts are epochs; completed counts are workflows. Server behavior still needs trace verification.

Study state: finished. Hint application: unverified.

| Condition | Recorded | Completed |
| --- | ---: | ---: |
| off | 5 | 20 |
| on | 5 | 20 |

Paired values are on minus off. Tasks with observed pairs receive equal weight. Missing pairs remain listed in `report.json`.

| Measure | Pairs | Mean difference |
| --- | ---: | ---: |
| mean_capped_completion_ms | 5 | 39.162 |
| post_tool_first_output_p90_ms | 5 | -34.140 |
| client_input_tokens | 5 | 0.000 |
| client_output_tokens | 5 | -13.800 |
| output_limit_hits | 5 | 0.000 |
| server_prefill_seconds | 0 | unknown |
| server_decode_seconds | 0 | unknown |
| server_generation_tokens | 0 | unknown |
| server_prompt_tokens | 0 | unknown |
| server_requests_finished | 0 | unknown |
| sampled_kv_fraction_seconds | 0 | unknown |

The server cache carries between epochs or trials. These differences have no confidence interval. Client timing includes network and parsing. Server counters cover all traffic, including preparation and cleanup. A model response completing says nothing about coding-task correctness in replay.
