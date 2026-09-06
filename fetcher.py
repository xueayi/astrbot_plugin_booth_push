"""Download item thumbnails into the plugin cache."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from astrbot.api import logger


async def download_thumb(
    item: dict[str, Any],
    cache_dir: Path,
    timeout: int,
    proxy: str = "",
) -> Path | None:
    """Download one item thumbnail into the plugin cache.

    Args:
        item: Item object containing ``id`` and ``thumb_url``.
        cache_dir: Thumbnail cache directory.
        timeout: Request timeout in seconds.
        proxy: Optional HTTP proxy URL.

    Returns:
        The cached image path, or ``None`` when no thumbnail is available.
    """
    thumb_url = str(item.get("thumb_url") or "")
    if not thumb_url:
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{item['id']}.jpg"
    temporary = cache_dir / f".{item['id']}.tmp"
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=True,
            follow_redirects=True,
            proxy=proxy or None,
        ) as client:
            response = await client.get(
                thumb_url,
                headers={"Referer": "https://booth.pm/"},
            )
            response.raise_for_status()
            temporary.write_bytes(response.content)
        temporary.replace(destination)
        return destination
    except Exception as exc:
        logger.warning("Failed to download thumbnail %s: %s", item["id"], exc)
        temporary.unlink(missing_ok=True)
        return None
