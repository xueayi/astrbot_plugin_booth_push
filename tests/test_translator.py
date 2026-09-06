from __future__ import annotations

import asyncio

from translator import _parse_json_object, _unwrap_key, translate_titles


class FailingContext:
    async def llm_generate(self, **kwargs):
        raise RuntimeError("provider key invalid")


def test_parse_json_object_tolerates_code_fence():
    parsed = _parse_json_object('ok\n```json\n{"1": "中文一号"}\n```')
    assert parsed == {"1": "中文一号"}


def test_unwrap_key_removes_list_wrapper():
    assert _unwrap_key("['sk-real-key']") == "sk-real-key"
    assert _unwrap_key("sk-plain-key") == "sk-plain-key"


def test_translate_titles_falls_back_to_direct_api(monkeypatch):
    cache: dict = {}

    async def kv_get(key, default=None):
        return cache.get(key, default)

    async def kv_put(key, value):
        cache[key] = value

    async def fake_direct(_pending, _provider_id):
        return '{"7": "中文标题"}'

    monkeypatch.setattr("translator._direct_llm_text", fake_direct)
    result = asyncio.run(
        translate_titles(
            FailingContext(),
            "provider-a",
            [{"id": 7, "title": "日本語タイトル"}],
            kv_get,
            kv_put,
        )
    )
    assert result == {7: "中文标题"}
    assert cache["translations"]["7"] == "中文标题"
