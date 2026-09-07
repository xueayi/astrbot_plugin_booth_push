"""Render the daily Booth items as a two-column long image."""

from __future__ import annotations

import math
import re
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from astrbot.api import logger

# Pinned to a noto-cjk release tag so the downloaded glyphs never change
# under us; @main is mutable.
FONT_URL = (
    "https://cdn.jsdelivr.net/gh/googlefonts/noto-cjk@Sans2.004/"
    "Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"
)
FONT_TIMEOUT_SECONDS = 60
FONT_CANDIDATES = [
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
]
# Glyph sets the daily image needs: CJK, kana and Western European accented
# letters. Candidates missing any of these are skipped so Spanish/Japanese
# text does not render as tofu.
FONT_COVERAGE_PROBE = "中文テストñáéíóúü¿¡"
UNSUPPORTED_SYMBOL_RE = re.compile(
    "[\U0001f000-\U0001fbff\u2600-\u27bf\u2b00-\u2bff\ufe00-\ufe0f\u200d\u00a9\u00ae\u2122]+"
)
CANVAS_WIDTH = 640
CANVAS_PAD = 18
COLS = 2
THUMB_SIZE = 240
CELL_PAD = 16
CATEGORY_LINE_HEIGHT = 24
TITLE_LINES = 2
TITLE_LINE_HEIGHT = 26
META_LINE_HEIGHT = 22
INFO_GAP = 8
HEADER_HEIGHT = 48
TOP_HEIGHT = 74
FOOTER_HEIGHT = 44
CELL_HEIGHT = (
    CELL_PAD
    + THUMB_SIZE
    + CELL_PAD
    + CATEGORY_LINE_HEIGHT
    + INFO_GAP
    + TITLE_LINES * TITLE_LINE_HEIGHT
    + INFO_GAP
    + TITLE_LINES * TITLE_LINE_HEIGHT
    + INFO_GAP
    + META_LINE_HEIGHT
    + CELL_PAD
)
CELL_WIDTH = (CANVAS_WIDTH - CANVAS_PAD * 2 - (COLS - 1) * CELL_PAD) // COLS
TEXT_WIDTH = CELL_WIDTH - CELL_PAD * 2
CATEGORY_STYLES = {
    "3D衣装": ("#2563eb", "#eff6ff"),
    "3D髪型": ("#7c3aed", "#f5f3ff"),
}
CATEGORY_ZH = {
    "3D衣装": "3D服装",
    "3D髪型": "3D发型",
}


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_lines: int,
) -> list[str]:
    """Wrap text without spaces and clip it to a fixed number of lines.

    Args:
        draw: Pillow draw context used to measure text.
        text: Text to wrap; embedded newlines are treated as spaces.
        font: Font used for measurement.
        max_lines: Maximum rendered lines.

    Returns:
        One to ``max_lines`` strings.
    """
    text = UNSUPPORTED_SYMBOL_RE.sub(" ", text)
    compact = " ".join(text.split())
    if not compact:
        return ["(無題)"]
    lines: list[str] = []
    current = ""
    for char in compact:
        candidate = current + char
        if not current or draw.textlength(candidate, font=font) <= TEXT_WIDTH:
            current = candidate
            continue
        lines.append(current)
        current = char
        if len(lines) >= max_lines:
            clipped = lines[-1]
            while clipped and draw.textlength(clipped + "…", font=font) > TEXT_WIDTH:
                clipped = clipped[:-1]
            return [*lines[:-1], clipped + "…"]
    if current:
        lines.append(current)
    if len(lines) <= max_lines:
        return lines
    return lines[:max_lines]


def _font_covers(font: ImageFont.FreeTypeFont, probe: str) -> bool:
    """Check whether a font renders every probe glyph instead of tofu.

    Args:
        font: Pillow font to inspect.
        probe: Characters that must all be renderable.

    Returns:
        True when the font covers the probe (or coverage cannot be checked).
    """
    try:
        # U+0378 is unassigned, so it renders as .notdef tofu (or blank);
        # any probe glyph matching that output is considered missing.
        blank = Image.new("L", (32, 32), 0).tobytes()
        tofu = _render_probe_char(font, chr(0x0378))
    except Exception:
        return True
    for char in probe:
        try:
            if _render_probe_char(font, char) in (tofu, blank):
                return False
        except Exception:
            return False
    return True


def _render_probe_char(font: ImageFont.FreeTypeFont, char: str) -> bytes:
    """Render one glyph onto a scratch canvas and return raw pixel bytes."""
    image = Image.new("L", (32, 32), 0)
    ImageDraw.Draw(image).text((2, 2), char, font=font, fill=255)
    return image.tobytes()


def _resolve_font(
    size: int,
    font_path: str,
    cache_dir: Path | None = None,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Resolve a CJK-capable font, downloading one when necessary.

    Candidates lacking required glyphs (e.g. limited Latin coverage) are
    skipped, which prevents accented Western text from rendering as tofu.

    Args:
        size: Font pixel size.
        font_path: Explicit font path from plugin configuration.
        cache_dir: Directory used to cache the downloaded font.

    Returns:
        A Pillow font. The default bitmap font is a final fallback.
    """
    candidates = [Path(font_path)] if font_path else []
    candidates.extend(FONT_CANDIDATES)
    for candidate in candidates:
        try:
            if candidate.is_file():
                font = ImageFont.truetype(str(candidate), size)
                if _font_covers(font, FONT_COVERAGE_PROBE):
                    return font
                logger.warning(
                    "Font %s lacks required glyphs, trying next candidate",
                    candidate,
                )
        except Exception as exc:
            logger.warning("Unable to load font %s: %s", candidate, exc)

    if cache_dir is not None:
        cached_font = cache_dir / "NotoSansCJKsc-Regular.otf"
        try:
            if not cached_font.is_file():
                cache_dir.mkdir(parents=True, exist_ok=True)
                request = urllib.request.urlopen(FONT_URL, timeout=FONT_TIMEOUT_SECONDS)
                try:
                    temporary = cached_font.with_name(cached_font.name + ".tmp")
                    with temporary.open("wb") as handle:
                        while chunk := request.read(1024 * 1024):
                            handle.write(chunk)
                    temporary.replace(cached_font)
                finally:
                    request.close()
            font = ImageFont.truetype(str(cached_font), size)
            if _font_covers(font, FONT_COVERAGE_PROBE):
                return font
            # A cached font that fails the probe is broken or incomplete.
            logger.warning("Cached font %s failed the glyph probe, re-downloading", cached_font)
            cached_font.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Unable to prepare cached CJK font: %s", exc)

    logger.warning("No CJK font found; Chinese text may render as boxes.")
    return ImageFont.load_default()


def build_long_image(
    free_items: list[dict[str, Any]],
    paid_items: list[dict[str, Any]],
    translations: dict[int, str],
    out_path: Path,
    font_path: str = "",
    font_cache_dir: Path | None = None,
) -> Path:
    """Render free and paid sections into a two-column PNG.

    Args:
        free_items: Items selected for the free section.
        paid_items: Items selected for the paid section.
        translations: Chinese titles keyed by item ID.
        out_path: Destination PNG path.
        font_path: Explicit configured font path.
        font_cache_dir: Directory used to cache the downloaded font.

    Returns:
        The saved image path.
    """
    groups = [("免费上新", free_items), ("付费上新", paid_items)]
    section_heights = [
        HEADER_HEIGHT + math.ceil(len(items) / COLS) * CELL_HEIGHT
        for _label, items in groups
        if items
    ]
    height = (
        TOP_HEIGHT + sum(section_heights) + (len(section_heights) - 1) * CELL_PAD + FOOTER_HEIGHT
    )
    image = Image.new("RGB", (CANVAS_WIDTH, max(height, TOP_HEIGHT)), "#f3f4f6")
    draw = ImageDraw.Draw(image)
    font = _resolve_font(16, font_path, font_cache_dir)
    heading_font = _resolve_font(22, font_path, font_cache_dir)
    small_font = _resolve_font(14, font_path, font_cache_dir)
    category_font = _resolve_font(15, font_path, font_cache_dir)

    draw.rectangle((0, 0, CANVAS_WIDTH, 6), fill="#2563eb")
    draw.text((CANVAS_PAD, 24), "Booth 每日上新", fill="#111827", font=heading_font)
    draw.text(
        (CANVAS_PAD, 55),
        (f"每日部分商品推荐 · 免费 {len(free_items)} 件 · 付费 {len(paid_items)} 件 · 按收藏排序"),
        fill="#6b7280",
        font=small_font,
    )
    y = TOP_HEIGHT

    for label, items in groups:
        if not items:
            continue
        if y > TOP_HEIGHT:
            y += CELL_PAD

        accent = "#059669" if label == "免费上新" else "#2563eb"
        draw.rectangle((CANVAS_PAD, y + 14, CANVAS_PAD + 4, y + 34), fill=accent)
        draw.text(
            (CANVAS_PAD + 16, y + 12),
            f"{label} · {len(items)} 件",
            fill="#111827",
            font=heading_font,
        )
        y += HEADER_HEIGHT

        for row_start in range(0, len(items), COLS):
            for col, item in enumerate(items[row_start : row_start + COLS]):
                cell_x = CANVAS_PAD + col * (CELL_WIDTH + CELL_PAD)
                card_bottom = y + CELL_HEIGHT - CELL_PAD
                draw.rounded_rectangle(
                    (cell_x, y, cell_x + CELL_WIDTH, card_bottom),
                    radius=14,
                    fill="white",
                    outline="#e5e7eb",
                    width=2,
                )
                thumb_path_value = str(item.get("thumb_path") or "")
                thumb_path = Path(thumb_path_value) if thumb_path_value else None
                thumb_x = cell_x + (CELL_WIDTH - THUMB_SIZE) // 2
                thumb_y = y + CELL_PAD
                try:
                    if thumb_path and thumb_path.is_file():
                        with Image.open(thumb_path) as source:
                            thumbnail = ImageOps.fit(
                                source.convert("RGB"),
                                (THUMB_SIZE, THUMB_SIZE),
                                Image.Resampling.LANCZOS,
                            )
                            mask = Image.new("L", (THUMB_SIZE, THUMB_SIZE), 0)
                            ImageDraw.Draw(mask).rounded_rectangle(
                                (0, 0, THUMB_SIZE - 1, THUMB_SIZE - 1),
                                radius=16,
                                fill=255,
                            )
                            image.paste(thumbnail, (thumb_x, thumb_y), mask)
                    else:
                        draw.rounded_rectangle(
                            (
                                thumb_x,
                                thumb_y,
                                thumb_x + THUMB_SIZE,
                                thumb_y + THUMB_SIZE,
                            ),
                            radius=16,
                            fill="#f0f0f0",
                        )
                except Exception as exc:
                    logger.warning(
                        "Unable to render thumbnail for item %s: %s",
                        item.get("id", ""),
                        exc,
                    )
                    draw.rounded_rectangle(
                        (
                            thumb_x,
                            thumb_y,
                            thumb_x + THUMB_SIZE,
                            thumb_y + THUMB_SIZE,
                        ),
                        radius=16,
                        fill="#f0f0f0",
                    )

                category = str(item.get("category") or "未分类")
                category_key = category
                category = CATEGORY_ZH.get(category, category)
                category_text = UNSUPPORTED_SYMBOL_RE.sub(" ", category).strip() or "未分类"
                category_color, category_background = CATEGORY_STYLES.get(
                    category_key,
                    ("#4b5563", "#f3f4f6"),
                )
                category_width = min(
                    draw.textlength(category_text, font=category_font) + 20,
                    TEXT_WIDTH,
                )
                text_y = thumb_y + THUMB_SIZE + CELL_PAD
                draw.rounded_rectangle(
                    (
                        cell_x + CELL_PAD,
                        text_y,
                        cell_x + CELL_PAD + category_width,
                        text_y + CATEGORY_LINE_HEIGHT,
                    ),
                    radius=10,
                    fill=category_background,
                )
                draw.text(
                    (cell_x + CELL_PAD + 10, text_y + 4),
                    category_text,
                    fill=category_color,
                    font=category_font,
                )

                title = str(item.get("title") or "(無題)")
                translated = translations.get(item.get("id", 0), "")
                title_lines = _wrap_text(draw, title, font, TITLE_LINES)
                title_y = text_y + CATEGORY_LINE_HEIGHT + INFO_GAP
                if translated and translated != item.get("title", ""):
                    translated_lines = _wrap_text(draw, translated, font, TITLE_LINES)
                else:
                    translated_lines = []
                translation_y = title_y + TITLE_LINES * TITLE_LINE_HEIGHT + INFO_GAP
                for index, line in enumerate(title_lines):
                    draw.text(
                        (cell_x + CELL_PAD, title_y + index * TITLE_LINE_HEIGHT),
                        line,
                        fill="#111827",
                        font=font,
                    )
                for index, line in enumerate(translated_lines):
                    draw.text(
                        (
                            cell_x + CELL_PAD,
                            translation_y + index * TITLE_LINE_HEIGHT,
                        ),
                        line,
                        fill="#6b7280",
                        font=font,
                    )
                meta_y = translation_y + len(translated_lines) * TITLE_LINE_HEIGHT + INFO_GAP
                price = int(item.get("price") or 0)
                likes = int(item.get("likes") or 0)
                draw.text(
                    (cell_x + CELL_PAD, meta_y),
                    f"价格 ¥{price:,} · 收藏 {likes}",
                    fill="#4b5563",
                    font=small_font,
                )
            y += CELL_HEIGHT

    draw.text(
        (CANVAS_PAD, height - FOOTER_HEIGHT + 14),
        "数据来源：booth.pm",
        fill="#9ca3af",
        font=small_font,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG", optimize=True)
    return out_path
