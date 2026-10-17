"""Optional techniques. Nothing here is permanent: every run takes a Features value, so the console, the CLI and the
eval harness can switch each one on or off per run. Defaults come from env (SRE_<NAME>, e.g. SRE_LOCALIZER, SRE_LEAN_TOOLS).

Two families:
  behaviour   localizer, falsify, memory           change WHAT the agent knows or checks
  cost        preflight, cluster_logs, lean_tools, compact_reports, budget
              change HOW MUCH the model has to read, write and call. Each is meant to leave the answer unchanged;
              the point of the switch is to prove that with a run: same root-cause accuracy, fewer tokens.
All cost switches are OFF by default, so an existing setup behaves exactly as before until one is turned on."""
import os
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Features:
    localizer: bool = False     # deterministic pre-computed evidence board (metrics facts, no ranking), injected into the first message (no LLM)
    falsify: bool = False       # prompt-only: refute each claim with one cheap check before digging deeper
    memory: bool = True         # incident memory (RAG): recall at the start, save after a verified outcome
    # ---- cost techniques
    preflight: bool = False     # code collects log patterns + warning events before the model starts; the model skips its own triage round trips
    cluster_logs: bool = False  # search_logs returns message templates with counts instead of raw lines
    lean_tools: bool = False    # hide built-in tools the phase does not need (file tools for inspectors) and shorten the todo tool description
    compact_reports: bool = False  # inspectors answer in a fixed short format instead of prose
    budget: bool = False        # hard cap on investigation tool calls per incident + stop-when-two-signals-agree rule

    def as_dict(self) -> dict:
        return asdict(self)

    def label(self) -> str:
        on = [k for k, v in self.as_dict().items() if v]
        return "+".join(on) if on else "llm-only"


NAMES = tuple(Features.__dataclass_fields__)
COST = ("preflight", "cluster_logs", "lean_tools", "compact_reports", "budget")
_DEFAULT_ON = {n for n, f in Features.__dataclass_fields__.items() if f.default is True}

# Cumulative behaviour arms: each adds one technique to the previous one.
ARMS: dict[str, Features] = {
    "llm-only": Features(False, False, False),
    "localizer": Features(True, False, False),
    "falsify": Features(True, True, False),
    "memory": Features(True, True, True),
}
DEFAULT_ARMS = tuple(ARMS)      # what --compare runs unless told otherwise (the cost arms below are opt-in)

# Cumulative cost arms, on top of the full behaviour set (localizer + falsify + memory). Run them with --arms cost-base,cost-lean,...
_B = dict(localizer=True, falsify=True, memory=True)
COST_ARMS: dict[str, Features] = {
    "cost-base": Features(**_B),
    "cost-lean": Features(**_B, lean_tools=True),
    "cost-logs": Features(**_B, lean_tools=True, cluster_logs=True),
    "cost-preflight": Features(**_B, lean_tools=True, cluster_logs=True, preflight=True),
    "cost-reports": Features(**_B, lean_tools=True, cluster_logs=True, preflight=True, compact_reports=True),
    "cost-budget": Features(**_B, lean_tools=True, cluster_logs=True, preflight=True, compact_reports=True, budget=True),
}
ARMS.update(COST_ARMS)


def _env(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def from_env() -> Features:
    return Features(**{n: _env("SRE_" + n.upper(), n in _DEFAULT_ON) for n in NAMES})


def parse(spec: str | None) -> Features:
    """'' -> env defaults. An arm name ('falsify', 'cost-lean') -> that arm. Otherwise a comma list of the techniques to turn ON
    ('localizer,memory,lean_tools'); anything not listed is off. 'none' = all off."""
    s = (spec or "").strip().lower()
    if not s:
        return from_env()
    if s in ARMS:
        return ARMS[s]
    names = {n.strip() for n in s.split(",") if n.strip()} - {"none"}
    bad = names - set(NAMES)
    if bad:
        raise ValueError(f"unknown feature(s) {sorted(bad)}; use {', '.join(NAMES)} or an arm: {list(ARMS)}")
    return Features(**{n: n in names for n in NAMES})


# ---- the features of the run in progress. Tools are module-level, so they read this at call time instead of at import.
_active: Features | None = None


def set_active(f: Features | None) -> None:
    global _active
    _active = f


def active() -> Features:
    """Features of the run in progress (set by run_alert); env defaults when no run set any (tools used on their own, tests)."""
    return _active if _active is not None else from_env()
