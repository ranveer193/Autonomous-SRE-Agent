import shutil
from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import ModelRetryMiddleware, TodoListMiddleware
from langgraph.checkpoint.memory import MemorySaver

from . import budget
from .config import NAMESPACE, TOPOLOGY, METRICS
from .features import Features, from_env
from .tuning import Tuning, active as active_tuning
from .lean import LeanTools, ORCHESTRATOR_DROP, SUBAGENT_DROP
from .llm import get_llm, get_subagent_llm
from .rag import RAG_TOOLS
from .tools.k8s import K8S_READ_TOOLS, WRITE_TOOLS, VERIFY_TOOLS
from .tools.loki import LOKI_TOOLS
from .tools.prometheus import PROM_TOOLS, current_problems
from .tools.tempo import TEMPO_TOOLS

ROOT = Path(__file__).resolve().parent.parent
# The agent's file tools see ONLY a copy of the runbooks. Never point the backend at ROOT: that exposes logs/ (past
# runs), eval/ (judge + ground truth), data/ (RAG store), README and .env (API keys) to read_file/grep/glob.
SANDBOX = ROOT / "data" / "agent_fs"


def _sandbox() -> str:
    """Rebuilt from scratch for every run. The agent has file tools on this folder; anything it wrote there (notes, scratch files, saved tool
    output) or any runbook since deleted from the repo used to survive into the NEXT run, a private channel between runs that no
    metric or log shows."""
    shutil.rmtree(SANDBOX, ignore_errors=True)
    shutil.copytree(ROOT / "skills", SANDBOX / "skills")
    return str(SANDBOX)

# Retry any failed model call (rate limit, timeout, 5xx) with exponential backoff: 1s, 2s, 4s (+jitter).
# on_failure="error": if all retries fail, raise, so the caller (eval harness) can clean up the fault.
RETRY = ModelRetryMiddleware(max_retries=7, initial_delay=2.0, backoff_factor=2.0, on_failure="error")


SYSTEM_PROMPT = """You are an SRE incident-response agent for a Kubernetes platform (namespace <<NAMESPACE>>).
Goal: minimise time-to-resolution, safely and with correct root cause.

<<TOPOLOGY>>

<<METRICS>>

Workflow (track it with write_todos):
1. <<RECALL>>
2. TRIAGE: the first message has a DETECTOR SNAPSHOT; call current_problems to confirm it.
   NO ACTIVE SYMPTOMS means the system is healthy now. Do NOT read skills and do NOT run a wide investigation:
   - vague alert (e.g. "check for issues"): answer now with a short health statement. Do not save an incident.
   - alert names a symptom: delegate ONE question about that symptom to one inspector (max 5 tool calls), then report
     "recovered or not reproducible" with the numbers. Never go looking for other problems.
   Old restarts, client-error (4xx) noise and past rollouts are background, not an incident, unless they match the alert.
   With active symptoms: read the matching skill in /skills/ (start with triage-first-5-minutes).
3. INVESTIGATE (only with active symptoms): delegate to inspectors IN PARALLEL: metrics-inspector, log-inspector,
   trace-inspector, k8s-inspector. Every `task` call MUST set subagent_type to exactly one
   of: metrics-inspector, log-inspector, trace-inspector, k8s-inspector, remediator. Never use `task` just to write the answer. Ask each a specific question, at most 2 rounds. Combine the reports into ONE root-cause
   statement with evidence from at least two signals.
4. FIX: first call current_problems again. If it shows no symptoms the service has already recovered: change nothing,
   report and recommend the durable fix. Otherwise delegate ONE action to `remediator`, the least invasive one that
   addresses the root cause. Only claim a rollout caused it if rollout_diff shows a change; if it shows the suspect setting
   was added in the current revision, rollback to the previous revision is the fix. For OOMKilled: restart to restore service only if it is currently failing, then
   set_memory_limit, and always escalate the leak/sizing fix to a human. After 2 failed fixes, stop and escalate.
   If the right fix is not in your tools, say so and escalate instead of guessing.
5. VERIFY: remediator calls verify_resolved. Do not claim success without the word RESOLVED.
6. <<LEARN>>
7. Final answer: root cause (component + mechanism), evidence, action, verification, what you ruled out.
Never invent evidence. If unsure, say so and escalate to a human.<<EXTRA>>"""

RECALL_ON = ("RECALL: search_incidents (it matches on the detector's symptoms by itself). Matches are hypotheses to test, never evidence: "
             "a recalled incident is not one of your two signals, and its fix is tried only after live data shows the same cause. "
             "A line saying a change did NOT help is a warning, not a hint.")
RECALL_OFF = "RECALL: disabled for this run (no incident memory). Go straight to step 2."
LEARN_ON = ("LEARN (only after a change YOU made returned RESOLVED, or after a change that returned NOT_RESOLVED): save_incident. "
            "It stores what the tools recorded and rejects anything else; your root cause is kept as an unconfirmed note.")
LEARN_OFF = "LEARN: disabled for this run. Do not try to save anything."

# Optional technique prompts. Kept short on purpose: they cost tokens on every model call.
LOCALIZER_PROMPT = ("\n\nLOCALIZER: the first message may contain a LOCALIZER section: facts computed from metrics (not by a model), per component, "
                    "with the pre-incident value beside the current one. It is not ranked and names no cause; a component can appear only because "
                    "something it depends on is wrong. It counts as ONE metrics signal. Explain each fact, and confirm any explanation with a "
                    "second signal type before acting on it.")
FALSIFY_PROMPT = """

FALSIFY BEFORE YOU DIG (applies to every claim: a recalled incident, a localizer suspect, the loudest service, a proposed fix):
- Write the claim and its prediction: "if X is the cause, then Y must be true".
- Pick the cheapest observation that would REFUTE it (Y absent: the suspect's own latency is normal, its dependency is the slow part, no rollout changed anything, no restarts, the error type differs). Run that check FIRST: 1-2 tool calls, before any deep dive.
- Refuted: drop it, note it under 'ruled out' with the observation, spend nothing more on it. Survived: only then go deeper.
- Delegate as: "Claim: ... Try to refute it by checking ...; answer REFUTED or SURVIVES first, with the numbers." Test at most 3 claims.
- Before a fix, one check that the fix target is really the cause. Final answer: 'Ruled out' lists each refuted claim with its refuting observation."""

SUB_FALSIFY = (" If asked to test a claim, look for evidence that REFUTES it first. Start your answer with REFUTED or SURVIVES and the"
               " one observation behind it; go deeper only if it survives and you were asked to.")

# ---- cost techniques (prompt side). Each is added ONLY when its feature is on, so the off arm is byte-for-byte the old prompt.
TRIAGE_ORIG = "TRIAGE: the first message has a DETECTOR SNAPSHOT; call current_problems to confirm it."
TRIAGE_PREFLIGHT = ("TRIAGE: the first message has a DETECTOR SNAPSHOT taken moments ago: do not call current_problems now "
                    "(call it again after any fix).")
PREFLIGHT_PROMPT = ("\n\nPRE-FLIGHT: the first message may contain a PRE-FLIGHT section: log message patterns with counts and Kubernetes Warning "
                    "events, collected by code (not by a model). Observations, not causes: counts cover only the newest lines, and the log patterns count as "
                    "ONE logs signal, the events as ONE cluster signal. Inspectors do not see the first message: copy the facts they need (from PRE-FLIGHT "
                    "and LOCALIZER) into each question and tell them not to collect those again, so they spend their calls on what is still unknown.")
COMPACT_PROMPT = ("\n\nINSPECTOR REPORTS are short on purpose: ANSWER / EVIDENCE / RULED OUT / CONFIDENCE. If you need a number or text that is not in a "
                  "report, ask that inspector one specific follow-up instead of guessing.")
BUDGET_PROMPT = ("\n\nBUDGET: investigation tool calls are capped per incident. Stop investigating as soon as two independent signal types agree on the "
                 "cause AND the claim survived its refuting check: do not start a second round to be thorough. If a tool answers BUDGET REACHED, "
                 "conclude from what you have and state what is unchecked.")
SUB_COMPACT = (" REPORT FORMAT (nothing else, max @WORDS@ words): `ANSWER: <one sentence; when asked to test a claim, REFUTED or SURVIVES first>` then "
               "`EVIDENCE:` up to 4 lines `- <fact with the exact number, error text or value>` then, if any, `RULED OUT:` up to 3 lines "
               "`- <thing> because <observation>` then `CONFIDENCE: high|medium|low`. No preamble, no advice unless asked.")
SUB_COMPACT_K8S = " If rollout_diff showed changes, add a `DIFF:` block with its +/- lines verbatim."
SUB_PRELOADED = (" The question may already contain the metrics, log patterns or events you would normally collect first: use them, do not collect "
                 "them again; spend your calls on what the question leaves unknown.")

# Keeps inspectors short and focused on the symptom window, so a healthy system is not combed for old noise.
# The numbers (calls per inspector, report words) are the run's Tuning; the defaults below reproduce the original prompts exactly.
BUDGET = (" Budget: at most 8 tool calls. The tools already limit data to the current incident window (symptom onset minus 2 minutes);"
          " anything older is background, not evidence. Answer the question you were asked, mention unrelated noise in one line, then stop.")

_SUBAGENTS = [
    {"name": "metrics-inspector",
     "description": "Read-only Prometheus: service error/latency overview, dependency call health, pod CPU/memory/restarts, any PromQL.",
     "system_prompt": "Investigate with metrics. Start with system_overview and dependency_health, then pod_resources for suspects. Report numbers and which service is deepest in the call chain." + BUDGET,
     "tools": PROM_TOOLS},
    {"name": "log-inspector",
     "description": "Read-only Loki: structured JSON logs. Error summaries by event/error_type/dependency, raw log search, logs by trace_id.",
     "system_prompt": "Investigate with logs. Start with log_error_summary, then search_logs on the suspect service. Quote exact error messages and error_type values." + BUDGET,
     "tools": LOKI_TOOLS},
    {"name": "trace-inspector",
     "description": "Read-only Tempo: find slow/error traces and show where time is spent (self-time per span).",
     "system_prompt": "Find slow or failing traces with find_traces and use trace_breakdown to see which span (the service itself, a datastore, a downstream call) dominates. Report the dominating span." + BUDGET,
     "tools": TEMPO_TOOLS},
    {"name": "k8s-inspector",
     "description": "Read-only Kubernetes: pods, events, deployment spec, ReplicaSets and rollout revisions (what changed and when).",
     "system_prompt": "Investigate cluster state. For a failing or not-Ready deployment call rollout_diff FIRST and report its diff verbatim, then pod readiness, events and termination reasons." + BUDGET,
     "tools": K8S_READ_TOOLS},
    {"name": "remediator",
     "description": "The ONLY agent with write access. Applies one approved fix, then verifies with verify_resolved.",
     "system_prompt": "Apply exactly the requested fix with the narrowest tool, then call verify_resolved and report its output verbatim.",
     "tools": WRITE_TOOLS + VERIFY_TOOLS,
     "interrupt_on": {t.name: True for t in WRITE_TOOLS}},
]


budget.READ_TOOLS = frozenset(t.name for t in [*PROM_TOOLS, *LOKI_TOOLS, *TEMPO_TOOLS, *K8S_READ_TOOLS] if t.name != "current_problems")


def make_subagents(f: Features, t: Tuning | None = None) -> list[dict]:
    """Fresh copies per run: prompts depend on the enabled techniques and the run's tuning, and the model may differ from the main agent."""
    t = t or active_tuning()
    sub_model = get_subagent_llm()                  # None unless SRE_SUBAGENT_PROVIDER / SRE_SUBAGENT_MODEL differ
    out = []
    for sa in _SUBAGENTS:
        sa = dict(sa, middleware=[RETRY])
        inspector = sa["name"] != "remediator"
        if inspector and t.inspector_calls != Tuning.inspector_calls:
            sa["system_prompt"] = sa["system_prompt"].replace(BUDGET, BUDGET.replace("at most 8 tool calls", f"at most {t.inspector_calls} tool calls"))
        if f.falsify and inspector:
            sa["system_prompt"] += SUB_FALSIFY
        if f.compact_reports and inspector:                 # the remediator must report verify_resolved verbatim: never compacted
            sa["system_prompt"] += SUB_COMPACT.replace("@WORDS@", str(t.report_words)) + (SUB_COMPACT_K8S if sa["name"] == "k8s-inspector" else "")
        if f.preflight and inspector:
            sa["system_prompt"] += SUB_PRELOADED
        if f.lean_tools:
            sa["middleware"].append(LeanTools(SUBAGENT_DROP))
        if f.budget:
            sa["middleware"].append(budget.ToolBudget())
        if sub_model is not None:
            sa["model"] = sub_model
        out.append(sa)
    return out


def _topology_block() -> str:
    if TOPOLOGY.strip():
        return (f"Topology (verify, don't assume):\n{TOPOLOGY.strip()}\nA failure usually propagates UP the call chain, "
                "so the loudest service is often a victim. Find the DEEPEST failing component.")
    return ("Topology is not provided. Derive the call graph (dependency_health, traces) before blaming a service: "
            "the loudest service is often a victim of a deeper failure.")


def _metrics_block() -> str:
    d, x = METRICS.get("dependency"), METRICS.get("extra_errors") or {}
    lines = [f"Application metrics for query_metrics (per-service label `{METRICS['service_label']}`): requests `{METRICS['requests']}`, "
             f"errors `{METRICS['errors']}`, latency histogram `{METRICS['duration']}`."]
    if d:
        lines.append(f"Dependency calls: errors `{d['errors']}`, latency `{d['duration']}`, labels `{d['source_label']}` -> `{d['target_label']}`.")
    if x:
        lines.append("Other error counters: " + ", ".join(f"`{m}`" for m in x.values()) + ".")
    return "\n".join(lines)


def system_prompt(f: Features | None = None) -> str:
    f = f or from_env()
    extra = ((LOCALIZER_PROMPT if f.localizer else "") + (FALSIFY_PROMPT if f.falsify else "") + (PREFLIGHT_PROMPT if f.preflight else "")
             + (COMPACT_PROMPT if f.compact_reports else "") + (BUDGET_PROMPT if f.budget else ""))
    base = SYSTEM_PROMPT.replace(TRIAGE_ORIG, TRIAGE_PREFLIGHT) if f.preflight else SYSTEM_PROMPT
    return (base.replace("<<NAMESPACE>>", NAMESPACE).replace("<<TOPOLOGY>>", _topology_block())
            .replace("<<METRICS>>", _metrics_block()).replace("<<RECALL>>", RECALL_ON if f.memory else RECALL_OFF)
            .replace("<<LEARN>>", LEARN_ON if f.memory else LEARN_OFF).replace("<<EXTRA>>", extra))


def build_agent(f: Features | None = None, t: Tuning | None = None):
    f = f or from_env()
    extra_mw = ([LeanTools(ORCHESTRATOR_DROP)] if f.lean_tools else []) + ([budget.ToolBudget()] if f.budget else [])
    return create_deep_agent(
        model=get_llm(),
        system_prompt=system_prompt(f),
        tools=(RAG_TOOLS if f.memory else []) + [current_problems],   # orchestrator: memory (optional) + quick symptom check; no direct cluster access
        subagents=make_subagents(f, t),
        skills=["/skills/"],
        backend=FilesystemBackend(root_dir=_sandbox(), virtual_mode=True),
        middleware=[TodoListMiddleware(), RETRY, *extra_mw],
        interrupt_on={t.name: True for t in WRITE_TOOLS},
        checkpointer=MemorySaver(),
    )
