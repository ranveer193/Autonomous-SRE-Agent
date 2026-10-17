"""Incident memory. Retrieve before investigating, save only what the tools observed after a VERIFIED outcome.

What the previous version got wrong, and what changed:
  * It stored the model's own account (symptoms, root cause, fix) and trusted it. A fix that worked for the wrong stated reason, a victim
    named as the root cause, or a wasted first action all became 'knowledge', and recall then repeated them.
    Now the key, the fix and the component come from the tools: symptoms = the detector's signature, fix = the changes that were
    followed by RESOLVED, component = the deployment that fix acted on, tried_and_failed = the changes followed by NOT_RESOLVED.
    The model's wording is kept only as 'agent's account (unconfirmed)'.
  * save_incident could be called any time; 'only after RESOLVED' was a prompt request. Now it is refused unless a change made in THIS
    incident was followed by RESOLVED ('failed' needs a change followed by NOT_RESOLVED). Recovered-by-itself is never saved.
  * Every run stored a new row, so three identical rows looked like three confirmations. Now identical incidents are merged and counted.
  * Matching used the model's free-text wording, so the query rarely resembled the stored text. Now both sides are the detector's signature
    (service:kind pairs, no magnitudes) and a match needs real overlap; it reports how many symptoms matched.
  * Recall was written up as evidence ('similarity 0.87, identical pattern'). The tool now states it is a hypothesis and shows what did
    NOT work, so a past mistake is not repeated.
Entries are 'system-verified'; `review()` lets a human or a judge confirm or delete them. New collection name: rows of the old format
(free-text keys, unverified) must not be mixed in."""
import logging, os, time, uuid
from pathlib import Path
import chromadb
from langchain_core.tools import tool
from . import incident

logger = logging.getLogger("sre")

# Absolute, so the console, the CLI and the eval harness all open the SAME store whatever their working directory is
# (a relative path silently gave each launch directory its own memory). Override with SRE_MEMORY_DIR.
STORE = Path(os.getenv("SRE_MEMORY_DIR") or Path(__file__).resolve().parent.parent / "data" / "chroma")
_col = chromadb.PersistentClient(path=str(STORE)).get_or_create_collection(
    "incidents_v2", metadata={"hnsw:space": "cosine"})
_clock = {"start": time.time()}
_emb_cache: list = []
MIN_OVERLAP = 0.5                    # share of symptoms that must match (Jaccard on the signature sets) to be shown at all
MERGE_OVERLAP = 0.8                  # same component + same fix + this much overlap = the same incident again


def _emb():
    """Lazy so the model loads on first use (and the log line appears after logging is set up)."""
    if not _emb_cache:
        from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
        _emb_cache.append(FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5"))
        logger.info("Using FastEmbed (ONNX) for vectorstore")
    return _emb_cache[0]


def start_incident_clock() -> None:
    _clock["start"] = time.time()


def _sig_set(sig: str) -> set[str]:
    return {x.strip() for x in (sig or "").split(";") if x.strip()}


def overlap(a: str, b: str) -> float:
    A, B = _sig_set(a), _sig_set(b)
    return len(A & B) / len(A | B) if A | B else 0.0


def _age(ts: int) -> str:
    d = max(0, int(time.time()) - int(ts))
    return f"{d // 86400}d ago" if d >= 86400 else (f"{d // 3600}h ago" if d >= 3600 else f"{d // 60}m ago")


@tool
def search_incidents(symptoms: str = "", service: str = "", k: int = 3) -> str:
    """Search past incidents with the same symptom signature as the current one. Call this first.
    Matching uses the detector's own symptoms for THIS incident (your `symptoms` text is only a fallback), so you do not need to
    describe them. Results are HYPOTHESES from earlier incidents, never evidence and never one of your signals: test the component
    and the fix against live data before relying on either. 'service' optionally filters on the component."""
    sig = incident.signature() or symptoms
    if not sig:
        return "No symptoms to match against."
    if _col.count() == 0:
        return "No past incidents stored yet."
    where = {"component": service} if service else None
    # Exact set overlap over every stored incident. The store is small, and a text embedding of 'a:5xx; b:latency' says nothing about
    # which symptom sets are alike: ranking by it first could drop the best match once the store grew past the candidate cut-off.
    have = _col.get(where=where) if where else _col.get()
    only_confirmed = os.getenv("SRE_MEMORY_CONFIRMED_ONLY", "").strip().lower() in ("1", "true", "yes", "on")
    rows = []
    for doc, m in zip(have["documents"], have["metadatas"]):
        ov = overlap(sig, m.get("sig", ""))
        if ov >= MIN_OVERLAP and (m.get("confirmed") or not only_confirmed):
            rows.append((m.get("outcome") == "resolved", ov, m.get("count", 1), doc, m))
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    if not rows:
        return "No past incident with a similar symptom signature."
    incident.mark_recalled()                              # the run's rag_hit comes from here, not from guessing at this tool's wording
    out = ["Past incidents with similar symptoms. HYPOTHESES to test, not evidence; do not cite them as a signal."]
    for good, ov, n, doc, m in rows[:k]:
        tag = "worked" if good else "did NOT work"
        trust = "confirmed" if m.get("confirmed") else "verified by the system, root cause unconfirmed"
        line = (f"[{int(ov * 100)}% of symptoms match, seen {n}x (repeats of one fault are not independent confirmations), "
                f"last {_age(m.get('ts', 0))}, {trust}] component={m.get('component')} "
                f"fix {tag}: {m.get('fix') or '-'} (ttr_s={m.get('ttr_seconds')})\n  {doc}")
        if m.get("tried_failed"):
            line += f"\n  tried and did NOT help: {m['tried_failed']}"
        out.append(line)
    return "\n\n".join(out)


@tool
def incident_elapsed_seconds() -> str:
    """Seconds since this incident was opened."""
    return str(int(time.time() - _clock["start"]))


@tool
def save_incident(service: str, symptoms: str, root_cause: str, fix: str, outcome: str, ttr_seconds: int) -> str:
    """Save the incident you just handled. Stored from what the tools recorded: the detector's symptoms, the change that was followed by
    RESOLVED, the deployment it acted on, and any change that was followed by NOT_RESOLVED. Your `symptoms`, `fix` and
    `ttr_seconds` are ignored; `service` and `root_cause` are kept only as your account, marked unconfirmed.
    outcome='resolved' is accepted only after a change you made was followed by RESOLVED; outcome='failed' only after a change you
    made was followed by NOT_RESOLVED. An incident that recovered by itself is never saved."""
    if outcome not in ("resolved", "failed"):
        return "Error: outcome must be 'resolved' or 'failed'"
    if outcome == "resolved" and not incident.has_verified_fix():
        return ("NOT SAVED: no change made in this incident was followed by RESOLVED. Memory only stores verified fixes; "
                "if it recovered by itself or you did not change anything, there is nothing to store.")
    if outcome == "failed" and not incident.has_failed_attempt():
        return "NOT SAVED: no change made in this incident was followed by NOT_RESOLVED, so there is no failed attempt to remember."
    sig = incident.signature() or symptoms
    good = outcome == "resolved"
    fix_txt = incident.fix_summary(good)                  # resolved: the changes followed by RESOLVED; failed: those followed by NOT_RESOLVED
    tried = incident.fix_summary(False) if good else ""   # a first action that did not help is worth remembering too
    component = (incident.effective_target() if good else "") or service
    note = "" if component == service else f" (stored under '{component}', the deployment the fix acted on, not '{service}')"
    have = _col.get(where={"component": component})
    for rid, m in zip(have["ids"], have["metadatas"]):
        if m.get("fix") == fix_txt and m.get("outcome") == outcome and overlap(sig, m.get("sig", "")) >= MERGE_OVERLAP:
            n = int(m.get("count", 1)) + 1
            ttr = incident.ttr_s()
            _col.update(ids=[rid], metadatas=[{**m, "count": n, "ts": int(time.time()),
                                               "ttr_seconds": min(int(m.get("ttr_seconds", 0)), ttr) if ttr else int(m.get("ttr_seconds", 0))}])
            incident.add_saved(rid)
            return f"Merged into an existing incident (now seen {n}x){note}."
    doc = f"agent's account (unconfirmed): {root_cause.strip()[:400]}"
    rid = str(uuid.uuid4())
    _col.add(ids=[rid], documents=[doc], embeddings=[_emb().embed_documents([sig])[0]],
             metadatas=[{"component": component, "claimed": service, "fix": fix_txt, "tried_failed": tried, "sig": sig, "outcome": outcome,
                         "ttr_seconds": incident.ttr_s() or 0, "ts": int(time.time()), "count": 1, "verified": True, "confirmed": False}])
    incident.add_saved(rid)
    return f"Saved incident ({outcome}){note}."


def review(ids: list[str], correct: bool) -> dict:
    """Confirm (correct=True) or delete (False) stored incidents. For a human on the console or a judge in the eval harness:
    the system verified the FIX, only a reviewer can vouch for the explanation."""
    ids = [i for i in ids if i]
    if not ids:
        return {"confirmed": 0, "deleted": 0}
    if correct:
        have = _col.get(ids=ids)
        for rid, m in zip(have["ids"], have["metadatas"]):
            _col.update(ids=[rid], metadatas=[{**m, "confirmed": True}])
        return {"confirmed": len(have["ids"]), "deleted": 0}
    _col.delete(ids=ids)
    return {"confirmed": 0, "deleted": len(ids)}


def list_incidents() -> list[dict]:
    """All stored incidents, newest first (for the UI)."""
    r = _col.get()
    rows = [{"id": i, "doc": d, **m} for i, d, m in zip(r["ids"], r["documents"], r["metadatas"])]
    return sorted(rows, key=lambda x: x.get("ts", 0), reverse=True)


def clear_memory() -> int:
    """Delete every stored incident (test isolation)."""
    ids = _col.get()["ids"]
    if ids:
        _col.delete(ids=ids)
    return len(ids)


RAG_TOOLS = [search_incidents, incident_elapsed_seconds, save_incident]
