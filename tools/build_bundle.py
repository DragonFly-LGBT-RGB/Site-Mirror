#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собирает download/site-mirror.zip — архив, который сайт отдаёт на устройство.

    python tools/build_bundle.py
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "download" / "site_mirror.py"
OUT = ROOT / "download" / "site-mirror.zip"

script = SRC.read_bytes()
digest = hashlib.sha256(script).hexdigest()
README = (ROOT / "BUNDLE_README.md").read_text(encoding="utf-8").replace("__SHA256__", digest)

START_BAT = """@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo site_mirror v1.4 - otkryvayu veb-interfeys...
python site_mirror.py
pause
"""

START_SH = """#!/bin/sh
cd "$(dirname "$0")" || exit 1
echo "site_mirror v1.4 — открываю веб-интерфейс..."
python3 site_mirror.py
"""

EMBED_TAGS_TEXT = (ROOT / "embed-tags.html").read_text(encoding="utf-8")

REQUIREMENTS = """# Зависимостей нет: только стандартная библиотека Python 3.8+.
# Этот файл здесь для совместимости с шаблонами CI.
"""

BRIDGE = (ROOT / "bridge.py").read_bytes()

files = {
    "site_mirror.py": script,
    "bridge.py": BRIDGE,
    "README.md": README.encode("utf-8"),
    "start.bat": START_BAT.encode("utf-8"),
    "start.sh": START_SH.encode("utf-8"),
    "embed-tags.html": EMBED_TAGS_TEXT.encode("utf-8"),
    "requirements.txt": REQUIREMENTS.encode("utf-8"),
}

with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for name, data in files.items():
        info = zipfile.ZipInfo(name, date_time=(2026, 11, 14, 12, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o755 << 16 if name.endswith(".sh") else 0o644 << 16
        z.writestr(info, data)

print("OK:", OUT, OUT.stat().st_size, "bytes")
print("SHA-256 site_mirror.py:", digest)
