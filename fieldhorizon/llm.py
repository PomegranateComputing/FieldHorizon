from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

from .config import AppConfig
from .prompting import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Reused across calls instead of a fresh TCP connection (and TLS handshake,
# where applicable) per request.
_session = requests.Session()

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 1.0


class OllamaError(RuntimeError):
    """Ollama is unreachable after retries, or returned no usable content."""


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        return response is not None and response.status_code >= 500
    return False


def _post_with_retry(url: str, payload: dict[str, Any], timeout: int, op_name: str) -> dict:
    """
    Shared retry loop for any Ollama HTTP endpoint: retries connection
    errors and 5xx responses with jittered exponential backoff, and
    re-raises 4xx / exhausted retries as OllamaError.
    """
    last_exc: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = _session.post(url, json=payload, timeout=timeout)
            res.raise_for_status()
            return res.json()

        except (requests.exceptions.ConnectionError, requests.exceptions.HTTPError) as exc:
            last_exc = exc

            if attempt >= MAX_ATTEMPTS or not _is_retryable(exc):
                break

            backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "Ollama %s request failed (attempt %d/%d): %s. Retrying in %.1fs.",
                op_name,
                attempt,
                MAX_ATTEMPTS,
                exc,
                backoff,
            )
            time.sleep(backoff)

    raise OllamaError(
        f"Ollama {op_name} request failed after {MAX_ATTEMPTS} attempt(s): {last_exc}"
    ) from last_exc


def call_ollama(
    cfg: AppConfig,
    prompt: str,
    model: str | None = None,
    options: dict[str, Any] | None = None,
    system: str | None = None,
) -> str:
    """
    `options` overrides individual generation options (e.g. {"temperature": 0}
    for calls that must return structured JSON) without disturbing the
    configured defaults for every other call.

    `system` overrides the FIELD_HORIZON persona in prompting.SYSTEM_PROMPT
    for this call only. Default None keeps the persona, so every existing
    call site is byte-identical. The corpus harvester's books/manifesto
    classifier passes its own neutral, injection-hardened system prompt:
    the doctrinal persona forbids structured output and grants no defence
    against instructions embedded in a harvested text, both of which a
    classifier reading untrusted third-party documents needs.
    """
    model = model or cfg.default_model
    url = cfg.ollama_base_url.rstrip("/") + "/api/chat"
    call_options: dict[str, Any] = {
        "temperature": cfg.temperature,
        "top_p": cfg.top_p,
        "repeat_penalty": cfg.repeat_penalty,
        "num_ctx": cfg.num_ctx,
    }
    call_options.update(options or {})

    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "options": call_options,
    }

    data = _post_with_retry(url, payload, timeout=300, op_name="chat")
    content = data.get("message", {}).get("content", "").strip()

    if not content:
        # Raise rather than return "" so a caller's retry loop
        # (e.g. synthesis.generate_fragment) sees a real failure
        # instead of an empty fragment silently flowing downstream.
        raise OllamaError(f"Ollama returned an empty message (model={model!r}).")

    return content


def embed(cfg: AppConfig, text: str, model: str | None = None) -> list[float]:
    """
    Embed `text` via Ollama's /api/embeddings (default model configured
    under embeddings.model in config.yaml, e.g. nomic-embed-text). Fully
    local -- same Ollama server as generation, no external API.
    """
    model = model or cfg.embedding_model
    url = cfg.ollama_base_url.rstrip("/") + "/api/embeddings"
    payload = {"model": model, "prompt": text}

    data = _post_with_retry(url, payload, timeout=120, op_name="embeddings")
    vector = data.get("embedding")

    if not vector:
        raise OllamaError(f"Ollama returned no embedding (model={model!r}).")

    return [float(x) for x in vector]
