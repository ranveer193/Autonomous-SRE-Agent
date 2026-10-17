"""Offline sanity tests for parsers (no cluster, no LLM). Run: python tests_offline.py"""
import json
from sre_agent.tools.tempo import parse_trace, breakdown
from sre_agent.tools.loki import _line


def sp(i, parent, name, s, e, svc_attrs=None, status=0):
    return {"spanId": i, "parentSpanId": parent, "name": name, "startTimeUnixNano": str(s * 10**6),
            "endTimeUnixNano": str(e * 10**6), "attributes": svc_attrs or [], "status": {"code": status}}


doc = {"batches": [
    {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "payment-service"}}]},
     "scopeSpans": [{"spans": [sp("a", "", "POST /payments", 0, 2600),
                               sp("b", "a", "SELECT payments", 50, 2550, [{"key": "db.system", "value": {"stringValue": "postgresql"}}])]}]}]}
out = breakdown(parse_trace(doc))
print(out)
assert "SELECT payments db=postgresql" in out and out.index("SELECT") < out.index("POST")   # db span dominates self-time

v2 = {"trace": {"resourceSpans": doc["batches"]}}
assert len(parse_trace(v2)) == 2

line = _line("1760000000000000000", json.dumps({"level": "ERROR", "event": "redis_error", "dependency": "redis",
                                                 "error_type": "ConnectionError", "message": "x", "trace_id": "abc"}))
print(line)
assert "dependency=redis" in line and "trace_id=abc" in line
print("OK")
