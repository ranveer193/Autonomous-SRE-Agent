import os, shlex
from .profile import load

PROFILE = load()                                   # system.yaml: all system-specific names live there
NAMESPACE = os.getenv("SRE_NAMESPACE") or PROFILE["namespace"]
DRY_RUN = os.getenv("SRE_DRY_RUN", "false").lower() == "true"
PROVIDER = os.getenv("SRE_PROVIDER", "groq").lower()          # groq | gemini | huggingface | nvidia | openrouter
_DEFAULT_MODELS = {"groq": "openai/gpt-oss-120b", "gemini": "gemini-2.5-flash",
                   "huggingface": "Qwen/Qwen2.5-72B-Instruct","nvidia" : "nvidia/nemotron-3-ultra-550b-a55b",
                   "openrouter": "google/gemma-4-31b-it"}
MODEL_NAME = os.getenv("SRE_MODEL") or _DEFAULT_MODELS.get(PROVIDER, "")
# Optional different model for the spawned subagents (inspectors + remediator). Unset = same as the main agent.
SUB_PROVIDER = (os.getenv("SRE_SUBAGENT_PROVIDER") or PROVIDER).lower()
SUB_MODEL = os.getenv("SRE_SUBAGENT_MODEL") or (MODEL_NAME if SUB_PROVIDER == PROVIDER else _DEFAULT_MODELS.get(SUB_PROVIDER, ""))
PROM_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200")

# From system.yaml. An empty `writable` list means the agent can never change anything.
WRITABLE_DEPLOYMENTS = set(PROFILE["writable"])
SERVICES = PROFILE["services"]
TOPOLOGY = PROFILE["topology"]
SELECTOR_LABEL = PROFILE["selector_label"]
METRICS = PROFILE["metrics"]
LOKI = PROFILE["loki"]

# Detection thresholds (used for alerting AND for "is the problem gone?")
ERROR_RATE_PCT = float(PROFILE["thresholds"]["error_rate_pct"])
P95_SECONDS = float(PROFILE["thresholds"]["p95_seconds"])

# Safety limits of the write tools (system.yaml `limits`) and the kubectl command (SRE_KUBECTL, e.g. "kubectl --context prod" or "oc").
MAX_REPLICAS = int(PROFILE["limits"]["max_replicas"])
MAX_MEMORY_MI = int(PROFILE["limits"]["max_memory_mi"])
KUBECTL = shlex.split(os.getenv("SRE_KUBECTL", "kubectl"))
