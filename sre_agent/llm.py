import os
from .config import PROVIDER, MODEL_NAME, SUB_PROVIDER, SUB_MODEL


# Seconds to wait for one model response. NVIDIA's client defaults to 60s, which large models (nemotron-ultra) exceed on long contexts.
LLM_TIMEOUT = float(os.getenv("SRE_LLM_TIMEOUT", "180"))

PROVIDERS = ("groq", "gemini", "huggingface", "nvidia", "openrouter")
KEY_ENV = {"groq": ("GROQ_API_KEY",), "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"), "huggingface": ("HUGGINGFACEHUB_API_TOKEN", "HF_TOKEN"),
           "nvidia": ("NVIDIA_API_KEY",), "openrouter": ("OPENROUTER_API_KEY",)}


def key_is_set(provider: str) -> bool:
    return any(os.getenv(n) for n in KEY_ENV.get(provider, ()))


def _key(*names: str) -> str:
    for n in names:
        if os.getenv(n):
            return os.environ[n]
    raise RuntimeError(f"Set one of {names} in .env for provider '{PROVIDER}'")


def get_subagent_llm(temperature: float = 0.0):
    """Model for spawned subagents (SRE_SUBAGENT_PROVIDER / SRE_SUBAGENT_MODEL). None = same as the main agent."""
    if (SUB_PROVIDER, SUB_MODEL) == (PROVIDER, MODEL_NAME):
        return None
    return get_llm(temperature, SUB_PROVIDER, SUB_MODEL)


def get_llm(temperature: float = 0.0, provider: str | None = None, model: str | None = None):
    """Single place that builds the chat model for the agent AND the eval judge. Select with SRE_PROVIDER."""
    return _build(provider or PROVIDER, model or MODEL_NAME, temperature)


def get_judge_llm(temperature: float = 0.0):
    """Grader for the eval harness. SRE_JUDGE_PROVIDER / SRE_JUDGE_MODEL pick a different model than the agent's, so the agent does not
    grade its own report (a model tends to rate its own reasoning as correct). Unset = same as the agent."""
    p = (os.getenv("SRE_JUDGE_PROVIDER") or PROVIDER).lower()
    from .config import _DEFAULT_MODELS
    return _build(p, os.getenv("SRE_JUDGE_MODEL") or (MODEL_NAME if p == PROVIDER else _DEFAULT_MODELS.get(p, "")), temperature)


def _build(provider, model_name, temperature):
    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=model_name, api_key=_key("GROQ_API_KEY"), temperature=temperature, timeout=LLM_TIMEOUT)
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model_name, google_api_key=_key("GOOGLE_API_KEY", "GEMINI_API_KEY"),
                                      temperature=temperature, timeout=LLM_TIMEOUT)
    if provider == "huggingface":
        from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
        endpoint = HuggingFaceEndpoint(repo_id=model_name, task="text-generation", temperature=max(temperature, 0.01),
                                       max_new_tokens=2048, timeout=LLM_TIMEOUT,
                                       huggingfacehub_api_token=_key("HUGGINGFACEHUB_API_TOKEN", "HF_TOKEN"))
        return ChatHuggingFace(llm=endpoint)

    if provider == "nvidia":
        from langchain_nvidia_ai_endpoints import ChatNVIDIA
        return ChatNVIDIA(model=model_name, api_key=_key("NVIDIA_API_KEY"), temperature=temperature, timeout=LLM_TIMEOUT)
    if provider == "openrouter":
        # OpenRouter speaks the OpenAI chat-completions schema (incl. tools), so the OpenAI client works as is.
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model_name, api_key=_key("OPENROUTER_API_KEY"), base_url="https://openrouter.ai/api/v1",
                          temperature=temperature, timeout=LLM_TIMEOUT)
    raise ValueError(f"Unknown provider '{provider}'. Use groq | gemini | huggingface | nvidia | openrouter")
