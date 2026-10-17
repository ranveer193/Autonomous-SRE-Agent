"""Is a model usable by the agent? Two cheap calls: (1) a plain reply, (2) a tool call, because the agents cannot work without tool calling.
Used by the console's Health page. Never raises: every problem comes back as a result row. Secrets are redacted from error text."""
import threading, time
from .llm import get_llm, KEY_ENV, PROVIDERS
from .logging_utils import redact

PING = "Reply with the single word: OK"
TOOL_PROMPT = "Call the function check_tool with value 'ok'. Do not answer in text."


def _hint(err: str, provider: str) -> str:
    e = err.lower()
    if "set one of" in e or "api_key" in e and "missing" in e:
        return f"Add the API key for '{provider}' ({' or '.join(KEY_ENV.get(provider, ()))}) to .env and restart the console."
    if any(x in e for x in ("401", "403", "unauthorized", "invalid api key", "permission", "forbidden")):
        return "The API key was rejected. Check that it is correct and has access to this model."
    if any(x in e for x in ("404", "not found", "does not exist", "unknown model", "decommissioned")):
        return "The provider does not know this model name. Check the spelling and that your account can use it."
    if any(x in e for x in ("429", "rate limit", "quota", "resource_exhausted")):
        return "Rate limit or quota reached. Wait a moment, or use another key or model."
    if "timed out" in e or "timeout" in e:
        return "No answer in time. The provider may be slow or unreachable from this machine."
    if any(x in e for x in ("connection", "resolve", "network", "ssl")):
        return "Could not reach the provider. Check your internet connection or proxy."
    if "no module named" in e:
        return "A client package for this provider is not installed. Run: pip install -r requirements.txt"
    return ""


def _call(fn, timeout: float):
    """Run fn in a daemon thread so a hung provider cannot hang the console. Returns (value, error_text)."""
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as e:                        # noqa: BLE001  (report everything, including import errors)
            box["e"] = f"{type(e).__name__}: {e}"
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, f"TimeoutError: no answer within {int(timeout)}s"
    return box.get("v"), box.get("e")


def check_model(provider: str, model: str, timeout: float = 45, build=None) -> dict:
    """-> {provider, model, status: ok|warn|fail, reachable, tool_calling, latency_s, error, hint}. `build` is injectable for tests."""
    build = build or (lambda: get_llm(0.0, provider, model))
    row = {"provider": provider, "model": model, "status": "fail", "reachable": False, "tool_calling": None, "latency_s": None, "error": "", "hint": ""}
    if not model:
        return {**row, "error": "no model name set", "hint": "Set SRE_MODEL (or SRE_SUBAGENT_MODEL) in .env."}
    llm, err = _call(build, 20)
    if err:
        return {**row, "error": redact(err)[:300], "hint": _hint(err, provider)}
    t0 = time.time()
    _, err = _call(lambda: llm.invoke(PING), timeout)
    if err:
        return {**row, "error": redact(err)[:300], "hint": _hint(err, provider)}
    row.update(reachable=True, latency_s=round(time.time() - t0, 1))

    def tools():
        from langchain_core.tools import tool

        @tool
        def check_tool(value: str) -> str:
            """Test function. Call it with value 'ok'."""
            return value
        return llm.bind_tools([check_tool]).invoke(TOOL_PROMPT)
    msg, err = _call(tools, timeout)
    if err:
        return {**row, "status": "warn", "tool_calling": False, "error": redact(err)[:300],
                "hint": "The model answers but tool calling failed. The agents need tool calling: pick an instruct model with tool support."}
    ok = bool(getattr(msg, "tool_calls", None))
    return {**row, "tool_calling": ok, "status": "ok" if ok else "warn",
            "hint": "" if ok else "The model answers but did not call the test tool. The agents need reliable tool calling: pick another model."}
