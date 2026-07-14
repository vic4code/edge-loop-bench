# Metrics contract

## Effectiveness metrics

| Metric | Definition |
| --- | --- |
| Objective success | Final patch satisfies hidden evaluation and path rules |
| Success rate | Successful runs divided by valid runs in an arm |
| Success-budget AUC | Area under the success curve across frozen budget tiers |
| Prompt tokens | Sum of all rendered model-input tokens at request time |
| Completion tokens | Sum of all model-generated tokens |
| Tokens to first success | Cumulative tokens when the first objectively valid patch existed |
| Public-pass hidden-fail | Public tests pass but hidden evaluation fails |
| Repeated failure | Same normalized failure signature occurs again in a run |
| Wasted-after-success | Resources used after the first objectively valid patch existed |
| Unsafe patch | A prohibited path or evaluator artifact was modified |

Prompt-cache reuse changes physical prefill work but not logical prompt-token accounting.

## Verifier metrics

| Metric | Definition |
| --- | --- |
| False approval | Verifier approves an objectively failing patch |
| False rejection | Verifier rejects an objectively correct patch |
| Approval precision | Correct approvals divided by all approvals |
| Approval recall | Correct approvals divided by all objectively correct proposals |
| Useful rejection | A rejection followed by a successful maker revision |
| Prevention efficiency | Verifier tokens per prevented false success |

`ESCALATE` is reported independently and never silently mapped to approval.

## Serving metrics

| Metric | Unit | Measurement boundary |
| --- | --- | --- |
| Time to first token | milliseconds | Request accepted to first decoded token observed |
| Prompt throughput | tokens/second | Prompt evaluation tokens divided by prompt-evaluation duration |
| Decode throughput | tokens/second | Generated tokens divided by decode duration |
| End-to-end latency | milliseconds | Request submission to completed response |
| Model load time | milliseconds | Runtime-reported load or controlled process-start boundary |
| Peak memory | bytes | Collector and sampling interval must be stated |
| Swap delta | bytes | Host swap change during the measured window |
| Energy | joules | Collector, privileges, and sampling distortion must be stated |

Report p50 and p95 latency across repeated runs. Do not pool cold and warm requests.

## Convenience metrics

The scaffold computes these descriptive values over valid runs. `completed`, `budget_exhausted`, and `timeout` are valid task outcomes; `infrastructure_error` and `invalid` are excluded from the success denominator and reported separately.

```text
total_tokens = prompt_tokens + completion_tokens
success_per_1k_tokens = 1000 * successes / sum(total_tokens)
mean_wall_seconds = sum(wall_seconds) / run_count
```

`success_per_1k_tokens` is a screening metric, not a replacement for a paired success comparison. It can reward a strategy that quits cheaply without solving hard tasks. Energy means always include an `energy_observations` count because collection can be incomplete.

## Paired deltas

Compare two strategies only on shared valid `(experiment, task, budget, seed)` units declared by the same manifest. For a pair A and B:

```text
success_delta_pp = 100 * mean(success_B - success_A)
token_delta = mean(total_tokens_B - total_tokens_A)
wall_delta_seconds = mean(wall_B - wall_A)
```

The summary command reports expected, observed, missing, and invalid run coverage. It rejects missing declared runs by default; `--allow-incomplete` permits an explicitly partial report. Missing pairs are never imputed.

Every text and JSON summary reports a manifest binding for each experiment identifier. An entirely unbound exploratory data set is labeled as such. Mixing bound and unbound rows, or two distinct manifest digests, under the same experiment identifier is rejected instead of silently pooling them.

## Invalid measurements

Reject a record when:

- a required identifier is empty;
- a run identity is not declared by the supplied experiment manifest;
- the record's manifest SHA-256 does not match the supplied manifest bytes;
- a token, call, or public-test counter exceeds its manifest budget;
- `max_call_context_tokens` is absent or exceeds the per-call context budget;
- a deployment run exceeds its wall-time or energy budget;
- a deployment energy budget exists but `energy_joules` is absent;
- tokens, calls, or durations are negative;
- a number is NaN or infinite;
- a numeric aggregate cannot be represented as a finite summary value;
- the same run key appears more than once;
- success is not a JSON boolean;
- energy is negative;
- a non-completed status lacks a failure reason or claims objective success.

`run_status` is one of `completed`, `budget_exhausted`, `timeout`, `infrastructure_error`, or `invalid`. Timeouts and budget exhaustion are valid objective failures when recorded explicitly; infrastructure crashes and invalid runs are excluded from the success denominator and reported separately.

`max_call_context_tokens` is the largest `input_tokens + generated_tokens` context observed in any single model call. Record zero when no model call was issued. For nonzero calls, the aggregate token total must be at least this maximum and no greater than `model_calls * max_call_context_tokens`; zero calls require zero prompt, completion, and maximum-context tokens. The field is required whenever results are validated against an effectiveness or deployment manifest so the per-call cap is enforceable rather than aspirational.
