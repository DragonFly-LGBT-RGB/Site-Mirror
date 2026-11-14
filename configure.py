#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подставляет реальный адрес GitHub Pages вместо плейсхолдера USERNAME.

Примеры:
    python configure.py --user octocat
    python configure.py --user octocat --repo my-game
    python configure.py --url https://mirror.example.com/ --repo-url https://github.com/octocat/site-mirror
    python configure.py --check

Discord читает метатеги из исходного HTML и не понимает относительные URL,
поэтому этот шаг обязателен перед деплоем.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

BASE_TOKEN = "https://USERNAME.github.io/site-mirror/"
REPO_TOKEN = "https://github.com/USERNAME/site-mirror"
BARE_REPO_TOKEN = "github.com/USERNAME/site-mirror"

FILES = [
    "index.html",
    "404.html",
    "embed-tags.html",
    "BUNDLE_README.md",
    "README.md",
    "oembed.json",
    "robots.txt",
    "sitemap.xml",
    "files.json",
    "tools/make_og.py",
]

# Упоминания в инструкциях («замени USERNAME на логин») не трогаем.
URL_MARKERS = ("USERNAME.github.io", "github.com/USERNAME")


def remaining() -> list[str]:
    found = []
    for rel in FILES:
        p = ROOT / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for marker in URL_MARKERS:
            if marker in text:
                found.append(f"{rel}: {marker}")
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Заменяет плейсхолдер USERNAME в абсолютных URL сайта.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="После замены скрипт пересобирает download/site-mirror.zip.",
    )
    ap.add_argument("--user", help="логин GitHub")
    ap.add_argument("--repo", default="site-mirror", help="имя репозитория (по умолчанию site-mirror)")
    ap.add_argument("--url", help="свой домен, например https://mirror.example.com/")
    ap.add_argument("--repo-url", help="полный URL репозитория")
    ap.add_argument("--check", action="store_true", help="только проверить, остался ли USERNAME")
    args = ap.parse_args(argv)

    if args.check:
        left = remaining()
        if left:
            print("Ещё не настроено:")
            for x in left:
                print("  ✗", x)
            print("\nЗапусти: python configure.py --user ТВОЙ_ЛОГИН")
            return 1
        print("✓ Все абсолютные URL настроены.")
        return 0

    if not args.user and not args.url:
        ap.error("нужен хотя бы --user или --url (либо --check)")

    # --- адрес страницы ------------------------------------------------- #
    if args.url:
        base = args.url.strip()
        if not base.endswith("/"):
            base += "/"
    else:
        user = args.user.strip().lstrip("@")
        if args.repo.lower() == f"{user}.github.io".lower():
            base = f"https://{user}.github.io/"
        else:
            base = f"https://{user}.github.io/{args.repo.strip('/')}/"

    # --- адрес репозитория ---------------------------------------------- #
    if args.repo_url:
        repo_url = args.repo_url.strip().rstrip("/")
    elif args.user:
        repo_url = f"https://github.com/{args.user.strip().lstrip('@')}/{args.repo.strip('/')}"
    else:
        repo_url = REPO_TOKEN
        print("⚠ --repo-url не указан: ссылки на репозиторий останутся с USERNAME.")

    changed = []
    for rel in FILES:
        p = ROOT / rel
        if not p.exists():
            continue
        old = p.read_text(encoding="utf-8")
        new = (old.replace(BASE_TOKEN, base)
                  .replace(REPO_TOKEN, repo_url)
                  .replace(BARE_REPO_TOKEN, repo_url.replace("https://", "").replace("http://", "")))
        if new != old:
            p.write_text(new, encoding="utf-8")
            changed.append(rel)

    # --- пересобираем ZIP, чтобы README внутри тоже ссылался на новый адрес #
    try:
        subprocess.run([sys.executable, str(ROOT / "tools" / "build_bundle.py")],
                       check=True, cwd=str(ROOT))
    except subprocess.CalledProcessError as e:
        print("⚠ Не удалось пересобрать ZIP:", e)

    # OG-картинка содержит адрес репозитория — перегенерируем, если есть Pillow
    try:
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_og.py")],
                       check=True, cwd=str(ROOT), capture_output=True)
        print("✓ OG-картинка перегенерирована с новым адресом.")
    except Exception:  # noqa: BLE001
        print("ℹ Чтобы обновить адрес на картинке: python tools/make_og.py (нужен Pillow).")

    print(f"✓ Адрес сайта:  {base}")
    print(f"✓ Репозиторий:  {repo_url}")
    print(f"✓ Обновлено файлов: {len(changed)}" + (": " + ", ".join(changed) if changed else ""))

    left = remaining()
    if left:
        print("⚠ Остались плейсхолдеры (проверь вручную):")
        for x in left:
            print("   ", x)
        return 1
    print("✓ Плейсхолдеров в URL не осталось. Можно пушить и включать Pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
