"""The five evaluation metrics, per run and per arm. Pure functions, no I/O: everything comes from a finished run's result.

  mttr_s            seconds from detection to healthy (None if the agent did not fix it: the harness had to recover)
  root_cause_ok     judge verdict (None if the judge failed: excluded from accuracy, never counted as wrong)
  distractor_blamed judge verdict; rate = blamed / judged runs
  tokens            input + output tokens of the agent (judge excluded). Input tokens are summed over every model call,
                    so they include re-sent context: compare arms with each other, not with a provider bill.
  tool_calls        real tool calls. Subagent hand-offs ('task') and the todo list are not counted; hand-offs are `delegations`.

Every count is split into "main" (the orchestrator) and "sub" (all spawned subagents) so a cheaper subagent model shows up.
"""
from statistics import mean

MAIN = "orchestrator"                       # agent name the logger gives the top-level agent


def split_by_agent(by_agent: dict | None) -> dict:
    """{agent: counters} -> {main: {...}, sub: {...}} with tokens, tool_calls, llm_calls, delegations."""
    out = {k: {"tokens": 0, "tool_calls": 0, "llm_calls": 0, "delegations": 0} for k in ("main", "sub")}
    for agent, c in (by_agent or {}).items():
        d = out["main" if agent == MAIN else "sub"]
        d["tokens"] += int(c.get("tokens_in", 0)) + int(c.get("tokens_out", 0))
        d["tool_calls"] += int(c.get("tool_calls", 0))
        d["llm_calls"] += int(c.get("llm_calls", 0))
        d["delegations"] += int(c.get("delegations", 0))
    return out


def run_metrics(res: dict) -> dict:
    """res = the dict run_scenario.run_once builds (needs mttr_s, stats, verdict). Safe on crashed or unjudged runs."""
    stats, v = res.get("stats") or {}, res.get("verdict") or {}
    ran = bool(stats.get("llm_calls"))      # a crashed agent has no stats: unknown, not zero
    sp = split_by_agent(stats.get("by_agent"))
    return {
        "mttr_s": res.get("mttr_s"),
        "root_cause_ok": v.get("root_cause_correct"),
        "distractor_blamed": v.get("blamed_distractor"),
        "tokens": (sp["main"]["tokens"] + sp["sub"]["tokens"]) if ran else None,
        # cost-technique readouts. tokens_in is the re-sent context (what lean_tools / clustering / pre-flight shrink); cached is the
        # part the provider billed as cache hits (0 when the provider reports none: then caching is unmeasured, not absent).
        "llm_calls": stats.get("llm_calls") if ran else None,
        "tokens_in": stats.get("tokens_in") if ran else None,
        "tokens_cached": stats.get("tokens_cached", 0) if ran else None,
        "budget_blocked": (res.get("budget") or {}).get("blocked") if ran else None,
        "tool_calls": (sp["main"]["tool_calls"] + sp["sub"]["tool_calls"]) if ran else None,
        "main": sp["main"] if ran else None,
        "sub": sp["sub"] if ran else None,
    }


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(mean(xs), 1) if xs else None


def _cached_pct(ms):
    """Share of input tokens served from the provider's cache, over runs that reported tokens_in. None when no run has input tokens."""
    ti = sum(m.get("tokens_in") or 0 for m in ms)
    return round(100 * sum(m.get("tokens_cached") or 0 for m in ms) / ti, 1) if ti else None


def _rate(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(bool(x) for x in xs) / len(xs), 3) if xs else None


def aggregate(cases: list[dict]) -> dict:
    """cases = scenario case dicts that each carry a `metrics` dict. Returns one row of means/rates.
    MTTR is averaged over recovered runs only; `recovered` shows how many that was."""
    ms = [c["metrics"] for c in cases if c.get("metrics")]
    side = lambda k, f: _avg([(m.get(k) or {}).get(f) for m in ms if m.get(k)])
    return {
        "runs": len(ms),
        "recovered": sum(m["mttr_s"] is not None for m in ms),
        "mttr_s": _avg([m["mttr_s"] for m in ms]),
        "root_cause_acc": _rate([m["root_cause_ok"] for m in ms]),
        "distractor_rate": _rate([m["distractor_blamed"] for m in ms]),
        "tokens": _avg([m["tokens"] for m in ms]),
        "tokens_main": side("main", "tokens"), "tokens_sub": side("sub", "tokens"),
        "tool_calls": _avg([m["tool_calls"] for m in ms]),
        "llm_calls": _avg([m.get("llm_calls") for m in ms]),
        "tokens_in": _avg([m.get("tokens_in") for m in ms]),
        "cached_pct": _cached_pct(ms),
        "budget_blocked": _avg([m.get("budget_blocked") for m in ms]),
        "tools_main": side("main", "tool_calls"), "tools_sub": side("sub", "tool_calls"),
    }


def compare(cases: list[dict], arms: list[str]) -> dict:
    """{arm: aggregate row}. If the memory arm ran more than once per scenario, adds 'memory (warm)' = run 2 and later,
    the runs that can actually use what run 1 stored. 'memory' itself then mixes cold and warm runs."""
    ok = [c for c in cases if c.get("kind") == "scenario" and c.get("status") != "SKIP"]
    out = {a: aggregate([c for c in ok if c.get("arm") == a]) for a in arms}
    warm = [c for c in ok if c.get("arm") == "memory" and (c.get("run") or 1) > 1]
    if "memory" in arms and warm:
        out["memory (warm)"] = aggregate(warm)
    return out
