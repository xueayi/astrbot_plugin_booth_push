"""Direct incremental crawler for Booth.pm."""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from astrbot.api import logger

BROWSE_URL = "https://booth.pm/ja/browse/{category}?sort=new&page={page}"
ITEM_JSON_URL = "https://booth.pm/ja/items/{item_id}.json"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ITEM_ID_RE = re.compile(r"/items/(\d+)")


def parse_price(raw: Any) -> int:
    """Extract the integer price from a Booth JSON value."""
    digits = re.sub(r"[^\d]", "", str(raw or ""))
    return int(digits) if digits else 0


def is_free_item(price: Any) -> bool:
    """Return whether an item is free by its price.

    Price is the only reliable signal: many paid items mention 無料/フリー
    in their titles (e.g. bundle notes), so title markers are not used.
    """
    return parse_price(price) <= 0


def _pximg_thumb_300(url: str) -> str | None:
    """Rewrite a pximg original URL to Booth's 300x300 CDN crop."""
    parts = urlsplit(url)
    if parts.netloc != "booth.pximg.net" or parts.path.startswith("/c/"):
        return None
    if not (parts.path.lower().endswith(".jpg") or parts.path.lower().endswith(".jpeg")):
        return None
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            "/c/300x300_a2_g5" + parts.path,
            parts.query,
            parts.fragment,
        )
    )


def pick_thumb(images: list[Any]) -> str:
    """Pick the best thumbnail variant from Booth image dicts."""
    if not images:
        return ""
    for image in images:
        if isinstance(image, dict):
            thumb = _pximg_thumb_300(image.get("original") or "")
            if thumb:
                return thumb
    for image in images:
        if not isinstance(image, dict):
            continue
        for key in ("resized", "main", "sqm"):
            value = image.get(key)
            if value:
                return value
    first = images[0]
    if isinstance(first, dict):
        return first.get("original") or ""
    return ""


class _RequestThrottle:
    """Enforce a minimum global interval between requests across worker threads.

    With a thread pool, sleeping per task lets ``workers / delay`` requests
    fire in parallel; reserving time slots serializes the actual request rate.
    """

    def __init__(self, interval: float) -> None:
        self._interval = max(0.0, interval)
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        """Reserve the next request slot and sleep until it starts."""
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self._interval
        if delay:
            time.sleep(delay)


def map_item(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Booth item JSON payload to a normalized item dict."""
    if not payload or payload.get("is_placeholder"):
        return None
    item_id = payload.get("id")
    if not item_id:
        return None
    images = payload.get("images") or []
    image_urls = [image.get("original") for image in images if image and image.get("original")]
    image_url = image_urls[0] if image_urls else ""
    shop = payload.get("shop") or {}
    tags = [tag.get("name") for tag in (payload.get("tags") or []) if tag.get("name")]
    return {
        "id": int(item_id),
        "title": payload.get("name") or "",
        "price": parse_price(payload.get("price")),
        "description": payload.get("description") or "",
        "image_url": image_url,
        "shop_name": shop.get("name") or "",
        "shop_url": shop.get("url") or "",
        "category": (payload.get("category") or {}).get("name") or "",
        "tags": json.dumps(tags, ensure_ascii=False),
        "item_url": f"https://booth.pm/ja/items/{item_id}",
        "likes": int(payload.get("wish_lists_count") or 0),
        "created_at": payload.get("published_at") or "",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "image_urls": json.dumps(image_urls, ensure_ascii=False),
        "thumb_url": pick_thumb(images),
        "is_free": 1 if is_free_item(payload.get("price")) else 0,
    }


def http_get_text(client: httpx.Client, url: str) -> str:
    """GET a URL with simple retries for transient Booth errors."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.get(url)
            if response.status_code == 404:
                return ""
            if response.status_code == 429:
                time.sleep(5 * (attempt + 1))
                last_error = RuntimeError(f"429 on {url}")
                continue
            response.raise_for_status()
            return response.text
        except (httpx.HTTPStatusError, httpx.TransportError, httpx.InvalidURL) as exc:
            last_error = exc
        time.sleep(1.5 * (attempt + 1))
    logger.error("Booth fetch failed for %s: %s", url, last_error)
    return ""


def page_item_ids(
    client: httpx.Client,
    category: str,
    page: int,
    throttle: _RequestThrottle,
) -> list[int]:
    """Collect item IDs from one sort=new browse page."""
    url = BROWSE_URL.format(category=quote(category, safe=""), page=page)
    throttle.wait()
    body = http_get_text(client, url)
    ids: list[int] = []
    seen: set[int] = set()
    for match in ITEM_ID_RE.finditer(body):
        item_id = int(match.group(1))
        if item_id not in seen:
            seen.add(item_id)
            ids.append(item_id)
    return ids


def fetch_item(
    client: httpx.Client,
    item_id: int,
    throttle: _RequestThrottle,
) -> dict[str, Any] | None:
    """Fetch and map one Booth item."""
    throttle.wait()
    body = http_get_text(client, ITEM_JSON_URL.format(item_id=item_id))
    if not body:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return map_item(payload)


def _published_after(item: dict[str, Any], since: str) -> bool:
    """Return whether an item was published after the UTC lower bound."""
    if not since:
        return True
    try:
        item_at = datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00"))
        since_at = datetime.fromisoformat(since)
        if item_at.tzinfo is None:
            item_at = item_at.replace(tzinfo=timezone.utc)
        if since_at.tzinfo is None:
            since_at = since_at.replace(tzinfo=timezone.utc)
        return item_at >= since_at
    except ValueError:
        return True


def crawl_with_client(
    client: httpx.Client,
    categories: list[str],
    seen: set[int],
    since: str,
    max_pages: int,
    workers: int,
    delay: float,
) -> dict[str, Any]:
    """Crawl Booth and return unseen items without persisting anything."""
    known = set(seen)
    throttle = _RequestThrottle(delay)
    summary: dict[str, Any] = {
        "items_by_category": {},
        "scanned": 0,
        "added": 0,
    }

    def fetch_one(item_id: int) -> dict[str, Any] | None:
        return fetch_item(client, item_id, throttle)

    for category in categories:
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            ids = page_item_ids(client, category, page, throttle)
            new_ids = [item_id for item_id in ids if item_id not in known]
            summary["scanned"] += len(new_ids)
            if not new_ids:
                break

            rows: list[dict[str, Any] | None] = []
            if workers > 1 and len(new_ids) > 1:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {executor.submit(fetch_one, item_id): item_id for item_id in new_ids}
                    for future in as_completed(futures):
                        rows.append(future.result())
            else:
                rows = [fetch_one(item_id) for item_id in new_ids]

            for row in rows:
                if row is None or row["id"] in known:
                    continue
                if row.get("category") != category or not _published_after(row, since):
                    continue
                known.add(row["id"])
                items.append(row)
                summary["added"] += 1

        if items:
            summary["items_by_category"][category] = items
    return summary


def crawl_new(
    categories: list[str],
    seen: set[int],
    since: str,
    max_pages: int,
    workers: int,
    delay: float,
    timeout: float,
    proxy: str = "",
) -> dict[str, Any]:
    """Open one HTTP client and crawl all requested categories."""
    kwargs: dict[str, Any] = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": httpx.Timeout(timeout, connect=15.0),
        "follow_redirects": True,
        "trust_env": True,
    }
    if proxy:
        kwargs["proxy"] = proxy
    with httpx.Client(**kwargs) as client:
        return crawl_with_client(
            client,
            categories,
            seen,
            since,
            max_pages,
            workers,
            delay,
        )
