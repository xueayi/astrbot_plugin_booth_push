"""AstrBot plugin that crawls Booth.pm and pushes daily new items."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.web import request as plugin_request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import crawler
from .fetcher import download_thumb
from .image_grid import CATEGORY_ZH, build_long_image
from .renderer import render_text
from .translator import translate_titles

PLUGIN_NAME = "astrbot_plugin_booth_push"
UPDATE_JOB_NAME = "booth_push_daily_update"
PUSH_JOB_NAME = "booth_push_daily_push"
# Booth item IDs grow monotonically, so keeping the largest N per category
# keeps the most recent history while bounding KV storage growth.
MAX_SEEN_PER_CATEGORY = 2000


class Main(star.Star):
    """Plugin lifecycle, crawling, scheduling, and push orchestration."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.thumb_dir = self.data_dir / "cache" / "thumbs"
        self.font_dir = self.data_dir / "fonts"
        self.image_dir = self.data_dir / "images"
        self._job_ids: list[str] = []
        self._startup_task: asyncio.Task | None = None
        self._register_web_apis()

    @filter.on_astrbot_loaded()
    async def on_loaded(self, *args, **kwargs) -> None:
        """Register the daily job and optionally run a first crawl."""
        try:
            await self._register_crons()
        except Exception:
            self.logger.exception("Booth cron registration failed")
        if self.config.get("startup_update", True):
            self._startup_task = asyncio.create_task(self._startup_update())

    async def terminate(self) -> None:
        """Remove scheduled jobs and cancel any pending startup crawl."""
        for job_id in self._job_ids:
            await self.context.cron_manager.delete_job(job_id)
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()

    @filter.command_group("booth")
    def booth_group(self):
        """Command group for Booth push management."""

    @booth_group.command("bind")
    async def cmd_bind(self, event: AstrMessageEvent) -> None:
        """Bind the current session as a push target."""
        umo = event.unified_msg_origin
        targets = await self.get_kv_data("targets", [])
        if not isinstance(targets, list):
            targets = []
        if umo in targets:
            yield event.plain_result("当前会话已绑定过推送目标。")
            return
        targets.append(umo)
        await self.put_kv_data("targets", targets)
        yield event.plain_result("当前会话已绑定为推送目标。")

    @booth_group.command("unbind")
    async def cmd_unbind(self, event: AstrMessageEvent) -> None:
        """Remove the current session from push targets."""
        umo = event.unified_msg_origin
        targets = await self.get_kv_data("targets", [])
        if umo in targets:
            targets.remove(umo)
            await self.put_kv_data("targets", targets)
            yield event.plain_result("已解除当前会话绑定。")
        else:
            yield event.plain_result("当前会话未绑定。")

    @booth_group.command("update")
    async def cmd_update(self, event: AstrMessageEvent) -> None:
        """Run one crawl without pushing."""
        ok, message = await self.run_update()
        yield event.plain_result(message if ok else f"更新失败：{message}")

    @booth_group.command("push")
    async def cmd_push(self, event: AstrMessageEvent) -> None:
        """Crawl first, then push the daily selection."""
        ok, message = await self.run_daily()
        yield event.plain_result(message if ok else f"推送未完成：{message}")

    @booth_group.command("status")
    async def cmd_status(self, event: AstrMessageEvent) -> None:
        """Show crawl, push, provider, and scheduling state."""
        jobs = await self.context.cron_manager.list_jobs("basic")
        registered = any(job.name == PUSH_JOB_NAME and job.enabled for job in jobs)
        last_update_at = await self.get_kv_data("last_update_at", "")
        last_push_at = await self.get_kv_data("last_push_at", "")
        provider_id = await self._translation_provider_id()
        quota = self._category_quota()
        quota_text = "、".join(
            f"{CATEGORY_ZH.get(name, name)} {count}" for name, count in quota.items()
        )
        targets = await self.get_kv_data("targets", [])
        yield event.plain_result(
            "Booth 推送状态\n"
            f"定时：{'已注册' if registered else '未注册'} {self.config.get('daily_cron', '')}\n"
            f"配额：{quota_text or '未配置'}\n"
            f"翻译：{provider_id or '未找到可用模型'}\n"
            f"上次爬取：{last_update_at or '尚未爬取'}\n"
            f"KV 目标：{len(targets)}\n"
            f"上次推送：{last_push_at or '尚未推送'}"
        )

    @booth_group.command("check")
    async def cmd_check(self, event: AstrMessageEvent) -> None:
        """Verify that the configured translation provider is usable."""
        provider_id = await self._translation_provider_id()
        if not provider_id:
            yield event.plain_result("未找到可用翻译模型，请在 WebUI 中配置 Provider。")
            return

        sample_title = "学園セーラー服 制服 JK"
        translations = await translate_titles(
            self.context,
            provider_id,
            [{"id": 0, "title": sample_title}],
            self.get_kv_data,
            self.put_kv_data,
        )
        translated = translations.get(0, "")
        if translated and translated != sample_title:
            yield event.plain_result(f"翻译通路正常：{translated}")
        else:
            yield event.plain_result("翻译失败，请查看 AstrBot 日志中的详细错误。")

    def _register_web_apis(self) -> None:
        """Register dashboard page APIs used by the plugin control page."""
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/update",
            self.api_update,
            ["POST"],
            "手动拉取 Booth 更新",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/push",
            self.api_push,
            ["POST"],
            "手动抓取并推送 Booth 商品",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/status",
            self.api_status,
            ["GET"],
            "查询 Booth 插件状态",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/targets",
            self.api_manage_targets,
            ["POST"],
            "动态管理 Booth 推送订阅",
        )

    async def api_update(self) -> dict:
        """Manual update endpoint for the plugin page."""
        self.logger.info("Manual Booth update requested from dashboard page")
        ok, message = await self.run_update()
        return {"status": "ok" if ok else "error", "message": message}

    async def api_push(self) -> dict:
        """Manual crawl-and-push endpoint for the plugin page."""
        self.logger.info("Manual Booth push requested from dashboard page")
        ok, message = await self.run_daily()
        return {"status": "ok" if ok else "error", "message": message}

    async def api_status(self) -> dict:
        """Return current plugin and subscription status."""
        jobs = await self.context.cron_manager.list_jobs("basic")
        provider_id = await self._translation_provider_id()
        quota = self._category_quota()
        configured = [
            str(target).strip()
            for target in self.config.get("push_targets", [])
            if str(target).strip()
        ]
        bound = await self.get_kv_data("targets", [])
        if not isinstance(bound, list):
            bound = []
        return {
            "status": "ok",
            "cron_registered": any(job.name == PUSH_JOB_NAME and job.enabled for job in jobs),
            "daily_cron": self.config.get("daily_cron", ""),
            "provider_id": provider_id,
            "quota": quota,
            "configured_targets": configured,
            "bound_targets": bound,
            "last_update_at": await self.get_kv_data("last_update_at", ""),
            "last_push_at": await self.get_kv_data("last_push_at", ""),
        }

    async def api_manage_targets(self) -> dict:
        """Add or remove a dynamic push subscription from the plugin page."""
        payload = await plugin_request.json({})
        if not isinstance(payload, dict):
            return {"status": "error", "message": "请求格式错误。"}
        umo = str(payload.get("umo") or "").strip()
        action = str(payload.get("action") or "").strip()
        if not umo:
            return {"status": "error", "message": "umo 不能为空。"}
        existing = await self.get_kv_data("targets", [])
        if not isinstance(existing, list):
            existing = []
        if action == "add" and umo not in existing:
            existing.append(umo)
            await self.put_kv_data("targets", existing)
            return {"status": "ok", "message": "已添加订阅。", "targets": existing}
        if action == "remove" and umo in existing:
            existing.remove(umo)
            await self.put_kv_data("targets", existing)
            return {"status": "ok", "message": "已移除订阅。", "targets": existing}
        return {"status": "ok", "message": "无需变更。", "targets": existing}

    async def _all_targets(self) -> list[str]:
        """Return the union of configured and KV-bound push targets."""
        configured = [
            str(target).strip()
            for target in self.config.get("push_targets", [])
            if str(target).strip()
        ]
        bound = await self.get_kv_data("targets", [])
        if not isinstance(bound, list):
            bound = []
        return list(dict.fromkeys([*configured, *(str(item) for item in bound)]))

    async def _startup_update(self) -> None:
        """Delayed first crawl so plugin loading is not blocked."""
        await asyncio.sleep(15)
        ok, message = await self.run_update()
        if not ok:
            self.logger.error("Startup Booth crawl failed: %s", message)

    async def _register_crons(self) -> None:
        """Replace stale plugin jobs and register the daily update/push jobs."""
        for job in await self.context.cron_manager.list_jobs():
            if job.name in (UPDATE_JOB_NAME, PUSH_JOB_NAME):
                await self.context.cron_manager.delete_job(job.job_id)

        push_job = await self.context.cron_manager.add_basic_job(
            name=PUSH_JOB_NAME,
            cron_expression=str(self.config.get("daily_cron", "0 8 * * *")),
            handler=self._daily_handler,
            description="Booth crawl and push",
            timezone=str(self.config.get("timezone", "") or "") or None,
            persistent=False,
        )
        self._job_ids = [push_job.job_id]
        self.logger.info(
            "Booth cron job registered: %s (cron=%s)",
            push_job.job_id[:8],
            push_job.cron_expression,
        )

    async def _daily_handler(self) -> None:
        """Run the scheduled crawl and push."""
        ok, message = await self.run_daily()
        if not ok and message != "没有新的商品。":
            self.logger.error("Scheduled Booth daily run failed: %s", message)

    def _category_quota(self) -> dict[str, int]:
        """Read and normalize configured per-category quotas."""
        raw_quota = self.config.get("category_quota", {})
        if not isinstance(raw_quota, dict):
            return {}
        quota: dict[str, int] = {}
        for category, count in raw_quota.items():
            try:
                normalized = max(0, int(count))
            except (TypeError, ValueError):
                normalized = 0
            if normalized:
                quota[str(category)] = normalized
        return quota

    def _window_start(self, last_push_at: str) -> str:
        """Return the UTC ISO lower bound for the current crawl window."""
        if not last_push_at:
            boundary = datetime.now(timezone.utc) - timedelta(hours=24)
        else:
            try:
                boundary = datetime.fromisoformat(str(last_push_at))
                if boundary.tzinfo is None:
                    boundary = boundary.replace(tzinfo=timezone.utc)
                boundary = boundary.astimezone(timezone.utc)
            except ValueError:
                boundary = datetime.now(timezone.utc) - timedelta(hours=24)
        return boundary.isoformat()

    async def _translation_provider_id(self) -> str:
        """Resolve the explicit provider or AstrBot's current default model."""
        provider_id = str(self.config.get("llm_provider", ""))
        if provider_id:
            return provider_id
        provider = await self.context.get_using_provider_async()
        return provider.meta().id if provider else ""

    async def _seen_ids(self) -> dict[str, list[int]]:
        """Load the persisted seen-ID mapping from plugin KV storage."""
        mapping = await self.get_kv_data("seen_ids", {})
        if not isinstance(mapping, dict):
            mapping = {}
        clean: dict[str, list[int]] = {}
        for category, values in mapping.items():
            ids: list[int] = []
            for value in values if isinstance(values, list) else []:
                try:
                    ids.append(int(value))
                except (TypeError, ValueError):
                    continue
            if ids:
                clean[str(category)] = ids
        return clean

    async def _crawl_result(self) -> tuple[bool, str, dict[str, Any]]:
        """Run a crawler pass and return a normalized result."""
        quota = self._category_quota()
        if not quota:
            return False, "未配置有效的类目配额。", {}
        seen_mapping = await self._seen_ids()
        seen: set[int] = set()
        for ids in seen_mapping.values():
            seen.update(ids)
        last_push_at = await self.get_kv_data("last_push_at", "")
        since = self._window_start(str(last_push_at or ""))
        try:
            result = await asyncio.to_thread(
                crawler.crawl_new,
                list(quota),
                seen,
                since,
                int(self.config.get("crawl_pages", 5) or 5),
                int(self.config.get("crawl_workers", 4) or 4),
                float(self.config.get("crawl_delay", 0.3) or 0.3),
                float(self.config.get("http_timeout", 30) or 30),
                str(self.config.get("http_proxy", "") or ""),
            )
        except Exception as exc:
            self.logger.error("Booth crawl failed: %s", exc)
            return False, "Booth 抓取失败，请查看日志。", {}
        await self.put_kv_data("last_update_at", datetime.now(timezone.utc).isoformat())
        return True, "抓取完成。", result

    async def run_update(self) -> tuple[bool, str]:
        """Run one crawl without pushing."""
        ok, message, result = await self._crawl_result()
        if not ok:
            return False, message
        added = int(result.get("added", 0))
        return True, f"抓取完成，发现 {added} 个新商品。"

    async def run_daily(self) -> tuple[bool, str]:
        """Crawl once, then push every new item to the configured targets."""
        ok, message, result = await self._crawl_result()
        if not ok:
            return False, message

        quota = self._category_quota()
        free_items: list[dict[str, Any]] = []
        paid_items: list[dict[str, Any]] = []
        for category, category_quota in quota.items():
            category_items = result.get("items_by_category", {}).get(category, [])
            free_items.extend(
                [item for item in category_items if item.get("is_free")][:category_quota]
            )
            paid_items.extend(
                [item for item in category_items if not item.get("is_free")][:category_quota]
            )
        free_items.sort(key=lambda item: int(item.get("likes") or 0), reverse=True)
        paid_items.sort(key=lambda item: int(item.get("likes") or 0), reverse=True)
        if not free_items and not paid_items:
            return False, "没有新的商品。"

        all_items = free_items + paid_items
        translations: dict[int, str] = {}
        translation_attempted = False
        if self.config.get("enable_translation", True):
            provider_id = await self._translation_provider_id()
            if provider_id:
                translation_attempted = True
                translations = await translate_titles(
                    self.context,
                    provider_id,
                    all_items,
                    self.get_kv_data,
                    self.put_kv_data,
                )

        timeout = int(self.config.get("http_timeout", 30) or 30)
        proxy = str(self.config.get("http_proxy", "") or "")
        thumbnails = await asyncio.gather(
            *(download_thumb(item, self.thumb_dir, timeout, proxy) for item in all_items)
        )
        for item, thumbnail in zip(all_items, thumbnails):
            item["thumb_path"] = str(thumbnail) if thumbnail else ""

        image_path = self.image_dir / "daily.png"
        try:
            # PIL rendering and possible font download are blocking; keep them
            # off the event loop so the whole bot does not stall.
            await asyncio.to_thread(
                build_long_image,
                free_items,
                paid_items,
                translations,
                image_path,
                font_path=str(self.config.get("font_path", "")),
                font_cache_dir=self.font_dir,
            )
        except Exception as exc:
            self.logger.error("Booth long image rendering failed: %s", exc)
            return False, "长图渲染失败。"

        targets = await self._all_targets()
        if not targets:
            return False, "没有配置推送目标。"

        message_chain = (
            MessageChain()
            .file_image(str(image_path))
            .message(
                render_text(
                    free_items,
                    paid_items,
                    translations,
                    footer=str(self.config.get("text_footer", "") or ""),
                )
            )
        )
        sent_count = 0
        for target in targets:
            try:
                sent = await self.context.send_message(target, message_chain)
                if sent:
                    sent_count += 1
            except Exception as exc:
                self.logger.error("Booth push to %s failed: %s", target, exc)
        if sent_count == 0:
            return False, "推送失败：所有目标均发送失败，本次内容将在下轮重试。"

        # At least one target succeeded: record the push point so successful
        # targets do not receive duplicates on the next run; failed targets
        # intentionally miss this batch (documented in the README).
        await self._mark_seen(quota, all_items)
        await self.put_kv_data(
            "last_push_at",
            datetime.now(timezone.utc).isoformat(),
        )
        if sent_count < len(targets):
            self.logger.warning(
                "Booth push partially succeeded: %d/%d targets",
                sent_count,
                len(targets),
            )
            return False, (
                f"推送部分成功：{sent_count}/{len(targets)} 个目标，未送达的目标本次不再补推。"
            )
        translated_count = sum(1 for item in all_items if translations.get(item.get("id")))
        translation_note = (
            "（翻译失败，保留日文标题）" if translation_attempted and translated_count == 0 else ""
        )
        return True, (
            f"已推送 {len(all_items)} 个商品到 {len(targets)} 个目标。" + translation_note
        )

    async def _mark_seen(
        self,
        quota: dict[str, int],
        all_items: list[dict[str, Any]],
    ) -> None:
        """Persist item IDs after a successful push so future runs skip them.

        The per-category list is capped at ``MAX_SEEN_PER_CATEGORY`` (newest
        IDs win) to keep the KV record bounded.
        """
        mapping = await self._seen_ids()
        for category, _count in quota.items():
            existing = set(mapping.get(category, []))
            existing.update(
                int(item["id"]) for item in all_items if item.get("category") == category
            )
            if existing:
                mapping[category] = sorted(existing)[-MAX_SEEN_PER_CATEGORY:]
        await self.put_kv_data("seen_ids", mapping)
