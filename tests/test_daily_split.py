"""Integration tests for the free/paid push switches and split images."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from astrbot.core.message.components import Image

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_plugin_booth_push_under_test"


def _load_plugin_module():
    """Import the plugin ``main`` as a package so relative imports resolve."""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "main.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


booth_main = _load_plugin_module()
Main = booth_main.Main


class FakeContext:
    """Minimal stand-in for star.Context: records sends and web API routes."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.routes: list[str] = []

    def register_web_api(self, route: str, handler, methods: list[str], desc: str) -> None:
        self.routes.append(route)

    async def send_message(self, target: str, chain) -> bool:
        self.sent.append((target, chain))
        return True


def _items(prefix: str, count: int, free: bool) -> list[dict]:
    return [
        {
            "id": (1 if free else 2) * 1000 + i,
            "title": f"{prefix} {i}",
            "category": "3D衣装",
            "price": 0 if free else 1000 + i,
            "likes": 100 - i,
            "is_free": free,
        }
        for i in range(count)
    ]


def _make_plugin(
    config: dict, tmp_path: Path, crawl_result: dict
) -> tuple[Main, FakeContext, list]:
    """Build a Main instance wired to in-memory KV and stubbed crawl/targets."""
    booth_main.get_astrbot_plugin_data_path = lambda: str(tmp_path)
    context = FakeContext()
    plugin = Main(context, config)  # type: ignore[arg-type]

    kv: dict = {}
    plugin.get_kv_data = lambda key, default=None: _kv_get(kv, key, default)
    plugin.put_kv_data = lambda key, value: _kv_put(kv, key, value)
    crawl_calls: list = []

    async def fake_crawl_result():
        crawl_calls.append(1)
        return True, "抓取完成。", crawl_result

    async def fake_all_targets() -> list[str]:
        return ["test:Target:1"]

    plugin._crawl_result = fake_crawl_result
    plugin._all_targets = fake_all_targets
    return plugin, context, crawl_calls


async def _kv_get(kv: dict, key: str, default=None):
    return kv.get(key, default)


async def _kv_put(kv: dict, key: str, value) -> None:
    kv[key] = value


def images_dir(tmp_path: Path) -> Path:
    return tmp_path / "astrbot_plugin_booth_push" / "images"


def _base_config(tmp_path: Path) -> dict:
    return {
        "push_free": True,
        "push_paid": True,
        "enable_translation": False,
        "send_text": False,
        "http_timeout": 5,
        "http_proxy": "",
        "font_path": "",
        "text_footer": "",
        "enabled_categories": {"3D衣装": True},
        "category_quota": {"3D衣装": 3},
    }


def _crawl_result() -> dict:
    return {
        "added": 6,
        "items_by_category": {
            "3D衣装": _items("無料アイテム", 3, free=True) + _items("有料アイテム", 3, free=False),
        },
    }


def _images(chain) -> list:
    return [component for component in chain.chain if isinstance(component, Image)]


def test_both_switches_send_two_images_in_one_chain(tmp_path: Path) -> None:
    plugin, context, crawl_calls = _make_plugin(_base_config(tmp_path), tmp_path, _crawl_result())
    ok, message = asyncio.run(plugin.run_daily())

    assert ok, message
    assert len(context.sent) == 1
    target, chain = context.sent[0]
    assert target == "test:Target:1"
    images = _images(chain)
    assert len(images) == 2
    images_dir = tmp_path / "astrbot_plugin_booth_push" / "images"
    free_png = images_dir / "daily_free.png"
    paid_png = images_dir / "daily_paid.png"
    assert free_png.is_file() and paid_png.is_file()
    assert "2 张长图" in message
    # Both sections are marked seen so the next run does not repeat them.
    assert set(plugin_get_seen(plugin)) == {"3D衣装"}
    assert crawl_calls


def plugin_get_seen(plugin: Main) -> list:
    kv_value = asyncio.run(plugin.get_kv_data("seen_ids", {}))
    return list(kv_value.keys())


def test_free_switch_off_sends_only_paid_image(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    config["push_free"] = False
    plugin, context, _ = _make_plugin(config, tmp_path, _crawl_result())
    ok, message = asyncio.run(plugin.run_daily())

    assert ok, message
    assert len(context.sent) == 1
    images = _images(context.sent[0][1])
    assert len(images) == 1
    assert not (images_dir(tmp_path) / "daily_free.png").exists()
    assert (images_dir(tmp_path) / "daily_paid.png").is_file()
    assert "付费 3 件" in message
    # Free items stay unmarked so they are not silently dropped forever.
    seen = asyncio.run(plugin.get_kv_data("seen_ids", {}))
    seen_ids = {int(value) for value in seen.get("3D衣装", [])}
    assert seen_ids and all(item_id >= 2000 for item_id in seen_ids)


def test_paid_switch_off_sends_only_free_image(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    config["push_paid"] = False
    plugin, context, _ = _make_plugin(config, tmp_path, _crawl_result())
    ok, message = asyncio.run(plugin.run_daily())

    assert ok, message
    assert len(context.sent) == 1
    images = _images(context.sent[0][1])
    assert len(images) == 1
    assert (images_dir(tmp_path) / "daily_free.png").is_file()
    assert not (images_dir(tmp_path) / "daily_paid.png").exists()
    assert "免费 3 件" in message


def test_both_switches_off_rejects_before_crawl(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    config["push_free"] = False
    config["push_paid"] = False
    plugin, context, crawl_calls = _make_plugin(config, tmp_path, _crawl_result())
    ok, message = asyncio.run(plugin.run_daily())

    assert not ok
    assert "均已关闭" in message
    assert not crawl_calls
    assert not context.sent


def test_enabled_switch_without_items_sends_single_remaining_image(tmp_path: Path) -> None:
    result = {"added": 3, "items_by_category": {"3D衣装": _items("有料アイテム", 3, free=False)}}
    plugin, context, _ = _make_plugin(_base_config(tmp_path), tmp_path, result)
    ok, message = asyncio.run(plugin.run_daily())

    assert ok, message
    images = _images(context.sent[0][1])
    assert len(images) == 1
    assert "免费 0 件、付费 3 件" in message
