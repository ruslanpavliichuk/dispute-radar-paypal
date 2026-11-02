import json

import httpx
import pytest
import respx

from api.config import settings
from ml.llm import complete_json

LIST = {"type": "array", "items": {"type": "string"}}
OBJ = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}


@pytest.fixture
def openai(monkeypatch):
    for k, v in {"llm_provider": "openai", "llm_api_key": "k", "llm_base_url": "https://api.x.ai/v1/", "llm_model": "grok-x"}.items():
        monkeypatch.setattr(settings, k, v)


def reply(content: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})


@respx.mock
def test_openai_compatible_wraps_a_list_root_and_unwraps_it(openai):
    route = respx.post("https://api.x.ai/v1/chat/completions").mock(return_value=reply({"items": ["a", "b"]}))
    assert complete_json("hi", LIST) == ["a", "b"]
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "grok-x" and sent["response_format"]["json_schema"]["schema"]["required"] == ["items"]
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
def test_openai_compatible_passes_an_object_root_through(openai):
    respx.post("https://api.x.ai/v1/chat/completions").mock(return_value=reply({"label": "other"}))
    assert complete_json("hi", OBJ) == {"label": "other"}


def test_openai_without_settings_fails_clearly(openai, monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "")
    with pytest.raises(RuntimeError, match="LLM_MODEL"):
        complete_json("hi", OBJ)


def test_unknown_provider_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "bard")
    with pytest.raises(RuntimeError, match="LLM_PROVIDER"):
        complete_json("hi", OBJ)


@respx.mock
def test_gemini_default_sends_key_in_header(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "g-key")
    monkeypatch.setattr(settings, "llm_model", "")
    route = respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"label": "other"}'}]}}]}))
    assert complete_json("hi", OBJ) == {"label": "other"}
    assert route.calls[0].request.headers["x-goog-api-key"] == "g-key" and "key=" not in str(route.calls[0].request.url)
