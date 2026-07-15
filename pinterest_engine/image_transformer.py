# -*- coding: utf-8 -*-
"""
pinterest_engine.image_transformer
-----------------------------------
Transforms existing 3:4 library assets into 1000x1500 (2:3) Pinterest Sales Pins.

Visual layout (top to bottom):
  - Hook zone (top 30%): dark vignette + VISUAL HOOK text in large serif font
  - Photo zone (middle): the library image, center-cropped or blurred-padded
  - Button zone (bottom 17%): dark bar with "DOWNLOAD THE PROTOCOL" + URL

Usage:
    from pinterest_engine.image_transformer import PinTransformer
    transformer = PinTransformer()
    pin_path = transformer.transform(record)   # record = parsed library JSON dict
"""
from __future__ import annotations

import io
import logging
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pin dimensions (Pinterest 2:3 standard)
# ---------------------------------------------------------------------------
PIN_W = 1000
PIN_H = 1500

# Colour palette (earth-toned, clinical-clean)
_VIGNETTE_COLOUR = (20, 18, 15, 180)       # dark warm translucent (RGBA)
_HOOK_TEXT_COLOUR = (255, 251, 245)         # off-white
_BUTTON_BG_COLOUR = (44, 62, 44, 230)      # deep forest green (RGBA)
_BUTTON_TEXT_COLOUR = (230, 220, 195)       # warm cream
_URL_TEXT_COLOUR = (200, 195, 180)          # muted cream
_DIVIDER_COLOUR = (180, 160, 110, 200)      # warm gold (RGBA)

# Layout zones (fractions of PIN_H)
_HOOK_ZONE_TOP_FRAC = 0.04
_HOOK_ZONE_BOT_FRAC = 0.30
_BUTTON_ZONE_TOP_FRAC = 0.83

# Sales URL and button label
_SALES_URL = "http://blueprint.holisticprotocolslab.com/"
_BUTTON_LABEL = "DOWNLOAD THE PROTOCOL  ->"

# Output subdirectory under outputs/
_PINS_SUBDIR = "pinterest_pins"

# ---------------------------------------------------------------------------
# Font resolution (Windows serif/sans stack with fallbacks)
# ---------------------------------------------------------------------------
_SERIF_CANDIDATES = [
    r"C:\Windows\Fonts\georgia.ttf",
    r"C:\Windows\Fonts\georgiai.ttf",
    r"C:\Windows\Fonts\palatia.ttf",
    r"C:\Windows\Fonts\times.ttf",
    r"C:\Windows\Fonts\timesbd.ttf",
]

_SANS_CANDIDATES = [
    r"C:\Windows\Fonts\calibri.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def _find_font(
    candidates: list[str], size: int
) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Canvas helpers
# ---------------------------------------------------------------------------

def _center_crop_to_size(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale and center-crop to exactly w x h with no distortion."""
    src_w, src_h = img.size
    ratio_target = w / h
    ratio_src = src_w / src_h

    if ratio_src > ratio_target:
        new_h = h
        new_w = int(src_w * h / src_h)
    else:
        new_w = w
        new_h = int(src_h * w / src_w)

    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


def _blurred_padding(img: Image.Image, w: int, h: int) -> Image.Image:
    """
    Fit image inside w x h without cropping.
    Fill letterbox/pillarbox areas with a Gaussian-blurred, dimmed background.
    """
    src_w, src_h = img.size
    scale = min(w / src_w, h / src_h)
    fit_w = int(src_w * scale)
    fit_h = int(src_h * scale)

    # Background: stretch to fill, blur, darken
    bg = img.resize((w, h), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
    bg = bg.convert("RGBA")
    dark = Image.new("RGBA", (w, h), (0, 0, 0, 120))
    bg = Image.alpha_composite(bg, dark).convert("RGB")

    # Foreground: sharp, scaled to fit
    fg = img.resize((fit_w, fit_h), Image.LANCZOS)
    x_off = (w - fit_w) // 2
    y_off = (h - fit_h) // 2
    bg.paste(fg, (x_off, y_off))
    return bg


def _draw_hook_zone(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    hook_text: str,
) -> None:
    """Overlay dark vignette + centered hook text in the top zone."""
    zone_top = int(PIN_H * _HOOK_ZONE_TOP_FRAC)
    zone_bot = int(PIN_H * _HOOK_ZONE_BOT_FRAC)
    zone_h = zone_bot - zone_top

    # Semi-transparent dark panel
    panel = Image.new("RGBA", (PIN_W, zone_h), _VIGNETTE_COLOUR)
    # Fade out at the bottom 60px of the panel
    for row in range(max(0, zone_h - 60), zone_h):
        fade = int(_VIGNETTE_COLOUR[3] * (1 - (row - (zone_h - 60)) / 60))
        for col in range(PIN_W):
            panel.putpixel((col, row), (*_VIGNETTE_COLOUR[:3], fade))

    canvas.paste(panel, (0, zone_top), panel)

    # Hook text wrapped to ~22 chars per line
    hook_clean = hook_text.strip().upper()
    lines = textwrap.wrap(hook_clean, width=22) or [hook_clean[:22]]

    font_large = _find_font(_SERIF_CANDIDATES, 64)
    line_height = 78
    total_h = len(lines) * line_height
    cursor_y = zone_top + (zone_h - total_h) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font_large)
        text_w = bbox[2] - bbox[0]
        x = (PIN_W - text_w) // 2
        # Drop shadow
        draw.text((x + 2, cursor_y + 2), line, font=font_large, fill=(0, 0, 0, 180))
        draw.text((x, cursor_y), line, font=font_large, fill=_HOOK_TEXT_COLOUR)
        cursor_y += line_height

    # Gold divider line
    div_y = zone_bot - 2
    draw.line([(60, div_y), (PIN_W - 60, div_y)], fill=_DIVIDER_COLOUR[:3], width=2)


def _draw_button_zone(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
) -> None:
    """Overlay the dark CTA button strip at the bottom of the pin."""
    zone_top = int(PIN_H * _BUTTON_ZONE_TOP_FRAC)
    zone_h = PIN_H - zone_top

    btn_panel = Image.new("RGBA", (PIN_W, zone_h), _BUTTON_BG_COLOUR)
    canvas.paste(btn_panel, (0, zone_top), btn_panel)

    font_btn = _find_font(_SERIF_CANDIDATES, 36)
    font_url = _find_font(_SANS_CANDIDATES, 20)

    # Top border line
    draw.line([(0, zone_top), (PIN_W, zone_top)], fill=_DIVIDER_COLOUR[:3], width=3)

    # Button label
    bbox = draw.textbbox((0, 0), _BUTTON_LABEL, font=font_btn)
    btn_w = bbox[2] - bbox[0]
    btn_y = zone_top + 28
    draw.text(
        ((PIN_W - btn_w) // 2, btn_y),
        _BUTTON_LABEL,
        font=font_btn,
        fill=_BUTTON_TEXT_COLOUR,
    )

    # URL
    url_bbox = draw.textbbox((0, 0), _SALES_URL, font=font_url)
    url_w = url_bbox[2] - url_bbox[0]
    draw.text(
        ((PIN_W - url_w) // 2, btn_y + 52),
        _SALES_URL,
        font=font_url,
        fill=_URL_TEXT_COLOUR,
    )


# ---------------------------------------------------------------------------
# PinTransformer
# ---------------------------------------------------------------------------

class PinTransformer:
    """
    Converts a library record dict into a 1000x1500 Pinterest Sales Pin JPEG.

    Parameters
    ----------
    outputs_dir : Path, optional
        Root outputs/ directory. Defaults to config.OUTPUTS_DIR.
    method : str
        'blurred_padding' (default) or 'center_crop'.
    """

    def __init__(
        self,
        outputs_dir: "Path | None" = None,
        method: str = "blurred_padding",
    ) -> None:
        if outputs_dir is None:
            import sys
            _root = Path(__file__).resolve().parents[1]
            if str(_root) not in sys.path:
                sys.path.insert(0, str(_root))
            import config as _cfg  # noqa: PLC0415
            outputs_dir = _cfg.OUTPUTS_DIR

        self.pins_dir: Path = outputs_dir / _PINS_SUBDIR
        self.pins_dir.mkdir(parents=True, exist_ok=True)
        self.method = method

    # ------------------------------------------------------------------
    # Public API

    def transform(self, record: dict) -> "Path | None":
        """
        Build the 2:3 pin for a single library record.
        Returns the saved pin Path, or None if the source image is missing.
        """
        img_path = record.get("local_image_path", "")
        imgbb_url = record.get("imgbb_url", "")
        topic = record.get("topic", "Holistic Protocol")
        visual_hook = record.get("visual_hook") or topic
        subject_slug = record.get("subject_slug", "pin")
        variant = record.get("variant_index", 0)

        source = self._load_source(img_path, imgbb_url)
        if source is None:
            log.warning("No source image for: %s v%s", topic, variant)
            return None

        pin = self._compose(source, visual_hook)

        filename = f"{subject_slug}_v{variant:02d}_pin.jpg"
        out_path = self.pins_dir / filename
        pin.save(str(out_path), format="JPEG", quality=92, optimize=True)
        log.info("Pin saved: %s", out_path.name)
        return out_path

    def transform_batch(
        self, records: list[dict]
    ) -> dict[str, "Path | None"]:
        """Process multiple records. Returns {slug_vN: Path | None}."""
        out: dict[str, "Path | None"] = {}
        for rec in records:
            key = f"{rec.get('subject_slug', 'pin')}_v{rec.get('variant_index', 0):02d}"
            out[key] = self.transform(rec)
        return out

    def get_pin_bytes(self, record: dict) -> bytes | None:
        """
        Transform a record and return the JPEG bytes in memory (no disk write).
        Used by the publisher to send base64-encoded image to Pinterest API.
        """
        img_path = record.get("local_image_path", "")
        imgbb_url = record.get("imgbb_url", "")
        visual_hook = record.get("visual_hook") or record.get("topic", "Holistic Protocol")

        source = self._load_source(img_path, imgbb_url)
        if source is None:
            return None

        pin = self._compose(source, visual_hook)
        buf = io.BytesIO()
        pin.save(buf, format="JPEG", quality=92, optimize=True)
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Private

    def _load_source(self, local_path: str, imgbb_url: str) -> "Image.Image | None":
        if local_path and Path(local_path).is_file():
            try:
                return Image.open(local_path).convert("RGB")
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot open %s: %s", local_path, exc)

        if imgbb_url and imgbb_url.startswith("http"):
            try:
                import urllib.request  # noqa: PLC0415
                with urllib.request.urlopen(imgbb_url, timeout=15) as resp:
                    data = resp.read()
                return Image.open(io.BytesIO(data)).convert("RGB")
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot fetch %s: %s", imgbb_url, exc)

        return None

    def _compose(self, source: Image.Image, visual_hook: str) -> Image.Image:
        """Assemble the final 1000x1500 canvas."""
        if self.method == "center_crop":
            canvas = _center_crop_to_size(source, PIN_W, PIN_H)
        else:
            canvas = _blurred_padding(source, PIN_W, PIN_H)

        canvas = canvas.convert("RGBA")
        draw = ImageDraw.Draw(canvas)

        _draw_hook_zone(canvas, draw, visual_hook)
        _draw_button_zone(canvas, draw)

        return canvas.convert("RGB")


# ---------------------------------------------------------------------------
# Quick standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys
    _root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_root))
    import config as cfg

    files = sorted(cfg.LIBRARY_DIR.glob("post_*.json"))
    if not files:
        print("No library records found.")
        sys.exit(1)

    for f in reversed(files):
        rec = json.loads(f.read_text(encoding="utf-8"))
        if rec.get("local_image_path") or rec.get("imgbb_url"):
            t = PinTransformer()
            result = t.transform(rec)
            print(f"Pin created: {result}")
            break
    else:
        print("No records with image paths found.")
