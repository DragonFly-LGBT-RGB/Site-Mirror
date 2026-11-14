#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генерирует assets/og-image.png (1200x630) и assets/icon-512.png.

Зависимость: Pillow (нужна только для перегенерации картинки, не для сайта).
    python tools/make_og.py
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

# Адрес репозитория подставляется configure.py через files.json
REPO_LABEL = "site_mirror · GitHub Pages"
try:
    _cfg = json.loads((ROOT / "files.json").read_text(encoding="utf-8"))
    _repo = str(_cfg.get("repository") or "")
    if _repo:
        REPO_LABEL = _repo.replace("https://", "").replace("http://", "").rstrip("/")
except Exception:  # noqa: BLE001
    pass

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
SANS = FONT_DIR / "DejaVuSans.ttf"
SANS_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
MONO = FONT_DIR / "DejaVuSansMono.ttf"
MONO_BOLD = FONT_DIR / "DejaVuSansMono-Bold.ttf"


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


W, H = 1200, 630
img = Image.new("RGBA", (W, H), (10, 14, 22, 255))

# --- мягкие светящиеся пятна -------------------------------------------------
glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
g = ImageDraw.Draw(glow)
g.ellipse((580, -320, 1360, 460), fill=(79, 141, 253, 105))
g.ellipse((-300, 330, 520, 1000), fill=(124, 92, 255, 80))
g.ellipse((860, 400, 1320, 860), fill=(84, 214, 138, 40))
g.ellipse((260, -220, 700, 180), fill=(88, 101, 242, 55))
img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(115)))

# --- сетка -------------------------------------------------------------------
grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
gd = ImageDraw.Draw(grid)
for x in range(0, W, 48):
    gd.line((x, 0, x, H), fill=(255, 255, 255, 9), width=1)
for y in range(0, H, 48):
    gd.line((0, y, W, y), fill=(255, 255, 255, 9), width=1)
img = Image.alpha_composite(img, grid)

d = ImageDraw.Draw(img)

# --- верхняя акцентная полоса ------------------------------------------------
d.rectangle((0, 0, W, 6), fill=(88, 101, 242, 255))
d.rectangle((0, 0, 340, 6), fill=(79, 141, 253, 255))

# --- бейдж -------------------------------------------------------------------
d.rounded_rectangle((72, 62, 452, 112), radius=25,
                    fill=(27, 44, 76, 255), outline=(110, 165, 255, 255), width=2)
d.text((94, 77), "GITHUB PAGES  ·  OPEN SOURCE", font=font(SANS_BOLD, 20),
       fill=(178, 205, 255, 255))

# --- заголовок ---------------------------------------------------------------
d.text((70, 138), "site_mirror", font=font(MONO_BOLD, 72), fill=(242, 247, 255, 255))
d.text((72, 236), "Скачивай сайты", font=font(SANS_BOLD, 58), fill=(255, 255, 255, 255))
d.text((72, 304), "в один ZIP-архив", font=font(SANS_BOLD, 58), fill=(122, 175, 255, 255))

# --- подпись -----------------------------------------------------------------
d.text((74, 396), "Ассеты из JS/JSON, iframe-рамки, музыка", font=font(SANS, 25),
       fill=(186, 199, 219, 255))
d.text((74, 434), "и аккуратно переписанные ссылки.", font=font(SANS, 25),
       fill=(186, 199, 219, 255))

# --- пилюли ------------------------------------------------------------------
pills = [("Python 3.8+", 72, 176), ("0 зависимостей", 272, 196), ("v1.4", 500, 96)]
for label, x, width in pills:
    d.rounded_rectangle((x, 496, x + width, 546), radius=25,
                        fill=(31, 41, 59, 255), outline=(78, 98, 130, 255), width=2)
    d.text((x + 20, 508), label, font=font(SANS_BOLD, 19), fill=(222, 232, 248, 255))

# --- тень под «окном терминала» ---------------------------------------------
shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle((698, 158, 1142, 472), radius=24,
                                        fill=(0, 0, 0, 150))
img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(28)))
d = ImageDraw.Draw(img)

# --- окно терминала ----------------------------------------------------------
d.rounded_rectangle((690, 142, 1132, 462), radius=22,
                    fill=(17, 23, 35, 245), outline=(78, 100, 135, 220), width=2)
d.rounded_rectangle((690, 142, 1132, 190), radius=22, fill=(26, 34, 49, 255))
d.rectangle((690, 176, 1132, 190), fill=(26, 34, 49, 255))
for i, color in enumerate(((255, 107, 107, 255), (245, 185, 66, 255), (84, 214, 138, 255))):
    d.ellipse((712 + i * 24, 160, 724 + i * 24, 172), fill=color)
d.text((800, 155), "site_mirror.py  —  mirror", font=font(MONO, 18),
       fill=(147, 160, 180, 255))
d.line((690, 190, 1132, 190), fill=(78, 100, 135, 130), width=1)

lines = [
    ((714, 216), "$ python site_mirror.py mirror", (122, 175, 255, 255)),
    ((714, 246), "  26.12.34.56 8080 --zip", (122, 175, 255, 255)),
    ((714, 296), "✓ рамка → ?embed=true", (222, 233, 248, 255)),
    ((714, 330), "✓ найдено ассетов: 1248", (222, 233, 248, 255)),
    ((714, 364), "✓ готово: mirror_game.zip", (84, 214, 138, 255)),
]
for pos, text, color in lines:
    d.text(pos, text, font=font(MONO, 20), fill=color)
d.rounded_rectangle((714, 410, 1108, 414), radius=2, fill=(38, 50, 70, 255))
d.rounded_rectangle((714, 410, 998, 414), radius=2, fill=(79, 141, 253, 255))

# --- кнопка «Скачать .zip» ---------------------------------------------------
d.rounded_rectangle((742, 492, 1080, 562), radius=18,
                    fill=(79, 141, 253, 255), outline=(160, 200, 255, 255), width=2)
d.text((782, 507), "↓", font=font(SANS_BOLD, 36), fill=(255, 255, 255, 255))
d.text((834, 514), "Скачать .zip", font=font(SANS_BOLD, 27), fill=(255, 255, 255, 255))

# --- нижняя строка -----------------------------------------------------------
d.line((72, 590, 1128, 590), fill=(48, 62, 86, 255), width=1)
d.text((72, 598), REPO_LABEL, font=font(MONO, 17),
       fill=(139, 154, 178, 255))
d.text((938, 598), "1200 × 630  ·  OG IMAGE", font=font(MONO, 17),
       fill=(139, 154, 178, 255))

img.convert("RGB").save(ASSETS / "og-image.png", optimize=True)

# --- квадратная иконка -------------------------------------------------------
S = 512
icon = Image.new("RGBA", (S, S), (12, 16, 25, 255))
ig = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(ig).ellipse((-140, -190, 400, 350), fill=(79, 141, 253, 120))
ImageDraw.Draw(ig).ellipse((220, 220, 700, 700), fill=(88, 101, 242, 110))
icon = Image.alpha_composite(icon, ig.filter(ImageFilter.GaussianBlur(70)))
di = ImageDraw.Draw(icon)
di.rounded_rectangle((56, 56, 456, 456), radius=92,
                     fill=(22, 30, 45, 235), outline=(105, 155, 245, 230), width=5)
di.rounded_rectangle((146, 150, 366, 176), radius=13, fill=(79, 141, 253, 255))
di.rounded_rectangle((146, 200, 300, 220), radius=10, fill=(120, 145, 180, 255))
di.rounded_rectangle((146, 238, 336, 258), radius=10, fill=(120, 145, 180, 255))
di.line((256, 278, 256, 366), fill=(84, 214, 138, 255), width=18)
di.polygon(((206, 336), (306, 336), (256, 396)), fill=(84, 214, 138, 255))
icon.convert("RGB").save(ASSETS / "icon-512.png", optimize=True)

print("OK:", ASSETS / "og-image.png", (ASSETS / "og-image.png").stat().st_size, "bytes")
print("OK:", ASSETS / "icon-512.png", (ASSETS / "icon-512.png").stat().st_size, "bytes")
