"""One LLM call for the whole project: JSON in, JSON out. Gemini (free tier) by default; Claude or any
OpenAI-compatible API (OpenAI, xAI Grok, ...) by config.

    from ml.llm import complete_json
    complete_json("Classify ...", schema)   # -> parsed JSON matching `schema`

Switch provider in .env: LLM_PROVIDER=gemini (GEMINI_API_KEY), claude (ANTHROPIC_API_KEY), or openai
(LLM_API_KEY + LLM_BASE_URL, e.g. https://api.x.ai/v1 for Grok; LLM_MODEL is required there). LLM_MODEL overrides the default model.
`schema` is a JSON Schema subset both providers accept: type, properties, items, enum, required.
shortcut: plain JSON calls only (no tool use or streaming), add them when an agent needs them (T11).
"""
import json
import time

import httpx

from api.config import settings

DEFAULT_MODEL = {"gemini": "gemini-3.1-flash-lite", "claude": "claude-haiku-5-5", "openai": ""}
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


def _gemini(prompt: str, schema: dict, model: str, temperature: float, tries: int) -> str:
    key = settings.gemini_api_key
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set in .env")
    out = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key},  # header, not ?key=, so the key never lands in logged URLs
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"temperature": temperature, "responseMimeType": "application/json", "responseJsonSchema": schema}},
        tries,
    )
    return out["candidates"][0]["content"]["parts"][0]["text"]


def _claude(prompt: str, schema: dict, model: str, temperature: float, tries: int) -> str:
    key = settings.anthropic_api_key
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    out = _post(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        {"model": model, "max_tokens": 4096, "temperature": temperature,
         "output_config": {"format": {"type": "json_schema", "schema": schema}},
         "messages": [{"role": "user", "content": prompt}]},
        tries,
    )
    return out["content"][0]["text"]


def _openai(prompt: str, schema: dict, model: str, temperature: float, tries: int) -> str:
    if not (settings.llm_api_key and settings.llm_base_url and model):
        raise RuntimeError("provider openai needs LLM_API_KEY, LLM_BASE_URL and LLM_MODEL in .env")
    wrapped = schema["type"] != "object"  # json_schema responses must be objects: wrap a list and unwrap it below
    out = _post(
        settings.llm_base_url.rstrip("/") + "/chat/completions",
        {"Authorization": f"Bearer {settings.llm_api_key}"},
        {"model": model, "temperature": temperature, "messages": [{"role": "user", "content": prompt}],
         "response_format": {"type": "json_schema", "json_schema": {
             "name": "answer", "schema": {"type": "object", "properties": {"items": schema}, "required": ["items"]} if wrapped else schema}}},
        tries,
    )
    text = out["choices"][0]["message"]["content"]
    return json.dumps(json.loads(text)["items"]) if wrapped else text


def complete_json(prompt: str, schema: dict, temperature: float = 0.0, tries: int = 6) -> dict | list:
    provider = settings.llm_provider
    if provider not in DEFAULT_MODEL:
        raise RuntimeError(f"LLM_PROVIDER must be one of {sorted(DEFAULT_MODEL)}, got {provider!r}")
    model = settings.llm_model or DEFAULT_MODEL[provider]
    call = {"gemini": _gemini, "claude": _claude, "openai": _openai}[provider]
    return json.loads(call(prompt, schema, model, temperature, tries))
