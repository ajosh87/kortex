"""Which LLM answers: 'provider:model' specs, OpenRouter fallbacks, cost estimates, and failed turns in plain words."""
import json
import os
import urllib.request
from functools import cache

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_openai import ChatOpenAI

load_dotenv()
MODEL = os.getenv("MODEL", "openrouter:openai/gpt-oss-120b")

# OpenRouter backup models, tried in order when the main one errors (e.g. a busy free model)
FALLBACKS = [m.strip() for m in os.getenv("MODEL_FALLBACKS", "openai/gpt-oss-120b").split(",") if m.strip()]

# provider -> env var holding its API key (Bedrock uses the normal AWS credential chain instead)
KEY_ENV = {"openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY",
           "openai": "OPENAI_API_KEY", "bedrock_converse": None}

# Public deployments: visitors bring their own API key. The server's keys (and its AWS credentials) are never used.
REQUIRE_USER_KEY = os.getenv("REQUIRE_USER_KEY", "").strip().lower() in ("1", "true", "yes")

# ...except for a few demo questions per browser session on the server's key, with the default MODEL only, so
# visitors can try the chat first. Set the hard cap as a credit limit on that key at the provider.
DEMO_QUESTIONS = int(os.getenv("DEMO_QUESTIONS", "5") or 0)

# provider -> suggested model id (any tool-calling model works)
PROVIDERS = {"openrouter": "openai/gpt-oss-120b", "anthropic": "claude-sonnet-5-5", "groq": "openai/gpt-oss-120b",
             "openai": "gpt-5-mini", "bedrock_converse": "us.amazon.nova-pro-v1:0"}


def providers():
    """Providers a visitor can pick. With REQUIRE_USER_KEY, only those that take a pasted key (not Bedrock)."""
    return [p for p in PROVIDERS if not (REQUIRE_USER_KEY and KEY_ENV[p] is None)]


def demo_left(used):
    """Demo questions this browser session can still ask on the server's key (0 when this deployment offers none:
    no REQUIRE_USER_KEY, DEMO_QUESTIONS=0, or no server key for the default model)."""
    env = KEY_ENV.get(split_spec(MODEL)[0])
    return max(0, DEMO_QUESTIONS - used) if REQUIRE_USER_KEY and env and os.getenv(env) else 0


def needs_key(api_key, used=0):
    """Whether a turn can not run yet because this deployment needs the visitor's own key (and the session has
    used up its demo questions)."""
    return REQUIRE_USER_KEY and not (api_key or "").strip() and not demo_left(used)


def split_spec(spec):
    """'groq:openai/gpt-oss-120b' -> ('groq', 'openai/gpt-oss-120b'). A bare OpenRouter id such as
    'meta-llama/llama-4-scout:free' (no known provider prefix) is treated as OpenRouter."""
    provider, _, name = spec.strip().partition(":")
    if provider not in KEY_ENV and "/" in provider:
        return "openrouter", spec.strip()
    return provider, name


def make_model(spec=MODEL, api_key=None, demo=False):
    """'provider:model' -> chat model. e.g. openrouter:openai/gpt-oss-120b, groq:openai/gpt-oss-120b,
    anthropic:claude-sonnet-5-5, openai:gpt-5-mini, bedrock_converse:us.amazon.nova-pro-v1:0. api_key overrides the
    env var. demo: a demo question on the server's key, which always runs the default MODEL."""
    api_key = (api_key or "").strip() or None  # a pasted key often ends with a space or newline
    own = bool(api_key)
    demo = demo and not own and demo_left(0) > 0
    spec = MODEL if demo else spec
    provider, name = split_spec(spec)
    if provider not in KEY_ENV:
        raise ValueError(f"Unsupported provider '{provider}'. Use one of: {', '.join(KEY_ENV)}")
    if REQUIRE_USER_KEY and not demo and (not own or KEY_ENV[provider] is None):
        raise ValueError("own-key: this deployment runs only with the visitor's own API key")
    server_key = os.getenv(KEY_ENV[provider]) if KEY_ENV[provider] else None
    key = api_key if own else (server_key if demo or not REQUIRE_USER_KEY else None)
    if provider in ("openrouter", "openai") and not key:  # else the OpenAI SDK would send OPENAI_API_KEY to OpenRouter
        raise ValueError(f"api_key: no {KEY_ENV[provider]} set")
    if provider == "openrouter":  # OpenAI-compatible API; OpenRouter tries FALLBACKS if the model fails or is rate-limited
        backups = [m for m in FALLBACKS if m != name]
        body = {"usage": {"include": True}, **({"models": [name] + backups} if backups else {})}
        return ChatOpenAI(model=name, api_key=key, base_url="https://openrouter.ai/api/v1", temperature=0,
                          timeout=90, max_retries=2, stream_usage=True, extra_body=body)
    if provider == "anthropic":  # Claude: the model's own default sampling; answers stay grounded by the prompt rules
        return init_chat_model(name, model_provider="anthropic", max_tokens=4096, timeout=90, max_retries=2,
                               **({"api_key": key} if key else {}))
    extra = {"stream_usage": True} if provider == "openai" else {}
    return init_chat_model(name, model_provider=provider, temperature=0, **extra, **({"api_key": key} if key else {}))


def unrepeat(s):
    """LangChain concatenates per-chunk metadata when it merges a stream ('m/xm/x' -> 'm/x')."""
    s = s or ""
    for size in range(1, len(s) // 2 + 1):
        if len(s) % size == 0 and s[:size] * (len(s) // size) == s:
            return s[:size]
    return s


@cache
def openrouter_prices():
    """Per-token prices for every OpenRouter model (public endpoint, fetched once per process)."""
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=10) as r:
            data = json.load(r)["data"]
        return {m["id"]: (float(m["pricing"]["prompt"]), float(m["pricing"]["completion"])) for m in data}
    except Exception:
        return {}


def estimate_cost(spec, model_name, tokens_in, tokens_out):
    """Approximate USD cost of one model call from OpenRouter's published prices; None if unknown."""
    if split_spec(spec)[0] != "openrouter" or tokens_in is None:
        return None
    price = openrouter_prices().get(model_name)
    return tokens_in * price[0] + (tokens_out or 0) * price[1] if price else None


ERRORS = [  # (text found in the error, what happened, what to do)
    ("own-key", "Bring your own API key", "This app answers with your key only: open Model in the sidebar, pick a "
                                          "provider and paste its key. It stays in this browser session. Explore, "
                                          "Memory and Data work without one."),
    ("code: 402", "The key is out of credit", "The demo key's credit is used up (or your key has none). Paste your own "
                                        "key under Model in the sidebar."),
    ("limit exceeded", "The key's spending limit is reached", "Paste your own key under Model in the sidebar."),
    ("overloaded", "The model is overloaded", "The provider is busy right now (Anthropic returns 529). Retry in a "
                                              "minute or pick another model."),
    ("credit balance", "The account has no credit", "Add credit to the provider account, or use another key."),
    ("code: 429", "The model is rate-limited", "Free OpenRouter models share a busy pool. Retry in a minute, or use a "
                                         "paid model such as `openai/gpt-oss-120b` (well under a cent per question)."),
    ("code: 401", "The API key was rejected", "Paste a valid key in the sidebar under Model, or set it in `.env`."),
    ("api_key", "No API key", "Paste a key in the sidebar under Model, or set it in `.env`."),
    ("code: 403", "This model refuses requests from this app", "Pick another model id in the sidebar."),
    ("code: 404", "Model not found", "Check the model id in the sidebar, e.g. `openai/gpt-oss-120b`."),
    ("Connection", "Cannot reach a service", "Check that FalkorDB is running and the model provider is reachable."),
    ("Recursion", "The agent took too many steps", "Ask a narrower question."),
]


def explain_error(e):
    """A failed turn in plain words: what happened and what to do."""
    msg = str(e)
    title, hint = next(((t, h) for needle, t, h in ERRORS if needle.lower() in msg.lower()),
                       ("The turn failed", "See the raw error."))
    return {"type": "error", "title": title, "message": hint, "raw": msg}
