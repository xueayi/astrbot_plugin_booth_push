from __future__ import annotations

import asyncio

from translator import _parse_json_object, translate_titles


class OkContext:
    async def llm_generate(self, **kwargs):
        class R:
            completion_text = 'ok\n```json\n{"7": "中文标题"}\n```'

        return R()


def test_parse_json_object_tolerates_code_fence():
    parsed = _parse_json_object('ok\n```json\n{"1": "中文一号"}\n```')
    assert parsed == {"1": "中文一号"}


def test_translate_titles_uses_official_provider_api():
    cache: dict = {}

    async def kv_get(key, default=None):
        return cache.get(key, default)

    async def kv_put(key, value):
        cache[key] = value

    result = asyncio.run(
        translate_titles(
            OkContext(),
            "provider-a",
            [{"id": 7, "title": "日本語タイトル"}],
            kv_get,
            kv_put,
        )
    )
    assert result == {7: "中文标题"}
    assert cache["translations"]["7"] == "中文标题"


def test_translate_titles_failure_keeps_cache():
    class FailingContext:
        async def llm_generate(self, **kwargs):
            raise RuntimeError("provider key invalid")

    cache: dict = {}

    async def kv_get(key, default=None):
        return cache.get(key, default)

    async def kv_put(key, value):
        cache[key] = value

    result = asyncio.run(
        translate_titles(
            FailingContext(),
            "provider-a",
            [{"id": 7, "title": "日本語タイトル"}],
            kv_get,
            kv_put,
        )
    )
    assert result == {}
