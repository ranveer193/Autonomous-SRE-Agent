"""Tuning: the numbers behind the techniques, settable per run (console Advanced options, --tuning, env).

Features say WHICH techniques are on. Tuning says HOW MUCH: the tool-call cap, how many calls an inspector may make, how long a
compact report may be... Before this, these were constants in the code (or one env var), so trying another value meant editing code
and the result file did not say which value produced it. Now every run takes a Tuning value, records it in its result, and the
defaults reproduce the old behaviour exactly: prompts and limits are unchanged until someone changes a number.

Each knob only does something when its technique is on (SPEC says which); a value set for a technique that is off is stored but unused.
Resolution order for a run: explicit value (console / --tuning / API) > env (SRE_TOOL_BUDGET, SRE_REPORT_WORDS ...) > default.
Explicit values are validated strictly (a typo is an error, never silently the default). Env values are lenient (junk = default,
out of range = nearest allowed value), as the old SRE_TOOL_BUDGET was."""
import os
from dataclasses import dataclass, asdict, fields


@dataclass(frozen=True)
class Tuning:
    tool_budget: int = 30         # budget:          investigation READ tool calls allowed per incident, all agents together
    inspector_calls: int = 8      # (always)         "at most N tool calls" written into every inspector's prompt
    report_words: int = 150       # compact_reports: word limit of an inspector's report
    log_patterns: int = 12        # cluster_logs:    patterns shown per search_logs call
    log_fetch: int = 100          # cluster_logs:    log lines fetched per search_logs call before clustering
    preflight_chars: int = 3200   # preflight:       size cap of the pre-flight block in the first message
    recursion_limit: int = 120    # (always)         graph steps before the run is aborted (LangGraph recursion_limit)

    def as_dict(self) -> dict:
        return asdict(self)

    def changed(self) -> dict:
        """Only the values that differ from the built-in defaults (what a report line needs to show)."""
        d = Tuning()
        return {k: v for k, v in self.as_dict().items() if v != getattr(d, k)}

    def label(self) -> str:
        c = self.changed()
        return ",".join(f"{k}={v}" for k, v in c.items()) if c else "defaults"


# name -> (min, max, used when, short label, what it does / what to watch). The console renders its inputs from this (via the API).
SPEC: dict[str, dict] = {
    "tool_budget": {"min": 1, "max": 200, "uses": "budget", "label": "Tool-call budget",
                    "hint": "Max investigation tool calls per incident (all agents together). Writes, verify and the final answer are never blocked. "
                            "Read the baseline arm's tool_calls first; if budget_blocked > 0 in a run that lost accuracy, the cap is too low."},
    "inspector_calls": {"min": 1, "max": 30, "uses": "", "label": "Calls per inspector",
                        "hint": "The 'at most N tool calls' line in each inspector's prompt. A request, not an enforced cap (that is the budget)."},
    "report_words": {"min": 30, "max": 500, "uses": "compact_reports", "label": "Report words",
                     "hint": "Word limit of a compact inspector report. Too low can drop evidence."},
    "log_patterns": {"min": 1, "max": 50, "uses": "cluster_logs", "label": "Log patterns shown",
                     "hint": "Message patterns returned by one search_logs call; the rest are summarised as '+N more'."},
    "log_fetch": {"min": 10, "max": 500, "uses": "cluster_logs", "label": "Log lines fetched",
                  "hint": "Lines read from Loki per search_logs call before they are clustered. Counts only cover these lines."},
    "preflight_chars": {"min": 500, "max": 12000, "uses": "preflight", "label": "Pre-flight size (chars)",
                        "hint": "Hard cap on the pre-flight block (about 4 chars per token)."},
    "recursion_limit": {"min": 20, "max": 500, "uses": "", "label": "Step limit",
                        "hint": "Graph steps before the run is aborted as runaway. Lower it to stop loops early; too low aborts a normal run."},
}
NAMES = tuple(f.name for f in fields(Tuning))
assert set(NAMES) == set(SPEC), "every Tuning field needs a SPEC entry"
ENV = {n: "SRE_" + n.upper() for n in NAMES}          # tool_budget keeps its original name, SRE_TOOL_BUDGET


def _clamp(name: str, v: int) -> int:
    return max(SPEC[name]["min"], min(SPEC[name]["max"], v))


def from_env() -> Tuning:
    """Lenient: a missing, empty or non-numeric env value is the default; a number outside the range is moved to the nearest allowed one."""
    base, out = Tuning(), {}
    for n in NAMES:
        raw = os.getenv(ENV[n])
        try:
            out[n] = _clamp(n, int(raw)) if raw not in (None, "") else getattr(base, n)
        except ValueError:
            out[n] = getattr(base, n)
    return Tuning(**out)


def _one(name: str, value) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    try:
        v = int(str(value).strip()) if not isinstance(value, int) else value
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {value!r}") from None
    lo, hi = SPEC[name]["min"], SPEC[name]["max"]
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {v}")
    return v


def parse(spec: "str | dict | None") -> Tuning:
    """Strict. '' / None / {} -> env defaults. 'tool_budget=20,report_words=100' or a dict -> those values on top of the env defaults.
    Blank values in a dict (an empty console field) are skipped. Unknown names, non-numbers and out-of-range numbers raise ValueError."""
    if spec is None or spec == "" or spec == {}:
        return from_env()
    if isinstance(spec, str):
        pairs = {}
        for part in (p.strip() for p in spec.split(",")):
            if not part:
                continue
            k, eq, v = part.partition("=")
            if not eq:
                raise ValueError(f"expected name=value, got {part!r}; names: {', '.join(NAMES)}")
            pairs[k.strip()] = v.strip()
        spec = pairs
    bad = set(spec) - set(NAMES)
    if bad:
        raise ValueError(f"unknown tuning value(s) {sorted(bad)}; use {', '.join(NAMES)}")
    base = from_env().as_dict()
    for k, v in spec.items():
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        base[k] = _one(k, v)
    return Tuning(**base)


def to_cli(t: Tuning) -> str:
    """EVERY value as `--tuning` text, for a subprocess. All of them, not just the changed ones: the child re-reads env, and a value equal to the
    code default can still differ from what env says, so leaving it out would silently lose the number the person chose."""
    return ",".join(f"{k}={v}" for k, v in t.as_dict().items())


# ---- the tuning of the run in progress. Tools and the budget are module-level, so they read this at call time (same pattern as features).
_active: Tuning | None = None


def set_active(t: Tuning | None) -> None:
    global _active
    _active = t


def active() -> Tuning:
    """Tuning of the run in progress (set by run_alert); env defaults when no run set any (tools used on their own, tests)."""
    return _active if _active is not None else from_env()
