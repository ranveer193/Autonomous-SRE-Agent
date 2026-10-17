# SRE agent prototype: Deep Agents + RAG + runbook skills, wired to the sre-playground

## Setup
    pip install -r requirements.txt
    cp .env.example .env      # GROQ_API_KEY (+ SRE_EVAL_DIR for the eval)
    python tests_offline.py   # parser sanity check, no cluster needed

Playground must be up: `python scripts/sre.py up`, traffic running (`traffic-start`), and Prometheus/Loki/Tempo
reachable on localhost 9090/3100/3200 (your kind-config.yaml already maps them).

## Run
    python main.py                                    # detects symptoms, investigates, asks approval before any change
    SRE_DRY_RUN=true python main.py "payment-service 5xx"   # writes only print what they would do

## Evaluate (injects a real fault, scores the agent)
    python eval/run_scenario.py payment-bad-config --auto-approve --runs 2
Reports MTTD, agent time, MTTR, whether RAG was hit, actions, and an LLM-judge verdict vs the scenario's ground_truth.
Run the same scenario twice: run 2 should hit RAG and be faster. Results land in eval/results/.

## Logging
Console shows each LLM decision, tool call + result, plan (todos), subagent delegation (indented) and approvals.
Full JSONL trace per run in `logs/`. `SRE_LOG_LEVEL=DEBUG` adds model reasoning and full tool output.

## Layout
    sre_agent/tools/{prometheus,loki,tempo,k8s}.py   tool box (read tools -> inspectors, write tools -> remediator only)
    sre_agent/detect.py                              symptom detector = alert source AND "is it fixed?" check
    sre_agent/rag.py                                 incident memory (Chroma), saved only after verified outcome
    skills/*/SKILL.md                                7 runbooks (triage, dependency-failure, slow-dependency, bad-rollout, ...)
    eval/run_scenario.py                             fault injection + scoring harness

## Design notes
- Model: set `SRE_PROVIDER` to `groq` (ChatGroq, openai/gpt-oss-120b), `gemini` (ChatGoogleGenerativeAI) or `huggingface` (ChatHuggingFace + HuggingFaceEndpoint); `SRE_MODEL` overrides the model. Built in `sre_agent/llm.py`, shared by agent and eval judge.
- Agents need reliable tool calling: for HuggingFace pick an instruct model your endpoint/provider serves with tool support.
- The agent has NO access to /admin/faults, faultctl or scenario files (they are ground truth).
- Write tools are limited to api-gateway, order-service, user-service, payment-service, postgres, redis.
- In-process faults (db_latency, memory leak, errors) are cleared by a pod restart, so recovery alone is a weak score.
  That is why the judge also checks root cause and the distractor (payment-db-slow: do not blame payment-service).
- Verify against your deepagents version: skills path, subagent-level interrupt_on, `__interrupt__` resume shape.

## Console and failure tests

```bash
pip install -r requirements.txt
python api.py                      # http://127.0.0.1:8765 (change with API_PORT in .env)  (localhost only, no auth)
python tests_guards.py             # offline safety checks, no cluster or LLM needed
python eval/run_suite.py --auto-approve --scenarios all --runs 2 --negative --stale --fresh-rag
```

Console tabs: cluster now, investigate (live trace and in-browser approvals), past traces, incident memory, failure tests (run the suite and read reports).
The suite writes `reports/<timestamp>/report.md` and `report.json`. Case kinds: guard (offline), scenario (fault injected, agent must fix),
healthy (no fault, agent must change nothing), stale (fault already recovered when the agent starts, must change nothing).
Use a sandbox cluster only: the suite injects faults and approves every change. `--reset-cmd` restores the playground before each case.

## Pointing the agent at a different system

All system-specific names live in **`system.yaml`** (path override: `SRE_SYSTEM_FILE`). Copy it and edit:
`namespace`, `writable` (deployments the agent may change; empty = it can never change anything), `topology` (free text, optional),
`selector_label`, `thresholds`, `metrics` (PromQL selectors for requests, errors and latency, plus optional dependency metrics) and `loki` (label and field names).
With no file the agent uses generic defaults and derives the topology itself. `python tests_guards.py` fails if a system-specific name
appears anywhere in the agent code, prompts, skills or UI, so run it after any edit. `eval/` is the only part tied to the sample playground.

Assumptions that remain: Kubernetes plus kube-state-metrics and cAdvisor, Prometheus, Loki (Tempo optional), pods named `<deployment>-<hash>`, and `kubectl` on the PATH.

## Console: multi-agent view, tests, safety (latest changes)

- **Windows fix**: all console and file output is UTF-8 (`sre_agent/__init__.py`, logger, suite subprocess). Emoji in log lines can no longer crash a run, and a logging failure never stops the agent.
- **Agents panel** (Investigate and Past traces): one lane per delegation on a shared time axis (overlap = parallel work), plus a table with status, time, model/tool calls, tokens and errors per agent. Parallel delegations render side by side; a failed subagent shows as failed instead of "working" forever.
- Every trace event now carries the agent that produced it (`agent`, `grp`), so the Raw log can be filtered by agent. Old traces still replay.
- **Failure tests**: live case list with a progress bar, Stop button, reports sorted worst first, each case expands to details and a "Replay what the agents did" link to its trace.
- **No leakage**: secrets (API keys, tokens, passwords) are redacted before anything is written to the console or `logs/`; the console only answers requests addressed to localhost and refuses cross-site posts; trace and report names cannot traverse paths; `python tests_guards.py` still fails if a system-specific name appears in agent code, prompts, skills, API or UI.

```bash
python tests_guards.py     # safety + no-system-names checks
node tests_ui.js           # trace model and rendering, including parallel subagents and HTML escaping
```

## Phase 2: metrics and technique comparison

Every scenario run now records five metrics (`sre_agent/metrics.py`, stored under `metrics` in `eval/results/*.json` and in each suite case):
**MTTR** (detection to healthy; empty when the harness had to recover the fault), **root-cause accuracy** and **distractor-blame rate** (judge verdicts),
**tokens per incident** (input+output over every model call, judge excluded; input is re-sent context, so compare arms with each other, not with a bill) and
**tool calls** (real tools only; subagent hand-offs are counted separately as `delegations`). Tokens and tool calls are split **main vs sub** (orchestrator vs all spawned subagents),
so a cheaper `SRE_SUBAGENT_MODEL` shows up directly. A crashed agent or a failed judge gives "no data", never a zero or a wrong answer.

```bash
python eval/run_suite.py --auto-approve --scenarios all --compare                  # 4 arms x every scenario x 2 runs
python eval/run_suite.py --auto-approve --scenarios payment-bad-config --compare --arms llm-only,memory --runs 3
python eval/run_suite.py --auto-approve --scenarios all --features localizer,memory   # normal suite with chosen techniques
python tests_phase2.py
```

Arms are cumulative: `llm-only`, `localizer` (+localizer), `falsify` (+falsify), `memory` (+memory). Memory is cleared at the start of each (scenario, arm) cell and
never between its runs, so in the memory arm run 1 is cold and run 2 can use what run 1 stored; the report adds a `memory (warm)` row (run 2 onward).
`reports/<ts>/report.md|json` get a comparison table (`comparison` key in the JSON). Compare mode runs scenarios only (no healthy/stale cases).

Console: the Investigate tab has Localizer / Falsify / Memory checkboxes (defaults from `SRE_LOCALIZER` / `SRE_FALSIFY` / `SRE_MEMORY`) and shows the main and subagent models.
Failure tests has the same checkboxes for a normal suite, and a **Compare techniques** switch with one checkbox per arm; the report view shows the table (best value in bold) and each case lists its arm and main/sub split.
With few runs per cell the means are noisy: read the table as a direction, and repeat before concluding.

## Failure tests: simpler console and guaranteed recovery

The **Failure tests** tab is now three steps: pick faults, set runs (advanced options are folded away), run. While it runs you see the current step, a progress bar and one row per test.
Results open as a verdict (pass rate), counts, and a worst-first list; each test shows who restored the system: **Agent fixed it**, **Auto-restored** (the harness had to) or **Still broken**.

Recovery is guaranteed, not best-effort:
- `eval/run_scenario.py` marks the fault in `eval/.active_fault` *before* injecting and recovers it in a `finally` block on any error (failed inject, agent crash, judge crash, Ctrl+C). It never recovers a fault the agent already fixed (a rollout undo would undo the agent's rollback).
- `eval/run_suite.py` turns a crash inside one case into a FAIL for that case and carries on; it also recovers any leftover fault at start and exit, and on SIGTERM.
- **Stop** in the console recovers the active fault even when the suite is killed hard (Windows). A red banner with **Recover now** (`POST /api/faults/recover`) appears if a fault is ever left active.
- If the system is still unhealthy after recovery, the case says so, and the next case is skipped as "baseline not clean" instead of producing misleading results.

## Model check (Cluster now page)

The **Models** panel on *Cluster now* checks the main agent model and the subagent model (`SRE_MODEL` / `SRE_SUBAGENT_MODEL`). Press **Check models**: each distinct model gets two tiny calls, a plain reply
(reachable, reply time) and a tool call (the agents cannot work without tool calling). Status: **Ready**, **Limited** (answers but tool calling fails) or **Not working** (with the error and a fix hint:
bad key, wrong model name, rate limit, timeout, missing package). If main and subagents use the same model it is checked once. Before checking, a row already says **No API key** when the key is missing.
**Test another model before using it** tries any provider/model without changing `.env`; if it is Ready, set it in `.env` and restart. Nothing is checked automatically, so no tokens are spent until you press the button.

```bash
python tests_models.py     # offline tests for the check (fake model, no keys)
```

## Step-by-step view of every test

Each test in **Failure tests** (live and in saved reports) opens to a timeline: **check** (is the system healthy before we start) → **inject** → **detect** → **alert** → **agent** (with an *Open trace* button for the live agent trace)
→ **verify** → **recover** (only if the agent did not fix it) → **judge** → **result**, each with the time since the test started and a green/amber/red mark. The running test is open and follows along; your open/closed choices survive refreshes.
The harness prints one `@@ {json}` line per step (`rs.stage()` in `eval/run_scenario.py`); the suite stores them under `steps` in `report.json`.

**"baseline not clean" / SKIP**: before injecting, the suite requires the system to be healthy, otherwise the result would be meaningless. The test is skipped and the timeline lists exactly what was already broken
(for example a 5xx rate from an earlier fault) with what to do: fix it, give a *Reset command*, or press **Recover now**. After one unclean baseline later checks wait 30 s instead of 150 s. If the metrics source itself
cannot be read (a `detector error`), the suite stops at once with a `setup` failure that names the problem instead of skipping every test.

## Fixed: "baseline not clean" on a healthy cluster

Cause: `tests_guards.py` swaps in fake kubectl and fake health functions, and `eval/run_suite.py` runs those checks in the same process right before the real agent. The checks never put the real functions back, so afterwards
the health check kept returning a fake `payment-service: 5xx 40%` + `detector error: x`, every baseline looked broken, and (had a test started) the agent would have used a fake kubectl, a health check that always fails, a forced `DRY_RUN=false`
and an empty writable list. `run()` now restores everything it touched and adds the check "guard checks leave no test patches behind", so a regression shows up in the report's guard list.

## Phase 3: run isolation, memory correctness, no leaks

See `CHANGES.md`. Short version: the harness clears the previous run's ReplicaSets and events (`--keep-history` to skip) and passes the alert time as the
incident onset; the incident window no longer merges back-to-back incidents; the agent's file sandbox is rebuilt every run; the localizer baseline is the
quietest value before onset and rollouts are listed neutrally for every deployment; memory credits only the last change before RESOLVED, recalls by exact
symptom overlap, reports `rag_hit` correctly and uses one absolute store. `python tests_phase3.py` covers all of it, including a leak check.

## Phase 4: cost techniques (switchable)

`preflight`, `cluster_logs`, `lean_tools`, `compact_reports`, `budget`: each is a checkbox in the console (Investigate and Failure tests), an env var
(`SRE_LEAN_TOOLS=1` ...) and a `--features` entry; cumulative arms `cost-base` ... `cost-budget` compare them with `--compare --arms`. All off by default.
The report adds input tokens, cached share, LLM calls and `budget blocked`. See `CHANGES.md` (Phase 4) for what each does, what was measured, and what was
deliberately not built. `python tests_phase4.py` checks every switch offline.

## Tuning: the numbers behind the techniques (per run)

A technique switch says *which* technique is on; tuning says *how much*. Seven numbers can be set per run instead of being edited in code:
`tool_budget` (cap on investigation tool calls, default 30), `inspector_calls` (8), `report_words` (150), `log_patterns` (12), `log_fetch` (100),
`preflight_chars` (3200), `recursion_limit` (120). Each is used only when its technique is on (except the last two, which always apply).

- **Console:** Failure tests, step 2, Advanced options, "Numbers behind the techniques". Blank = default. The same values apply to every test and, in compare mode, every technique set. A wrong value is refused in the form with its allowed range.
- **CLI:** `python eval/run_suite.py ... --tuning tool_budget=20,report_words=100` (also `run_scenario.py` and `main.py`).
- **Env:** `SRE_TOOL_BUDGET`, `SRE_REPORT_WORDS`, ... (see `.env.example`). Precedence: console/CLI > env > built-in default.
- **Recorded:** the numbers are in every result (`tuning`), in the trace's first line, in `report.json` (`meta.tuning`) and in the report header when any differ from the defaults.

At the defaults nothing changes: every prompt is identical to before. `python tests_tuning.py` checks it offline.


## Leakage and portability audit (latest)

- **Runbooks no longer carry scenario answers**: removed the copied error string, hostname hint, hard-coded 512 MiB, "DBA", `/health` vs `/ready` and "unknown ids" wording. `tests_guards.py` now bans these phrases and fails if agent code or skills mention `ground_truth` or `faultctl`.
- **Eval cannot touch live memory**: eval scripts default `SRE_MEMORY_DIR` to `data/chroma_eval`. Before this, the console's failure tests (fresh-RAG on by default) wiped the production incident memory.
- **Judge model is separate**: `SRE_JUDGE_PROVIDER` / `SRE_JUDGE_MODEL`. Unset it still grades with the agent's model, which biases scores.
- **One eval adapter**: `eval/target.py` (`SRE_EVAL_DIR`, `SRE_SCENARIO_DIR`, `SRE_FAULT_CMD`); `SRE_KUBECTL` sets the kubectl command; write-tool ceilings are `limits` in `system.yaml`.
