"""Analysis-only recovery from confirmed OpenAI prepaid-credit exhaustion.

The process-local latch avoids repeated paid-provider attempts until restart.
Never wrap order execution in this helper: only a fresh analysis is retried.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from threading import Lock

from openai import APIStatusError

from .routing import describe_routing


_LOCK = Lock()
_EXHAUSTED = {}


def is_credit_exhausted(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, APIStatusError) and current.status_code == 429:
            body = current.body
            if isinstance(body, dict):
                detail = body.get("error", body)
                if isinstance(detail, dict) and detail.get("code") == "credit_balance_exhausted":
                    # Local servers also use the OpenAI SDK; only trust the
                    # actual OpenAI endpoint, not a quoted error in model text.
                    if current.response.request.url.host == "api.openai.com":
                        return True
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
    return False


def _scope(config):
    endpoint = config.get("hybrid_openai_backend_url") if config["llm_provider"] == "hybrid" else config.get("backend_url")
    values = [endpoint or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")]
    values.extend(os.getenv(key, "") for key in ("OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID"))
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def run_with_credit_fallback(config, *, enabled, local_models, analyze, on_switch):
    """Return (effective config, analysis result, audit metadata).

    analyze must construct a NEW graph each time; partial cloud reports must
    not become inputs to the local retry. on_switch persists routing first.
    """
    requested = config["llm_provider"]
    metadata = {"requestedProvider": requested, "provider": requested,
                "fallback": None, "attempts": [], "agentRouting": describe_routing(config)}
    eligible = enabled and requested in {"openai", "hybrid"}
    scope = _scope(config)
    with _LOCK:
        detected = _EXHAUSTED.get(scope) if eligible else None

    def switch(reason):
        nonlocal config
        config = deepcopy(config)
        config.update(llm_provider="ollama", quick_think_llm=local_models["quick"],
                      deep_think_llm=local_models["deep"], backend_url=local_models["url"],
                      ollama_backend_url=local_models["url"], news_local_single_pass=True,
                      news_openai_fallback=False, local_safe_validation=True)
        metadata.update(provider="ollama", agentRouting=describe_routing(config),
                        fallback={"code": "credit_balance_exhausted", "mode": reason,
                                  "detectedAt": detected})
        on_switch(config, deepcopy(metadata))

    def attempt():
        item = {"provider": config["llm_provider"], "status": "running"}
        metadata["attempts"].append(item)
        try:
            value = analyze(config)
        except BaseException:
            item["status"] = "failed"
            raise
        item["status"] = "completed"
        return value

    try:
        if detected:
            switch("latched")
        try:
            result = attempt()
        except Exception as error:
            if not eligible or detected or not is_credit_exhausted(error):
                raise
            detected = datetime.now(timezone.utc).isoformat()
            with _LOCK:
                _EXHAUSTED[scope] = detected
            switch("restart")
            result = attempt()
    except BaseException as error:
        error.llm_execution = deepcopy(metadata)
        raise
    return config, result, metadata
