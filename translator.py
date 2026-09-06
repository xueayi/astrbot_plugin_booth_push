"""Translate item titles in one batch and cache the results."""

from __future__ import annotations

import ast
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


def _unwrap_key(raw: Any) -> str:
    """Extract a usable API key from common config storage shapes."""
    text = str(raw or "").strip()
    if not text.startswith("["):
        return text
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list) and parsed:
            return str(parsed[0]).strip()
    except (SyntaxError, ValueError):
        pass
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list) and parsed:
            return str(parsed[0]).strip()
    except (TypeError, ValueError):
        pass
    return text


def _provider_credentials(provider_id: str) -> tuple[str, str, str] | None:
    """Read AstrBot provider config without modifying it."""
    config_path = Path(get_astrbot_data_path()) / "cmd_config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    provider = next(
        (item for item in config.get("provider", []) if item.get("id") == provider_id),
        None,
    )
    source_id = str(provider.get("provider_source_id") or "") if provider else ""
    source = next(
        (item for item in config.get("provider_sources", []) if item.get("id") == source_id),
        None,
    )
    if not provider or not source:
        return None
    api_base = str(source.get("api_base") or "").rstrip("/")
    key = _unwrap_key(source.get("key") or provider.get("key") or "")
    model = str(provider.get("model") or source.get("model") or "")
    return (api_base, key, model) if api_base and key and model else None


async def _direct_llm_text(pending: dict[int, str], provider_id: str) -> str:
    """Call an OpenAI-compatible provider directly with AstrBot credentials."""
    credentials = _provider_credentials(provider_id)
    if not credentials:
        raise RuntimeError("Provider credentials are not available.")
    api_base, key, model = credentials
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate Japanese Booth product titles to concise Simplified "
                    "Chinese. Keep product names and proper nouns recognizable. "
                    'Respond only with JSON in this shape: {"<id>": "<Chinese title>"}.'
                ),
            },
            {
                "role": "user",
                "content": json.dumps(pending, ensure_ascii=False),
            },
        ],
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=60.0, trust_env=True) as client:
        response = await client.post(
            f"{api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM response has no choices.")
    return str((choices[0].get("message") or {}).get("content") or "")


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
        logger.warning(
            "Booth title translation via AstrBot context failed; trying direct API: %s",
            exc,
        )
        try:
            direct_text = await _direct_llm_text(pending, provider_id)
            parsed = _parse_json_object(direct_text)
        except Exception as direct_exc:
            logger.warning("Booth title translation failed: %s", direct_exc)
            return {int(item_id): str(value) for item_id, value in cache.items()}

    for item_id, title in pending.items():
        translated = parsed.get(str(item_id))
        if isinstance(translated, str) and translated.strip():
            cache[str(item_id)] = translated.strip()
    await kv_put("translations", cache)
    return {int(item_id): str(value) for item_id, value in cache.items()}
