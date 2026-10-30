"""One LLM call for the whole project: JSON in, JSON out. Gemini (free tier) by default, Claude by config.

    from ml.llm import complete_json
    complete_json("Classify ...", schema)   # -> parsed JSON matching `schema`

Switch provider with LLM_PROVIDER=claude and ANTHROPIC_API_KEY in .env (LLM_MODEL overrides the model).
`schema` is a JSON Schema subset both providers accept: type, properties, items, enum, required.
shortcut: plain JSON calls only (no tool use or streaming), add them when an agent needs them (T11).
"""
import json
import time

import httpx

from api.config import settings

DEFAULT_MODEL = {"gemini": "gemini-3.1-flash-lite", "claude": "claude-haiku-5-5"}
RETRY_STATUS = {429, 500, 502, 503, 504}


def _post(url: str, headers: dict, body: dict, tries: int = 6) -> dict:
    for attempt in range(tries):
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=120)
        except httpx.TransportError:  # timeouts and dropped connections are transient
            if attempt == tries - 1:
                raise
        else:
            if r.status_code not in RETRY_STATUS or attempt == tries - 1:
                r.raise_for_status()
                return r.json()
        time.sleep(min(60, 4 * 2**attempt))  # free-tier rate limits clear within a minute


def _gemini(prompt: str, schema: dict, model: str, temperature: float) -> str:
    key = settings.gemini_api_key
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set in .env")
    out = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key},  # header, not ?key=, so the key never lands in logged URLs
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"temperature": temperature, "responseMimeType": "application/json", "responseJsonSchema": schema}},
    )
    return out["candidates"][0]["content"]["parts"][0]["text"]


def _claude(prompt: str, schema: dict, model: str, temperature: float) -> str:
    key = settings.anthropic_api_key
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    out = _post(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        {"model": model, "max_tokens": 4096, "temperature": temperature,
         "output_config": {"format": {"type": "json_schema", "schema": schema}},
         "messages": [{"role": "user", "content": prompt}]},
    )
    return out["content"][0]["text"]


def complete_json(prompt: str, schema: dict, temperature: float = 0.0) -> dict | list:
    provider = settings.llm_provider
    if provider not in DEFAULT_MODEL:
        raise RuntimeError(f"LLM_PROVIDER must be one of {sorted(DEFAULT_MODEL)}, got {provider!r}")
    model = settings.llm_model or DEFAULT_MODEL[provider]
    call = _gemini if provider == "gemini" else _claude
    return json.loads(call(prompt, schema, model, temperature))
