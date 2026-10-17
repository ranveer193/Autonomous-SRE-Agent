"""Offline tests for the model check. No network, no API keys: the model is a fake.   python tests_models.py"""
import sys, time
from types import SimpleNamespace as NS
from sre_agent.model_check import check_model

res = []
check = lambda n, c, d="": res.append((n, bool(c), d))


class Fake:
    def __init__(self, reply="OK", tool=True, err=None, bind_err=None, delay=0):
        self.reply, self.tool, self.err, self.bind_err, self.delay = reply, tool, err, bind_err, delay

    def invoke(self, x):
        time.sleep(self.delay)
        if self.err:
            raise RuntimeError(self.err)
        return NS(content=self.reply, tool_calls=[{"name": "check_tool"}] if getattr(self, "_bound", False) and self.tool else [])

    def bind_tools(self, tools):
        if self.bind_err:
            raise RuntimeError(self.bind_err)
        b = Fake(self.reply, self.tool, self.err, None, self.delay)
        b._bound = True
        return b


r = check_model("groq", "m", build=lambda: Fake())
check("working model with tool calling is ok", r["status"] == "ok" and r["reachable"] and r["tool_calling"] and r["latency_s"] is not None, str(r))
r = check_model("groq", "m", build=lambda: Fake(tool=False))
check("answers but no tool call is limited", r["status"] == "warn" and r["reachable"] and r["tool_calling"] is False and r["hint"], str(r))
r = check_model("groq", "m", build=lambda: Fake(bind_err="does not support tools"))
check("tool binding error is limited, not failed", r["status"] == "warn" and r["reachable"], str(r))
r = check_model("groq", "m", build=lambda: Fake(err="401 Unauthorized: Invalid API Key gsk_abcdefghijklmnopqrstuvwxyz123456"))
check("rejected key is fail with a key hint", r["status"] == "fail" and not r["reachable"] and "key" in r["hint"].lower(), str(r))
check("secret is redacted from the error", "gsk_abcdefghijklmnopqrstuvwxyz123456" not in r["error"], r["error"])
r = check_model("groq", "m", build=lambda: Fake(err="404 model not found"))
check("unknown model gives a name hint", r["status"] == "fail" and "model name" in r["hint"].lower(), str(r))
r = check_model("groq", "m", build=lambda: Fake(err="429 rate limit"))
check("rate limit gives a wait hint", "rate limit" in r["hint"].lower(), str(r))
r = check_model("groq", "m", timeout=1, build=lambda: Fake(delay=3))
check("hung provider times out instead of hanging", r["status"] == "fail" and "timed out" in r["hint"].lower() or "timeout" in r["error"].lower(), str(r))
r = check_model("groq", "", build=lambda: Fake())
check("empty model name is fail", r["status"] == "fail" and "SRE_MODEL" in r["hint"], str(r))
def boom():
    raise RuntimeError("Set one of ('GROQ_API_KEY',) in .env for provider 'groq'")
r = check_model("groq", "m", build=boom)
check("missing key is explained", r["status"] == "fail" and "GROQ_API_KEY" in r["hint"], str(r))
r = check_model("groq", "m", build=lambda: (_ for _ in ()).throw(ModuleNotFoundError("No module named 'langchain_groq'")))
check("missing client package is explained", "pip install" in r["hint"], str(r))

bad = [n for n, ok, d in res if not ok]
for n, ok, d in res:
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else "  " + d))
sys.exit(1 if bad else 0)
