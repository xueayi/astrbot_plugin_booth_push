"""Render the plain-text daily push."""

from __future__ import annotations

from typing import Any

from .image_grid import CATEGORY_ZH


def render_text(
    free_items: list[dict[str, Any]],
    paid_items: list[dict[str, Any]],
    translations: dict[int, str],
) -> str:
    """Build the text message that accompanies the long image.

    Args:
        free_items: Items selected for the free section.
        paid_items: Items selected for the paid section.
        translations: Chinese titles keyed by item ID.

    Returns:
        A non-empty plain text block.
    """
    lines: list[str] = []
    for label, items in (("免费上新", free_items), ("付费上新", paid_items)):
        if not items:
            continue
        lines.append(label)
        for item in items:
            item_id = item.get("id", 0)
            title = str(item.get("title") or "")
            translated = translations.get(item_id, "")
            raw_category = str(item.get("category", ""))
            category = CATEGORY_ZH.get(raw_category, raw_category)
            lines.append(f"[{category}] {title}")
            if translated and translated != title:
                lines.append(translated)
            lines.append(str(item.get("item_url") or ""))
            lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    lines.extend(
        [
            "",
            "数据来源：https://github.com/xueayi/astrbot_plugin_booth_push",
        ]
    )
    return "\n".join(lines)
