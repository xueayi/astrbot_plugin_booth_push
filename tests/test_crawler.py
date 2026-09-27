from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crawler import (
    _RequestThrottle,
    crawl_with_client,
    is_free_item,
    map_item,
    page_item_ids,
)


class FakeClient:
    pass


def test_map_item_normalizes_booth_payload():
    item = map_item(
        {
            "id": 123,
            "name": "制服セット",
            "price": "800円",
            "description": "desc",
            "images": [
                {
                    "original": "https://booth.pximg.net/a/b.jpg",
                    "resized": "https://booth.pximg.net/c/300x300_a2_g5/a/b.jpg",
                }
            ],
            "shop": {"name": "Shop", "url": "https://shop.example"},
            "category": {"name": "3D衣装"},
            "tags": [{"name": "制服"}],
            "wish_lists_count": 12,
            "published_at": "2026-09-05T10:00:00+09:00",
        }
    )

    assert item is not None
    assert item["id"] == 123
    assert item["price"] == 800
    assert item["is_free"] == 0
    assert item["thumb_url"].startswith("https://booth.pximg.net/c/300x300_a2_g5/")
    assert item["item_url"] == "https://booth.pm/ja/items/123"
    assert item["category"] == "3D衣装"


def test_is_free_item_uses_price_only():
    # Title markers like 無料/FREE are unreliable: paid bundles mention them.
    assert is_free_item("0円") is True
    assert is_free_item("") is True
    assert is_free_item(None) is True
    assert is_free_item("800円") is False
    assert is_free_item("無料で使えます 800円") is False


def test_page_item_ids_extracts_unique_ids(monkeypatch):
    monkeypatch.setattr(
        "crawler.http_get_text",
        lambda client, url: (
            '<a href="/ja/items/11">A</a><a href="/ja/items/11">B</a><a href="/ja/items/22">C</a>'
        ),
    )
    ids = page_item_ids(FakeClient(), "3D衣装", 1, _RequestThrottle(0))
    assert ids == [11, 22]


def test_throttle_serializes_request_slots(monkeypatch):
    import crawler

    sleeps: list[float] = []
    monkeypatch.setattr(crawler.time, "sleep", lambda s: sleeps.append(s))
    throttle = _RequestThrottle(0.5)
    start = crawler.time.monotonic()
    monkeypatch.setattr(crawler.time, "monotonic", lambda: start)

    throttle.wait()
    throttle.wait()
    throttle.wait()

    # The first slot is free; each subsequent wait reserves the next 0.5s slot.
    assert sleeps == [0.5, 1.0]


def test_crawl_with_client_filters_seen_and_window(monkeypatch):
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    old_item = {
        "id": 1,
        "category": "3D衣装",
        "is_free": 1,
        "likes": 1,
        "created_at": (now - timedelta(days=2)).isoformat(),
    }
    new_item = {
        "id": 2,
        "category": "3D衣装",
        "is_free": 0,
        "likes": 5,
        "created_at": (now - timedelta(hours=1)).isoformat(),
    }
    seen_item = {
        "id": 3,
        "category": "3D髪型",
        "is_free": 1,
        "likes": 9,
        "created_at": now.isoformat(),
    }
    pages = {("3D衣装", 1): [1, 2], ("3D髪型", 1): [3]}
    monkeypatch.setattr(
        "crawler.page_item_ids",
        lambda client, category, page, throttle: pages.get((category, page), []),
    )
    monkeypatch.setattr(
        "crawler.fetch_item",
        lambda client, item_id, throttle: {
            1: old_item,
            2: new_item,
            3: seen_item,
        }[item_id],
    )

    result = crawl_with_client(
        FakeClient(),
        ["3D衣装", "3D髪型"],
        seen={3},
        since=since,
        max_pages=2,
        workers=1,
        delay=0,
    )

    assert result["added"] == 1
    assert [item["id"] for item in result["items_by_category"]["3D衣装"]] == [2]
    assert "3D髪型" not in result["items_by_category"]


def _likes_crawl(monkeypatch, likes_by_id, min_likes=0):
    now = datetime.now(timezone.utc)
    items = {
        item_id: {
            "id": item_id,
            "category": "3D衣装",
            "is_free": 0,
            "likes": likes,
            "created_at": now.isoformat(),
        }
        for item_id, likes in likes_by_id.items()
    }
    monkeypatch.setattr(
        "crawler.page_item_ids",
        lambda client, category, page, throttle: list(likes_by_id),
    )
    monkeypatch.setattr(
        "crawler.fetch_item",
        lambda client, item_id, throttle: items[item_id],
    )
    return crawl_with_client(
        FakeClient(),
        ["3D衣装"],
        seen=set(),
        since="",
        max_pages=1,
        workers=1,
        delay=0,
        min_likes=min_likes,
    )


def test_crawl_with_client_min_likes_threshold(monkeypatch):
    result = _likes_crawl(monkeypatch, {10: 3, 11: 5, 12: 6, 13: 20}, min_likes=5)

    # min_likes=5 excludes items with likes <= 5; 6 and 20 stay.
    collected = result["items_by_category"]["3D衣装"]
    assert sorted(item["id"] for item in collected) == [12, 13]


def test_crawl_with_client_min_likes_zero_keeps_all(monkeypatch):
    result = _likes_crawl(monkeypatch, {10: 0, 11: 5})

    assert result["added"] == 2
