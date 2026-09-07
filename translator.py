"""Translate item titles in one batch and cache the results."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

# Keep the translation KV record bounded; oldest entries (insertion order)
# are dropped first. Items rarely resurface once pushed, so this is safe.
MAX_CACHE_ENTRIES = 5000


def _parse_json_object(text: str) -> dict:
    """Parse an LLM JSON response, tolerating prose or code fences."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not a JSON object")
    return parsed


async def translate_titles(
    context: Any,
    provider_id: str,
    items: list[dict[str, Any]],
    kv_get: Callable[..., Awaitable[Any]],
    kv_put: Callable[..., Awaitable[None]],
) -> dict[int, str]:
    """Translate unseen titles with one LLM request.

    Args:
        context: AstrBot plugin context.
        provider_id: Configured chat completion provider ID.
        items: Items selected for this push.
        kv_get: Callable used to read the translation cache.
        kv_put: Callable used to write the translation cache.

    Returns:
        Chinese titles keyed by item ID.
    """
    cache = await kv_get("translations", {})
    if not isinstance(cache, dict):
        cache = {}
    cache = {str(key): value for key, value in cache.items() if isinstance(value, str)}

    pending = {
        int(item["id"]): str(item.get("title") or "")
        for item in items
        if str(item["id"]) not in cache
    }
    if not pending:
        return {int(item_id): str(value) for item_id, value in cache.items()}

    try:
        response = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=json.dumps(pending, ensure_ascii=False),
            system_prompt=(
                "Translate Japanese Booth product titles to concise Simplified "
                "Chinese. Keep product names and proper nouns recognizable. "
                'Respond only with JSON in this shape: {"<id>": "<Chinese title>"}.'
            ),
        )
        text = str(response.completion_text or "").strip()
        parsed = _parse_json_object(text)
    except Exception as exc:
        logger.warning("Booth title translation failed: %s", exc)
        return {int(item_id): str(value) for item_id, value in cache.items()}

    for item_id, title in pending.items():
        translated = parsed.get(str(item_id))
        if isinstance(translated, str) and translated.strip():
            cache[str(item_id)] = translated.strip()
    if len(cache) > MAX_CACHE_ENTRIES:
        for key in list(cache.keys())[:-MAX_CACHE_ENTRIES]:
            del cache[key]
    await kv_put("translations", cache)
    return {int(item_id): str(value) for item_id, value in cache.items()}
