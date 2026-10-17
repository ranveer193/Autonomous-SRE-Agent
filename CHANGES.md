# Phase 3 changes: run isolation, memory correctness, no leaks to the agent

Everything here sits on top of the version you uploaded (which already had `incident.py`, the evidence-board localizer, tool-recorded memory and the
restart-at-0-replicas guard). Tests: `python tests_phase3.py` (new, 29 checks), `python tests_phase1.py` (rewritten for the evidence board),
`python tests_guards.py`. Not run against a live cluster or live Prometheus: the PromQL subquery in the localizer baseline and `tidy_history`
need one real run to confirm.

## A. Previous run leaking into the next one
| # | File | Change |
|---|---|---|
| 1 | `eval/run_scenario.py` | `tidy_history()`: before injecting, deletes the previous run's 0-replica ReplicaSets of the writable deployments (rollout history, `rollout restart` revisions) and the namespace events. Only when the system is healthy. Skip with `--keep-history`. Knows nothing about any scenario. |
| 2 | `sre_agent/detect.py` | `contiguous_start`: a run of 2 HEALTHY samples (30s) now ends an incident; before, it was tolerated like missing samples, so back-to-back runs merged and the old fault, its fix and its logs entered the window. One healthy sample and short no-traffic gaps are still forgiven. |
| 3 | `sre_agent/incident.py`, `sre_agent/runner.py`, `eval/run_scenario.py` | `run_alert(..., onset=)` and `incident.begin(onset_ts=)`: when the alert time is known it is the window start (what an alert rule's `startsAt` gives). The harness passes its detection time. Without it the onset is still estimated from metrics. |
| 4 | `sre_agent/localizer.py` | "before" = the LOWEST value in the 5 minutes before onset (`min_over_time` subquery), not an average, so the previous incident cannot show up as the healthy baseline. |
| 5 | `sre_agent/agent.py` | `_sandbox()` wipes and rebuilds the agent's file folder every run. Before, anything the agent wrote there, and any deleted runbook, survived into the next run. |

## B. Localizer is evidence only
| 6 | `sre_agent/localizer.py` | Rollouts are one neutral line listing EVERY deployment relative to onset, no longer attached to the components that look unhealthy (that pairing was the lure). |
| 7 | `sre_agent/agent.py` | Localizer prompt no longer says to look for "a change made before the symptoms began". It now says: explain each fact, confirm with a second signal type. |

## C. Memory
| 8 | `sre_agent/incident.py` | `record_verify`: on RESOLVED only the LAST change since the previous verify is credited; earlier ones are "unclear" (not stored as the fix, not stored as failed). On NOT_RESOLVED all are marked failed. |
| 9 | `sre_agent/rag.py`, `sre_agent/runner.py` | `rag_hit` comes from a flag set inside `search_incidents` when it returns a past incident. The old string check counted "No past incident with a similar symptom signature" as a hit. |
| 10 | `sre_agent/rag.py` | Recall = exact Jaccard over all stored incidents (no embedding ranking and top-12 cut-off). The embedder is only used when saving, so the store stays compatible. |
| 11 | `sre_agent/rag.py` | Recall text says repeats of one fault are not independent confirmations. |
| 12 | `sre_agent/rag.py` | Store path is absolute (`SRE_MEMORY_DIR` overrides), so console, CLI and harness share one store. |

## D. Tools
| 13 | `sre_agent/tools/k8s.py` | `scale_deployment` to the replica count it already has returns `NO_OP` and is not counted as a failed fix (same as restart at 0 replicas). |

## E. Tests
`tests_phase1.py` still called the removed `L.rank`; rewritten for `build`/`render`. `tests_phase3.py` covers every item above, including a leak check.

## What can reach the agent, and what I checked
- Agent-visible: system prompt, `skills/`, tool docstrings and outputs, the first message (alert text from the detector, snapshot, evidence board), recalled incidents.
- `tests_phase3.py` fails if anything under `sre_agent/` (except `metrics.py`, scoring code) or `skills/` mentions fault injection, ground truth, `faultctl`, scenarios, distractors or the harness, and if agent-side code imports `metrics.py`. `tests_guards.py` still bans system names.
- New data given to the agent: the alert time only (equal to what a production alert carries). No scenario name, ground truth or fault time.
- `tidy_history()` and `--keep-history` live in `eval/`, outside the agent.

## Still true, not changed (decide on purpose)
- **Memory in the eval is curated by the judge.** `run_scenario.py` confirms or deletes saved incidents using the judge, which sees the ground truth. Warm runs therefore see an oracle-filtered store, which production never has. Compare memory arms with `SRE_KEEP_WRONG_SAVES=1` to measure poisoning, and say which mode a result used.
- **Judge leniency.** It scored runs "correct" that named "postgres is slow" or only escalated although a tool could fix it. Confirmed memory is only as good as the judge.
- `SRE_MEMORY_CONFIRMED_ONLY` stays off by default.

# Phase 4: cost techniques, each behind a switch

All five are OFF by default (existing behaviour is unchanged until one is turned on). Tests: `python tests_phase4.py` (62 checks; no cluster, no LLM).
Not run against a live cluster or a real model: the token numbers below are char/4 estimates of what the model is sent, measured with a fake model.

| switch (`SRE_<NAME>`) | what it does | measured offline | risk to the answer |
|---|---|---|---|
| `lean_tools` | model is not shown file tools it never uses (inspectors: all 7; orchestrator keeps `ls` + `read_file`), todo tool description shortened | orchestrator static request ~6,980 -> ~4,440 tokens per call; log-inspector ~2,690 -> ~550 per call | very low: same prompts, data tools and approval gate |
| `cluster_logs` | `search_logs` returns message templates with counts + one example `trace_id`, fetches 100 lines instead of 25 | 60 raw lines -> 1 line (>10x smaller in the test) | low: distinct error types/services never merge; counts say they cover only the newest lines |
| `preflight` | code collects log patterns + Warning events before the model starts; model skips `current_problems` at triage and passes known facts to inspectors | saves round trips (each re-sends the context): measure with the arms | medium: more evidence up front. Neutral, unranked, counts as ONE logs + ONE cluster signal |
| `compact_reports` | inspectors answer ANSWER / EVIDENCE / RULED OUT / CONFIDENCE (max 150 words); remediator untouched; no "suspect" field | shorter subagent output and a smaller orchestrator history | medium: a too-short report could drop evidence. Check root-cause accuracy |
| `budget` | enforced cap (default 30, `SRE_TOOL_BUDGET`) on investigation READ calls per incident + "stop when two signals agree" | n/a (a ceiling) | medium: if the cap bites too early accuracy drops. `budget_blocked` in every result shows whether it ever bit |

Writes, `verify_resolved`, `current_problems`, memory tools, the plan and subagent hand-offs are never counted or blocked by the budget.

New measurements per run (and per arm): `llm_calls`, `tokens_in`, `tokens_cached` (provider cache hits; 0 means the provider reported none), `budget_blocked`.
Cumulative cost arms: `cost-base` (localizer+falsify+memory) -> `cost-lean` -> `cost-logs` -> `cost-preflight` -> `cost-reports` -> `cost-budget`.

```bash
python eval/run_suite.py --auto-approve --scenarios all --compare --arms cost-base,cost-lean,cost-logs,cost-preflight,cost-reports,cost-budget --runs 3
python eval/run_scenario.py <scenario> --auto-approve --features localizer,falsify,memory,lean_tools      # one technique on its own
SRE_LEAN_TOOLS=1 python main.py "..."                                                                    # or from env
```
A technique "does not degrade the agent" only if, against `cost-base`, root-cause accuracy and distractor-blame rate stay the same while tokens/LLM calls fall.

## Looked at and NOT built (and why)
- **Evidence handles / offload**: deepagents offloads results above 20,000 tokens; your tool results are far below that (<= ~1.5k tokens), so it never fires, and a handle adds a tool definition plus a round trip.
- **Delta views**: the localizer already shows "now vs before" per component. Nothing left to add.
- **Evidence ledger instead of history**: needs a custom state graph and hides earlier reasoning from the orchestrator. `compact_reports` shrinks the biggest part of that history first; revisit with the per-call token curve from the new metrics.
- **Prompt caching**: deepagents already enables Anthropic/Bedrock/Fireworks caching (no-op elsewhere). Groq, Gemini and OpenRouter cache a stable prefix on their side; nothing to switch. The prefix is stable, and `cached_pct` now shows what the provider actually served from cache.
- **Cost routing**: already available (`SRE_SUBAGENT_MODEL`); the main/sub token split shows its effect. Combine with any arm.

## Found, not changed
- `tools/util.py: bounded()` is defined but applied to no tool, so `SRE_TOOL_MAX_CHARS` has no effect today (k8s output is capped at 6,000 chars by `_k`; metrics/log tools are bounded by their own limits).

# Phase 4b: tunables settable per run (console, CLI, env)

The tool-call cap and a few other numbers were constants (or one env var), so trying another value meant editing code and the result did not say which
value produced it. New `sre_agent/tuning.py`: a small `Tuning` value passed next to `Features` through `run_alert`, the API (`tuning` on `/api/runs` and
`/api/suite`), `--tuning` on `run_suite.py` / `run_scenario.py` / `main.py`, and a block in the console's Failure tests > Advanced options.

| number | default | used when | where it acts |
|---|---|---|---|
| `tool_budget` | 30 | `budget` | `budget.cap()` (was `SRE_TOOL_BUDGET` only) |
| `inspector_calls` | 8 | always | "at most N tool calls" in each inspector prompt (a request; the enforced cap is `tool_budget`) |
| `report_words` | 150 | `compact_reports` | word limit in the compact report format |
| `log_patterns` | 12 | `cluster_logs` | patterns shown per `search_logs` |
| `log_fetch` | 100 | `cluster_logs` | lines fetched per `search_logs` before clustering |
| `preflight_chars` | 3200 | `preflight` | size cap of the pre-flight block |
| `recursion_limit` | 120 | always | LangGraph step limit (was hard-coded in `runner.py`) |

- **Defaults change nothing.** Checked by dumping every system and subagent prompt for all 128 feature combinations before and after: identical.
- **Strict when typed, lenient in env.** The console, API and CLI refuse a non-number or out-of-range value with the field name and range (the API answers 400 before a suite starts). Env keeps the old forgiving behaviour (junk = default).
- **Precedence:** console/CLI > env > default. A value typed in the console that equals the built-in default still wins over a different env value (the suite subprocess is sent every value).
- **Recorded:** `tuning` in each run result and case, in the trace's first line, `meta.tuning` / `meta.tuning_changed` in `report.json`, and a header line in `report.md` when anything is non-default. Compare mode applies one tuning to all arms, so to compare two budgets run two suites.
- Tests: `python tests_tuning.py` (64 checks) and 16 new checks in `node tests_ui.js`. The form was also driven in a simulated DOM (fields, typed values sent, bad values blocked, reset, compare mode), but not in a real browser.
- Not exposed: `SRE_TOOL_MAX_CHARS` (still unwired, see "Found, not changed" in Phase 4). A knob that does nothing would mislead.
- Not added to the Investigate tab: the API already accepts `tuning` for single runs; only the suite form has the inputs, as asked.
