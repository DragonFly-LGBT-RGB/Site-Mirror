#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
site_mirror.py — «скачиватель игры» по IP из Radmin VPN + понятный интерфейс.

Что нового в 1.2
----------------
  * URL теперь можно вводить прямо в интерфейсе (http://..., https://..., host:port,
    просто IP — всё поймёт).
  * Появился графический интерфейс на tkinter (запускается, если просто открыть файл).
  * Консольный режим и мастер остались и работают по-прежнему.

Заточен под два проекта:
  * web-dashers/web-dashers.github.io — статическая JS-игра (Geometry Dash веб-мод).
    Ассеты (спрайты, .fnt, .ogg/.mp3, json-уровни) грузятся из JS динамически,
    поэтому обычный «рекурсивный обход ссылок» их не видит. Здесь есть режим
    --extract-assets, который выковыривает пути к файлам прямо из JS/JSON/HTML.
  * ppy/osu-web — PHP/Laravel-приложение. Статикой оно НЕ копируется
    (нужны PHP, MySQL, Redis, очереди, API). Для него есть режим `repo`,
    который клонирует репозиторий и подсказывает, как поднять его локально.

Зависимостей нет — только стандартная библиотека Python 3.8+.

Примеры:
    python site_mirror.py                          # откроется окно (tkinter)
    python site_mirror.py --cli                    # интерактивный мастер в консоли
    python site_mirror.py mirror 26.12.34.56 8080
    python site_mirror.py mirror http://26.12.34.56:8080 --threads 16
    python site_mirror.py mirror http://26.12.34.56:8080/game/index.html
    python site_mirror.py serve web-dashers_26.12.34.56_8080
    python site_mirror.py repo web-dashers
    python site_mirror.py repo osu-web

Качает ТОЛЬКО с того хоста:порта, который ты указал (свой/дружеский сервер).
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import mimetypes
import os
import posixpath
import queue
import re
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Константы
# --------------------------------------------------------------------------- #

VERSION = "1.4"
APP_DIR = Path(__file__).resolve().parent

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 site_mirror/1.2"
)

TEXT_KINDS = {
    "text/html", "application/xhtml+xml", "text/css", "text/javascript",
    "application/javascript", "application/json", "application/manifest+json",
    "application/ld+json", "text/plain", "image/svg+xml", "application/xml",
    "text/xml", "application/rss+xml", "application/atom+xml",
}

# расширения, которые считаем «полезными файлами игры»
ASSET_EXTS = (
    "png|jpg|jpeg|gif|webp|bmp|ico|avif|tif|tiff|psd|svg|"
    "css|js|mjs|cjs|map|json|geojson|xml|txt|plist|fnt|atlas|dat|ini|cfg|"
    "mp3|ogg|oga|wav|m4a|aac|flac|opus|weba|"
    "mp4|webm|ogv|mov|"
    "ttf|otf|woff|woff2|eot|"
    "wasm|swf|glsl|frag|vert|obj|mtl|bin|lvl|gmd|obj"
)
ASSET_RE = re.compile(
    r"""(?P<q>["'`])(?P<url>[^"'`\s<>]{1,300}?\.(?:%s)(?:\?[^"'`\s<>]*)?)(?P=q)""" % ASSET_EXTS,
    re.IGNORECASE,
)

CSS_URL_RE = re.compile(r"""url\(\s*(?P<q>['"]?)(?P<url>[^'")]+)(?P=q)\s*\)""", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(r"""@import\s+(?P<q>['"])(?P<url>[^'"]+)(?P=q)""", re.IGNORECASE)
ABSOLUTE_RE = re.compile(r"""https?://[^\s"'`<>()\\]+""", re.IGNORECASE)
# "image/png", "text/html" — MIME-типы, а не пути на сервере
MIME_RE = re.compile(r"^[a-z]+/[a-z0-9.+-]{1,40}$", re.I)
# строки-«пути» в JS: "assets/music/" + name, "assets/levels/", "sprites/" и т.п.
PATH_STR_RE = re.compile(r"""(?P<q>["'`])(?P<p>[A-Za-z0-9_\-][A-Za-z0-9_\-./]{1,240})(?P=q)""")

HTML_LINK_ATTRS = {
    "href", "src", "poster", "data", "action", "formaction", "background",
    "data-src", "data-href", "data-url", "data-file", "manifest",
    "data-srcset", "srcset", "content", "cite", "longdesc",
}

# что пробуем скачать «на всякий случай», даже если ссылки не было
COMMON_PROBES = [
    "/robots.txt", "/sitemap.xml", "/favicon.ico",
    "/manifest.json", "/manifest.webmanifest", "/asset-manifest.json",
    "/sw.js", "/service-worker.js", "/version.json", "/asset_manifest.json",
    "/assets/config.json", "/data/levels.json", "/levels.json", "/config.json",
    # Web Dashers: аккаунты, сохранения, онлайн-уровни (только GET)
    "/api/auth/me", "/api/saves", "/api/levels", "/api/level", "/api/config",
    "/api/version", "/api/songs", "/api/leaderboard",
]

WINDOWS_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


# --------------------------------------------------------------------------- #
#  Утилиты
# --------------------------------------------------------------------------- #

def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)


def hget(headers, name: str, default: str = "") -> str:
    """Заголовки приходят с разным регистром (Content-type / Content-Type)."""
    if not headers:
        return default
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v if isinstance(v, str) else (v[0] if v else default)
    return default


def human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(n) < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"


def q_uuid(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8", "replace")).hexdigest()[:8]


def safe_segment(seg: str) -> str:
    """Приводит сегмент пути к виду, который переживёт Windows."""
    seg = WINDOWS_BAD.sub("_", seg)
    seg = seg.strip().rstrip(". ")
    if not seg:
        seg = "_"
    if seg.split(".")[0].lower() in WINDOWS_RESERVED:
        seg = "_" + seg
    if len(seg) > 100:
        root, dot, ext = seg.rpartition(".")
        if dot and len(ext) <= 10:
            seg = root[: 95 - len(ext)] + "." + ext
        else:
            seg = seg[:100]
    return seg


def split_url(url: str):
    return urllib.parse.urlsplit(url)


def origin_of(url: str):
    p = split_url(url)
    return (p.scheme.lower(), (p.hostname or "").lower(), p.port or default_port(p.scheme))


def default_port(scheme: str):
    return 443 if scheme.lower() == "https" else 80


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Абсолютный, нормализованный URL без фрагмента. None — если нам такое не надо."""
    if not url:
        return None
    url = url.strip()
    if not url or url[0] in "#?":
        return None
    low = url.lower()
    if low.startswith(("data:", "blob:", "javascript:", "mailto:", "tel:", "about:",
                       "chrome:", "intent:", "whatsapp:", "ftp:", "file:")):
        return None
    if base:
        url = urllib.parse.urljoin(base, url)
    p = split_url(url)
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    path = p.path or "/"
    # сводим //, /./, /../
    trailing = path.endswith("/")
    path = posixpath.normpath(path)
    if not path.startswith("/"):
        path = "/" + path
    if trailing and not path.endswith("/"):
        path += "/"
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc, urllib.parse.quote(urllib.parse.unquote(path), safe="/:@!$&'()*+,;=-._~%"),
         p.query, ""))


def content_kind(content_type: str, path: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        return "html"
    if ct == "text/css":
        return "css"
    if ct in ("text/javascript", "application/javascript", "application/x-javascript"):
        return "js"
    if ct in ("application/json", "application/manifest+json", "application/ld+json"):
        return "json"
    if ct == "image/svg+xml":
        return "svg"
    if ct.startswith("text/") or ct in ("application/xml", "text/xml", "application/rss+xml"):
        return "text"
    ext = posixpath.splitext(path)[1].lower()
    return {
        ".html": "html", ".htm": "html", ".css": "css", ".js": "js", ".mjs": "js",
        ".json": "json", ".svg": "svg",
    }.get(ext, "")


def guess_ext(content_type: str, url_path: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    known = {
        "text/html": ".html", "text/css": ".css", "application/javascript": ".js",
        "text/javascript": ".js", "application/json": ".json", "image/png": ".png",
        "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
        "image/svg+xml": ".svg", "image/x-icon": ".ico", "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg", "audio/wav": ".wav", "text/plain": ".txt",
        "application/xml": ".xml", "application/octet-stream": "",
    }
    if ct in known:
        return known[ct]
    ext = posixpath.splitext(url_path)[1]
    if ext and len(ext) <= 12:
        return ""
    return mimetypes.guess_extension(ct) or ""


# --------------------------------------------------------------------------- #
#  URL  ->  путь на диске
# --------------------------------------------------------------------------- #

def url_to_relpath(url: str, kind: str = "", content_type: str = "") -> str:
    """
    Детерминированно превращает URL в относительный путь файла.
    Структура сайта сохраняется (assets/style.css -> assets/style.css),
    чтобы даже не переписанные ссылки работали.
    """
    p = split_url(url)
    raw_path = urllib.parse.unquote(p.path or "/")

    segments = [safe_segment(s) for s in raw_path.split("/") if s not in ("", ".")]
    last = segments[-1] if segments else ""

    # ?v=123 у спрайтов/скриптов — это cache-buster, имя файла от него не зависит.
    # У остальных (?page=2, api) query важен, поэтому кодируем его в имя.
    static_ext = posixpath.splitext(last)[1].lower().lstrip(".")
    cache_buster = static_ext in {
        "js", "mjs", "css", "png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp", "avif",
        "woff", "woff2", "ttf", "otf", "eot", "mp3", "ogg", "wav", "m4a", "fnt", "atlas",
        "wasm", "mp4", "webm", "json",
    }
    if p.query and re.search(r"(^|&)embed(=|&|$)", p.query):
        # ?embed=true — тот же документ, просто другой режим показа
        segments = segments[:-1] if segments and segments[-1] == last else segments
        segments.append("index.html")
        return "/".join(segments)
    if p.query and not cache_buster:
        tag = q_uuid(p.query)
        stem, ext = posixpath.splitext(last)
        qpart = f"{stem}__q_{tag}{ext}" if ext else f"{last}__q_{tag}"
        if segments:
            segments[-1] = qpart
        else:
            segments = [qpart]
        last = qpart

    if raw_path.endswith("/") or not last:
        segments.append("index.html")
    else:
        stem, ext = posixpath.splitext(last)
        if not ext:
            # путь без расширения: /levels/12345
            if kind == "html":
                segments.append("index.html")
            else:
                ext2 = guess_ext(content_type, raw_path) or ".bin"
                segments[-1] = last + ext2
    return "/".join(segments) if segments else "index.html"


# --------------------------------------------------------------------------- #
#  Извлечение ссылок из HTML (через настоящий парсер)
# --------------------------------------------------------------------------- #

class LinkHarvester(HTMLParser):
    """Собирает из HTML: ссылки в атрибутах, srcset, <base>, инлайн CSS/JS."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []   # (сырое значение, как трактовать)
        self.base_href: str | None = None
        self.inline_chunks: list[str] = []
        self.found_title: str = ""
        self._in_style = False
        self._in_script = False
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and d.get("href"):
            self.base_href = d["href"]
        if tag == "style":
            self._in_style = True
        if tag == "title":
            self._in_title = True
        if tag == "script":
            self._in_script = True
            if d.get("src"):
                self.links.append((d["src"], "url"))
        for k, v in d.items():
            if not v or k not in HTML_LINK_ATTRS:
                continue
            if k in ("srcset", "data-srcset"):
                for part in v.split(","):
                    cand = part.strip().split(" ")[0]
                    if cand:
                        self.links.append((cand, "url"))
            elif k == "content":
                if re.search(r"\.(png|jpe?g|gif|webp|svg|ico|mp3|ogg)(\?|$)", v, re.I) or v.startswith(("http", "/")):
                    self.links.append((v, "url"))
            else:
                self.links.append((v, "url"))
        if d.get("style"):
            self.inline_chunks.append(d["style"])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        if tag == "script":
            self._in_script = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_style or self._in_script:
            if len(data) < 5_000_000:
                self.inline_chunks.append(data)
        elif self._in_title and not self.found_title:
            self.found_title = data.strip()

    def handle_comment(self, data):
        for m in ABSOLUTE_RE.finditer(data):
            self.links.append((m.group(0), "url"))


def is_asset_path(path: str) -> bool:
    """Похоже ли на файл ассета (тогда html в ответе — верный признак «не найдено»)."""
    ext = posixpath.splitext(path)[1].lower().lstrip(".")
    return bool(ext) and ext in ASSET_EXTS.split("|")


def harvest_css(text: str):
    out = []
    for m in CSS_URL_RE.finditer(text):
        u = m.group("url").strip()
        # "image/png" из JS (toDataURL("image/png")) — не адрес файла
        if MIME_RE.match(u) or u.startswith(("data:", "about:", "javascript:")):
            continue
        out.append(u)
    for m in CSS_IMPORT_RE.finditer(text):
        u = m.group("url").strip()
        if not MIME_RE.match(u):
            out.append(u)
    return out


def harvest_dirs(text: str):
    """Каталоги, упомянутые в JS строками: "assets/music/" + name, "assets/levels/" и т.п."""
    out = set()
    for m in PATH_STR_RE.finditer(text):
        p = m.group("p")
        if " " in p or len(p) < 3:
            continue
        # "image/png", "text/html" — это MIME-тип, а не папка на сервере
        if MIME_RE.match(p) or p.lower() in ("utf-8", "utf8"):
            continue
        # CSS/JS-классы вида bg-black/40 — не каталоги
        if p.count("/") == 1 and p.split("/")[0] in ("bg", "text", "border", "from", "to", "via", "ring", "shadow"):
            continue
        if p.endswith("/"):
            d = p
        elif "/" in p:
            d = p.rsplit("/", 1)[0] + "/"
        else:
            continue
        if d.strip("/") and any(ch.isalpha() for ch in d):
            out.add(d)
    return out


def harvest_js(text: str):
    """Пути к ассетам, зашитые строками в JS/JSON."""
    out = []
    for m in ASSET_RE.finditer(text):
        u = m.group("url")
        if len(u) < 400:
            out.append(u)
    # ещё fetch('/api/...') и XHR
    for m in re.finditer(r"""(?:fetch|axios\.(?:get|post)|open)\(\s*["'`]([^"'`\s]{1,300})["'`]""", text):
        out.append(m.group(1))
    return out


# --------------------------------------------------------------------------- #
#  Конфиг
# --------------------------------------------------------------------------- #

class Config:
    def __init__(self, base_url: str, out_dir: Path, **kw):
        self.base_url = base_url
        self.origin = origin_of(base_url)
        self.out_dir = out_dir
        self.threads = kw.get("threads", 12)
        self.delay = kw.get("delay", 0.0)
        self.timeout = kw.get("timeout", 25)
        self.retries = kw.get("retries", 2)
        self.max_files = kw.get("max_files", 50_000)
        self.max_bytes = kw.get("max_bytes", 300 * 1024 * 1024)
        self.max_file_bytes = kw.get("max_file_bytes", 200 * 1024 * 1024)
        self.depth = kw.get("depth", 0)              # 0 = без ограничения
        self.extract_assets = kw.get("extract_assets", True)
        self.rewrite = kw.get("rewrite", True)
        self.rewrite_js_absolute = kw.get("rewrite_js_absolute", True)
        self.max_404 = kw.get("max_404", 400)
        self.probe_common = kw.get("probe_common", True)
        self.follow_external = kw.get("follow_external", False)
        self.max_speculative = kw.get("max_speculative", 400)
        self.dir_sweep = kw.get("dir_sweep", True)
        self.expand = kw.get("expand", True)
        self.expand_limit = kw.get("expand_limit", 5000)
        self.resume = kw.get("resume", True)
        self.verify_certs = kw.get("verify_certs", True)
        self.extra_urls: list[str] = kw.get("extra_urls", [])


# --------------------------------------------------------------------------- #
#  Движок зеркалирования
# --------------------------------------------------------------------------- #

class Mirror:
    def __init__(self, cfg: Config, log_cb=None, progress_cb=None):
        self.cfg = cfg
        self.q: "queue.Queue[tuple[str, int, str | None]]" = queue.Queue()
        self.lock = threading.Lock()
        self.seen: set[str] = set()
        self.map: dict[str, str] = {}            # url -> относительный путь
        self.taken: dict[str, str] = {}          # относительный путь -> url
        self.text_files: list[tuple[str, str]] = []   # (relpath, url)
        self.errors: list[tuple[str, str]] = []
        self.finished = threading.Event()
        self.done = 0
        self.bytes_total = 0
        self.skipped = 0
        self.detected: set[str] = set()
        self.spec_misses = 0            # сколько откатов уже провалилось
        self.spec_issued = 0            # сколько догадок уже поставлено в очередь
        self.soft_misses = 0            # догадки, которых не оказалось на сервере
        self.tries = 0                  # сколько запросов уже сделано (для полосы прогресса)
        self.errors_total = 0           # все ошибки, включая обрезанные лимитом
        self.dirs_known: set[str] = set()   # каталоги, которые точно есть на сервере
        self.expanded = 0               # сколько вариантов имён досгенерировано
        self.listing_dirs: set[str] = set()  # каталоги, где сервер отдал автоиндекс
        self._reported_listings: set[str] = set()
        self.cooldown_until = 0.0       # пауза после 429
        self.dyn_delay = 0.0            # плавно растущая пауза между запросами
        self.too_many = 0               # сколько раз ловили 429
        self.started = time.time()
        self.started_str = time.strftime("%Y-%m-%d %H:%M:%S")
        self.ctx = self._make_ctx(cfg.verify_certs)
        self.stop_flag = False
        self.log_cb = log_cb            # куда писать строки лога (по умолчанию stdout)
        self.progress_cb = progress_cb  # уведомление о прогрессе

    @staticmethod
    def _make_ctx(verify: bool) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # -- helpers ---------------------------------------------------------- #

    def in_scope(self, url: str) -> bool:
        if self.cfg.follow_external:
            return True
        return origin_of(url) == self.cfg.origin

    def enqueue(self, url: str | None, depth: int = 0, alts=(), soft: bool = False):
        """
        Ставит URL в очередь.
        alts — куда откатиться, если путь не найден (404).
        soft — «догадка»: если её нет на сервере, это не ошибка и в отчёт не идёт.
        """
        if not url:
            return
        with self.lock:
            if url in self.seen:
                return
            if not self.in_scope(url):
                return
            if self.cfg.max_files and len(self.seen) >= self.cfg.max_files:
                return
            if self.cfg.depth and depth > self.cfg.depth:
                return
            self.seen.add(url)
        self.q.put((url, depth, tuple(a for a in alts if a and a != url), soft))

    def enqueue_ref(self, raw: str, base: str, depth: int, script_dir: str | None = None,
                    soft: bool = False, trusted: bool = False):
        """
        Ставит ссылку, найденную внутри страницы/скрипта.

        Один и тот же путь в игре может означать разное в зависимости от того,
        как автор его писал: от документа, от корня сайта или от папки скрипта.
        Поэтому запоминаем до трёх вариантов: основной + два отката.
        """
        if not raw or len(raw) > 500:
            return
        raw = raw.strip()
        if raw.startswith("//"):
            raw = split_url(base).scheme + ":" + raw
        primary = normalize_url(raw, base)
        if not primary:
            return
        # «голое» имя файла без каталога — это почти всегда догадка (параметр в JS),
        # таких догадок на большом сайте тысячи, поэтому ограничиваем их число
        bare = "/" not in raw.strip("./").split("?")[0]
        if trusted:
            # ссылка из листинга каталога — такой файл точно существует
            bare = False
            soft = False
        if bare:
            with self.lock:
                if self.spec_issued >= self.cfg.max_speculative:
                    return
                self.spec_issued += 1
            soft = True
        alts: list[str] = []
        if not raw.startswith(("/", "http:", "https:", "//")):
            root_rel = normalize_url("/" + raw.lstrip("./"), self.cfg.base_url)
            if root_rel:
                alts.append(root_rel)
            if script_dir and "/" in raw.strip("./"):
                sd = normalize_url(script_dir.rstrip("/") + "/", self.cfg.base_url)
                if sd:
                    alts.append(normalize_url(raw, sd))
        self.enqueue(primary, depth, alts, soft)

    def log(self, msg: str):
        # Одна точка вывода: консоль (по умолчанию) или графический интерфейс.
        if self.log_cb:
            self.log_cb(msg)
        else:
            print(msg, flush=True)

    def progress(self):
        if not self.progress_cb:
            return
        with self.lock:
            snap = dict(done=self.done, queued=self.q.qsize(), seen=len(self.seen),
                        bytes_total=self.bytes_total, errors=len(self.errors),
                        tries=self.tries, errors_total=max(self.errors_total, len(self.errors)),
                        skipped=self.skipped)
        try:
            self.progress_cb(snap)
        except Exception:  # noqa: BLE001
            pass

    # -- загрузка --------------------------------------------------------- #

    def fetch(self, url: str, referer: str | None = None):
        last_err = None
        for attempt in range(self.cfg.retries + 1):
            wait = self.cooldown_until - time.time()
            if wait > 0:
                time.sleep(min(wait, 5.0))
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Accept-Language": "ru,en;q=0.8",
                    # сайты предпросмотра (вроде arena.site) отдают настоящую
                    # страницу только при нормальном Referer
                    "Referer": referer or self.cfg.base_url,
                })
                with urllib.request.urlopen(req, timeout=self.cfg.timeout, context=self.ctx) as r:
                    if self.dyn_delay > 0:
                        self.dyn_delay = max(0.0, self.dyn_delay - 0.002)
                    return r.status, dict(r.headers), r.read(self.cfg.max_file_bytes + 1), r.geturl()
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code == 429:
                    # сервер просит притормозить — тормозим все потоки
                    with self.lock:
                        self.too_many += 1
                        self.cooldown_until = time.time() + min(1.0 + attempt * 2, 10)
                        self.dyn_delay = min(self.dyn_delay + 0.05, 1.0)
                    if attempt < self.cfg.retries:
                        time.sleep(min(1.0 + attempt * 2, 10))
                        continue
                if e.code in (401, 403, 404, 410) and attempt == 0:
                    return e.code, dict(e.headers or {}), b"", url
                if e.code < 500:
                    return e.code, dict(e.headers or {}), b"", url
            except ssl.SSLError as e:
                if self.cfg.verify_certs:
                    # самоподписанный сертификат в локальной сети — обычное дело
                    self.cfg.verify_certs = False
                    self.ctx = self._make_ctx(False)
                    self.log("  ! HTTPS-сертификат не проверен (самоподписанный) — продолжаю без проверки")
                    continue
                last_err = f"SSL: {e}"
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
            if attempt < self.cfg.retries:
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(last_err or "ошибка загрузки")

    def detect_stack(self, url: str, headers: dict, body: bytes):
        blob = body[:200_000].decode("utf-8", "replace").lower()
        h = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
        found = set()
        if "laravel_session" in h or "xsrf-token" in blob or "laravel" in h:
            found.add("laravel")
        if "livewire" in blob:
            found.add("livewire")
        if "x-powered-by: express" in h:
            found.add("express")
        if "__next_data__" in blob or "_next/static" in blob:
            found.add("nextjs")
        if "vite" in blob and "/@vite/" in blob:
            found.add("vite")
        if "osu!" in blob or "osu-web" in blob or "ppy.sb" in blob or "osu.ppy.sh" in blob:
            found.add("osu-web")
        if "geometry dash" in blob or "gd_" in blob or "level creator" in blob:
            found.add("web-dashers")
        for f in found:
            with self.lock:
                self.detected.add(f)

    # -- обработка одного URL --------------------------------------------- #

    def looks_like_wrapper(self, url: str, body: bytes) -> str | None:
        """
        Некоторые площадки (Arena, CodePen, JSFiddle, GitHub Pages-редиректы)
        не отдают сайт напрямую: они показывают свою «рамку», а игра лежит
        в iframe. Возвращает адрес настоящей страницы или None.
        """
        if not url.lower().split("?")[0].endswith((".html", "/")) and not re.search(r"/[^./]*$", url.split("?")[0]):
            return None
        head = body[:300_000]
        if isinstance(head, bytes):
            head = head.decode("utf-8", "replace")
        if "<iframe" not in head.lower():
            return None
        m = re.search(r"<iframe[^>]+src=\s*[\"']([^\"']+)[\"']", head, re.I)
        if not m:
            return None
        target = m.group(1).strip()
        if target.startswith("//"):
            target = "https:" + target
        target = urllib.parse.urljoin(url, target)
        # рамка может ссылаться сама на себя — тогда это не обёртка
        if target.split("?")[0].rstrip("/") == url.split("?")[0].rstrip("/"):
            # попробуем вариант с ?embed=true (типично для предпросмотра)
            sep = "&" if "?" in target else "?"
            if "embed" not in target:
                target = target + sep + "embed=true"
            else:
                return None
        return target

    def handle(self, url: str, depth: int, alts: tuple = (), soft: bool = False):
        cfg = self.cfg
        if cfg.delay or self.dyn_delay:
            time.sleep(cfg.delay + self.dyn_delay)
        if self.bytes_total > cfg.max_bytes:
            with self.lock:
                self.done += 1
            return

        # докачка: если файл уже есть и сервер говорит тот же размер — не качаем
        if cfg.resume and not alts:
            probe_rel = url_to_relpath(url)
            existing = cfg.out_dir / probe_rel
            if existing.exists() and existing.stat().st_size > 0:
                try:
                    head = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(head, timeout=cfg.timeout, context=self.ctx) as r:
                        clen = r.headers.get("Content-Length")
                        if clen and int(clen) == existing.stat().st_size:
                            with self.lock:
                                self.map[url] = probe_rel
                                self.skipped += 1
                                self.done += 1
                            self.progress()
                            return
                except Exception:       # noqa: BLE001
                    pass                # HEAD не поддержан — просто качаем

        # если откатов уже слишком много провалилось — больше не спекулируем
        if alts and self.spec_misses > self.cfg.max_speculative:
            alts = ()

        try:
            status, headers, body, final_url = self.fetch(url)
        except Exception as e:                      # noqa: BLE001
            if alts:
                self.enqueue(alts[0], depth, alts[1:], soft)
                return
            with self.lock:
                if soft:
                    self.soft_misses += 1
                else:
                    self.errors.append((url, str(e)))
                    self.errors_total += 1
                self.done += 1
            if not soft:
                self.log(f"  ✗ {url}  ->  {e}")
            self.progress()
            return

        if status >= 400:
            # путь не сошёлся — пробуем следующий вариант
            if alts and status in (403, 404, 410):
                with self.lock:
                    self.spec_misses += 1
                self.enqueue(alts[0], depth, alts[1:], soft)
                return
            with self.lock:
                if soft:
                    self.soft_misses += 1
                else:
                    self.errors.append((url, f"HTTP {status}"))
                    self.errors_total += 1
                self.done += 1
            if status not in (404,) and not soft:
                self.log(f"  ! {status}  {url}")
            self.progress()
            return

        ctype = hget(headers, "Content-Type")
        kind = content_kind(ctype, split_url(final_url).path)

        # сервер-«ловушка» отдаёт index.html на любой путь: не сохраняем это как .png/.mp3
        if kind == "html" and is_asset_path(split_url(final_url).path):
            with self.lock:
                if soft:
                    self.soft_misses += 1
                else:
                    self.errors.append((url, "сервер вернул html вместо файла"))
                    self.errors_total += 1
                self.done += 1
            self.progress()
            return

        if len(body) > cfg.max_file_bytes:
            with self.lock:
                self.errors.append((url, f"слишком большой файл ({human(len(body))})"))
                self.errors_total += 1
                self.done += 1
            self.progress()
            return

        empty_try = getattr(self, "_empty_try", set())

        # --- «обёртка предпросмотра»: сверху рамка площадки, игра внутри iframe ---
        if kind == "html" and url not in empty_try:
            inner = self.looks_like_wrapper(url, body)
            if inner:
                with self.lock:
                    self.done += 1
                    # рамку тоже сохраним под её локальным именем, чтобы было
                    # откуда взять index.html, если у игры нет своего «/»
                    self.map.setdefault(url, url_to_relpath(url, kind))
                empty_try.add(url)
                self._empty_try = empty_try
                self.log(f"  ↪ {url} — это рамка предпросмотра, внутри iframe: {inner}")
                self.enqueue(inner, depth, (), soft)
                self.progress()
                return

        # --- пустая заглушка: «сайта нет», «пусто», nginx-приветствие ---
        if kind == "html" and url not in empty_try:
            info = diagnose_page(body)
            if info["kind"] in ("notfound", "placeholder", "empty"):
                empty_try.add(url)
                self._empty_try = empty_try
                # хитрость: некоторые хостинги отдают настоящий сайт только с ?embed=true
                sep = "&" if "?" in url else "?"
                embed_url = url + sep + "embed=true"
                # вариант «?embed=true» — это служебная догадка, а не отдельная страница
                with self.lock:
                    self.map_aliases = getattr(self, "map_aliases", {})
                    self.map_aliases[embed_url] = url_to_relpath(url, kind)
                embed_try = getattr(self, "_embed_try", set())
                if url not in embed_try:
                    embed_try.add(url)
                    self._embed_try = embed_try
                    self.log(f"  ↪ {url} — {info['reason'][:90]}… пробую ?embed=true")
                    with self.lock:
                        self.done += 1
                    self.enqueue(embed_url, depth, (), soft)
                    self.progress()
                    return
                with self.lock:
                    self.errors.append((url, info["reason"]))
                    self.errors_total += 1
                    self.done += 1
                self.log(f"  ⚠ {url} — {info['reason']}")
                self.progress()
                return

        # автоиндекс каталога (nginx / python http.server) — сам листинг не нужен,
        # но из него можно вытащить имена файлов, которых нет в ссылках
        _head = body[:600] if isinstance(body, (bytes, bytearray)) else body[:600].encode("utf-8", "replace")
        is_listing = kind == "html" and (
            b"Directory listing for" in _head or b"<title>Index of" in _head
        )
        is_text = kind in ("html", "css", "js", "json", "svg", "text")
        relpath = ""

        if not is_listing:
            relpath = url_to_relpath(url, kind, ctype)
            with self.lock:
                owner = self.taken.get(relpath)
                if owner and owner != url:
                    # тот же файл уже сохранён под другим URL (например / и /index.html)
                    self.map[url] = relpath
                    self.done += 1
                    return
                self.taken[relpath] = url
            dest = cfg.out_dir / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)

            # уже есть и размер совпадает — не перекачиваем (режим докачки)
            if cfg.resume and dest.exists() and dest.stat().st_size == len(body) and not is_text:
                with self.lock:
                    self.map[url] = relpath
                    self.skipped += 1
                    self.done += 1
                self.progress()
                return

            try:
                dest.write_bytes(body)
            except OSError as e:
                with self.lock:
                    self.errors.append((url, f"запись: {e}"))
                    self.errors_total += 1
                    self.done += 1
                self.progress()
                return

            with self.lock:
                self.map[url] = relpath
                self.done += 1
                self.bytes_total += len(body)
                n = self.done
            self.detect_stack(url, headers, body)
            self._note_dirs(relpath)

            if n % 25 == 0 or n <= 40:
                self.log(f"  [{n:>5}] {status} {kind or 'bin':<5} {human(len(body)):>9}  /{relpath}")
        else:
            listing_path = split_url(final_url).path
            with self.lock:
                self.listing_dirs.add(listing_path)
                self.done += 1
                n = self.done
            if listing_path not in self._reported_listings:
                self._reported_listings.add(listing_path)
                self.log(f"  [{n:>5}] индекс каталога {listing_path} — забираю всё содержимое")

        # каждый обработанный URL чуть-чуть двигает полосу прогресса
        self.progress()

        if not is_text:
            return

        text = body.decode("utf-8", "replace")
        if not is_listing:
            with self.lock:
                self.text_files.append((relpath, url))

        # --- куда резолвить относительные ссылки ---
        base = final_url
        next_depth = depth + 1

        if kind == "html":
            self._note_dir_candidates(harvest_dirs(text), final_url)
            hp = LinkHarvester()
            try:
                hp.feed(text)
            except Exception:                       # noqa: BLE001
                pass
            if hp.base_href:
                base_override = normalize_url(hp.base_href, final_url)
                if base_override:
                    base = base_override
            for raw, _ in hp.links:
                self.enqueue_ref(raw, base, next_depth, trusted=is_listing)
            for chunk in hp.inline_chunks:
                # в инлайновых <script> url(...) — это код, а не CSS
                if "<" not in chunk and "function" not in chunk and "=>" not in chunk:
                    for u in harvest_css(chunk):
                        self.enqueue_ref(u, base, next_depth)
                if cfg.extract_assets:
                    for u in harvest_js(chunk):
                        self.enqueue_ref(u, base, next_depth, script_dir=None)
            # <a href="page"> без расширения тоже нужны
            for m in re.finditer(r"""href\s*=\s*["']([^"'#?][^"']*)["']""", text, re.I):
                self.enqueue_ref(m.group(1), base, next_depth)

        elif kind == "css":
            for u in harvest_css(text):
                self.enqueue_ref(u, final_url, next_depth)
            self._note_dir_candidates(harvest_dirs(text), final_url)

        elif kind in ("js", "json", "text", "svg"):
            self._note_dir_candidates(harvest_dirs(text), final_url)
            if cfg.extract_assets:
                for u in harvest_js(text):
                    # пути в JS обычно считают от документа, но бывает и от папки скрипта
                    self.enqueue_ref(u, self.cfg.base_url, next_depth,
                                     script_dir=posixpath.dirname(split_url(url).path) or None)
            if kind == "svg":
                for u in harvest_css(text):
                    self.enqueue_ref(u, final_url, next_depth)
            # абсолютные ссылки на другие ресурсы того же сервера
            for m in ABSOLUTE_RE.finditer(text):
                u = normalize_url(m.group(0), final_url)
                if u and self.in_scope(u):
                    self.enqueue(u, next_depth)

    def _note_dir_candidates(self, dirs, base: str):
        for d in dirs:
            for cand in (normalize_url(d, base), normalize_url("/" + d.lstrip("./"), self.cfg.base_url)):
                if not cand:
                    continue
                p = split_url(cand).path
                if p.endswith("/") and self.in_scope(cand):
                    with self.lock:
                        self.dirs_known.add(p)

    def _note_dirs(self, relpath: str):
        d = posixpath.dirname(relpath)
        with self.lock:
            while d:
                self.dirs_known.add("/" + d.strip("/") + "/")
                parent = posixpath.dirname(d)
                if parent == d:
                    break
                d = parent

    # -- обход каталогов и досгенерация имён ------------------------------ #

    def sweep_dirs(self):
        """
        Заходим в каждый известный каталог. Если сервер отдаёт автоиндекс
        (python -m http.server, nginx autoindex, live-server) — оттуда доберутся
        файлы, на которые нет ни одной ссылки: музыка, уровни, фоны.
        """
        dirs = sorted(self.dirs_known)
        if not dirs:
            return
        self.log(f"\nПроверяю каталоги на автоиндекс ({len(dirs)} шт.)…")
        for d in dirs:
            self.enqueue(normalize_url(d, self.cfg.base_url), 1, soft=True)
        self.q.join()

    def expand_siblings(self):
        """
        Игра часто строит имена так: "assets/game-bg/game_bg_" + 12 + "_001-hd.png".
        Взяв один найденный файл, досматриваем соседей с другими номерами.
        Так добираются фоны, земля, музыка — даже если сервер не отдаёт автоиндекс.
        """
        families: dict[tuple[str, str, str, int], int] = {}
        for rel in list(self.taken):
            d, base = posixpath.split(rel)
            if (("/" + d.strip("/") + "/") if d else "/") in self.listing_dirs:
                continue                      # этот каталог и так выкачан целиком
            stem, dot, ext = base.rpartition(".")
            if not dot:
                stem, ext = base, ""
            m = re.match(r"^(?P<pre>\D*)(?P<num>\d{1,3})(?P<suf>.*)$", stem)
            if not m:
                continue
            pre, num, suf = m.group("pre"), m.group("num"), m.group("suf")
            width = len(num) if num.startswith("0") else max(len(num), 2)
            key = (d, pre, suf, ext, num.startswith("0"), width)
            families[key] = families.get(key, 0) + 1

        if not families:
            return

        # семейства с большим числом найденных файлов — первыми
        ordered = sorted(families.items(), key=lambda kv: kv[1], reverse=True)
        guesses: list[str] = []
        for (d, pre, suf, ext, zero, width), _count in ordered:
            for i in range(0, 100):
                num = f"{i:0{width}d}" if width > 1 and zero else str(i)
                fname = f"{pre}{num}{suf}" + ("." + ext if ext else "")
                candidate = posixpath.join(d, fname) if d else fname
                if candidate in self.taken:
                    continue
                guesses.append(candidate)
                if len(guesses) >= self.cfg.expand_limit:
                    break
            if len(guesses) >= self.cfg.expand_limit:
                break

        if not guesses:
            return
        self.expanded = len(guesses)
        self.log(f"Досматриваю соседние файлы в нумерованных наборах ({len(guesses)} шт.)…")
        for g in guesses:
            self.enqueue(normalize_url("/" + g.lstrip("/"), self.cfg.base_url), 1, soft=True)
        self.q.join()

    # -- воркеры ---------------------------------------------------------- #

    def worker(self):
        while not self.finished.is_set():
            try:
                url, depth, alts, soft = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if not self.stop_flag:
                    with self.lock:
                        self.tries += 1
                    self.handle(url, depth, alts, soft)
            except Exception as e:                   # noqa: BLE001
                with self.lock:
                    self.errors.append((url, f"сбой обработки: {e}"))
                    self.errors_total += 1
            finally:
                self.q.task_done()

    def run(self) -> None:
        cfg = self.cfg
        self.log("")
        self.log(f"Цель:      {cfg.base_url}")
        self.log(f"Папка:     {cfg.out_dir}")
        self.log(f"Потоков:   {cfg.threads}   Лимит файлов: {cfg.max_files}   "
                 f"Лимит объёма: {human(cfg.max_bytes)}")
        self.log("")

        self.enqueue(cfg.base_url, 0)
        if not split_url(cfg.base_url).path.endswith("/"):
            self.enqueue(normalize_url("/", cfg.base_url), 0)
        if cfg.probe_common:
            for p in COMMON_PROBES:
                self.enqueue(normalize_url(p, cfg.base_url), 1, soft=True)
        for u in cfg.extra_urls:
            self.enqueue(normalize_url(u, cfg.base_url), 0)

        threads = [threading.Thread(target=self.worker, daemon=True) for _ in range(cfg.threads)]
        for t in threads:
            t.start()

        # очередь пуста ровно тогда, когда ни один воркер не держит задачу,
        # поэтому join() — надёжный признак завершения обхода
        self.q.join()

        if cfg.dir_sweep and not self.stop_flag:
            try:
                self.sweep_dirs()
            except KeyboardInterrupt:
                raise
        if cfg.expand and not self.stop_flag:
            self.expand_siblings()

        self.finished.set()
        for t in threads:
            t.join(timeout=10)

        # CLI: тоже хотим готовый index.html (в веб-интерфейсе это делается в WebJob)
        try:
            self.ensure_index()
        except Exception:  # noqa: BLE001
            pass

    # -- переписывание ссылок --------------------------------------------- #

    def ensure_index(self):
        """
        Настоящая игра может жить по адресу вида /?embed=true, и тогда
        index.html не появляется. Копируем туда главную страницу сайта,
        иначе локальный сервер покажет список файлов вместо игры.
        """
        out = self.cfg.out_dir
        index = out / "index.html"
        if index.exists() and index.stat().st_size > 0:
            return
        best = None
        for url, rel in self.map.items():
            if not self.in_scope(url):
                continue
            path = split_url(url).path
            is_html = rel.lower().endswith((".html", ".htm"))
            if not is_html:
                continue
            score = 0
            if path in ("", "/"):
                score += 100
            if path.endswith("/index.html"):
                score += 60
            if "embed" in split_url(url).query:
                score += 40
            score -= len(path)
            if best is None or score > best[0]:
                best = (score, rel)
        if not best:
            return
        src = out / best[1]
        if not src.exists():
            return
        index.write_bytes(src.read_bytes())
        self.log(f"  index.html ← {best[1]} (у игры нет главной страницы со своим именем)")

    def rewrite_all(self):
        if not self.cfg.rewrite or not self.cfg.out_dir.exists():
            return
        self.log("\nПереписываю ссылки на локальные пути…")
        changed = 0
        for relpath, url in self.text_files:
            f = self.cfg.out_dir / relpath
            if not f.exists():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            new = self.rewrite_text(text, relpath, url)
            if new != text:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(new, encoding="utf-8")
                changed += 1
        self.log(f"  обновлено файлов: {changed}")

    def rewrite_text(self, text: str, relpath: str, page_url: str) -> str:
        here_dir = posixpath.dirname(relpath)

        def local_path(target_url: str) -> str | None:
            target = self.map.get(target_url)
            if not target:
                return None
            rel = posixpath.relpath(target, here_dir or ".")
            return urllib.parse.quote(rel, safe="/@!$&'()*+,;=-._~%")

        # собираем кандидатов: атрибуты, url(), абсолютные адреса
        candidates: list[str] = []
        for m in re.finditer(r"""(?:href|src|poster|action|data|content|srcset)\s*=\s*["']([^"']+)["']""", text, re.I):
            val = m.group(1)
            if "srcset" in m.group(0).lower() or "," in val and re.search(r"\s\d+[wx]", val):
                candidates += [p.strip().split(" ")[0] for p in val.split(",") if p.strip()]
            else:
                candidates.append(val)
        candidates += [m.group("url") for m in CSS_URL_RE.finditer(text)]
        candidates += [m.group("url") for m in CSS_IMPORT_RE.finditer(text)]
        if self.cfg.rewrite_js_absolute:
            candidates += [m.group(0) for m in ABSOLUTE_RE.finditer(text)]

        base = page_url
        replacements: dict[str, str] = {}
        for raw in candidates:
            if not raw or raw.startswith(("data:", "#", "javascript:", "mailto:", "tel:", "blob:")):
                continue
            frag = ""
            if "#" in raw and not raw.startswith("#"):
                raw, _, frag = raw.partition("#")
                frag = "#" + frag if frag else ""
            absu = normalize_url(raw, base)
            if not absu:
                continue
            target_rel = local_path(absu)
            if not target_rel:
                continue
            if target_rel == raw:
                continue
            replacements[raw] = target_rel + frag

        if not replacements:
            return text
        # длинные строки меняем первыми, чтобы не портить вложенные совпадения
        for old in sorted(replacements, key=len, reverse=True):
            text = text.replace(old, replacements[old])
        return text

    # -- отчёт ------------------------------------------------------------ #

    def report(self, write_file: bool = True):
        cfg = self.cfg
        dt = time.time() - self.started
        total_size = sum(p.stat().st_size for p in cfg.out_dir.rglob("*") if p.is_file()) if cfg.out_dir.exists() else 0
        files = sum(1 for p in cfg.out_dir.rglob("*") if p.is_file()) if cfg.out_dir.exists() else 0

        self.log("")
        self.log("=" * 68)
        self.log(f"Готово за {dt:.1f} c")
        self.log(f"  файлов на диске : {files}")
        self.log(f"  объём           : {human(total_size)}")
        self.log(f"  ошибок          : {len(self.errors)}")
        if self.soft_misses:
            self.log(f"  догадок мимо    : {self.soft_misses} (не ошибки)")
        if self.too_many:
            self.log(f"  сервер отдавал 429 : {self.too_many} раз "
                     f"(слишком быстро — попробуй --threads 4 --delay 0.1)")
        if "laravel" in self.detected or "osu-web" in self.detected:
            self.log("")
            self.log("  ⚠  Сервер похож на osu-web / Laravel.")
            self.log("     Статическая копия НЕ будет работать как игра: это PHP-бэкенд")
            self.log("     с MySQL, Redis и очередями. Для osu-web используй режим:")
            self.log("         python site_mirror.py repo osu-web")
        if "web-dashers" in self.detected:
            self.log("  ✓ Похоже на Web Dashers (статика + JS) — копия должна играться.")
        self.log("=" * 68)

        # отчёт на диск
        lines = [
            "# Отчёт о копировании",
            "",
            f"- Источник: `{cfg.base_url}`",
            f"- Дата: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- Файлов: **{files}**, объём: **{human(total_size)}**, ошибок: **{len(self.errors)}**",
            f"- Время: {dt:.1f} c",
            "",
            "## Как запустить локально",
            "",
            "```bash",
            f'python site_mirror.py serve "{cfg.out_dir.name}"',
            "```",
            "",
            "или:",
            "",
            "```bash",
            f'python -m http.server 8000 --directory "{cfg.out_dir.name}"',
            "```",
            "",
            "Открой в браузере http://localhost:8000/",
            "",
        ]
        if self.detected:
            lines += ["## Что за сервер", "", f"- Определено: {', '.join(sorted(self.detected))}", ""]
        if self.errors:
            lines += ["## Не скачалось", ""]
            for u, e in self.errors[:400]:
                lines.append(f"- `{u}` — {e}")
            if len(self.errors) > 400:
                lines.append(f"- … и ещё {len(self.errors) - 400}")
            lines.append("")
        if not write_file:
            return
        try:
            (cfg.out_dir / "_MIRROR_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass


# --------------------------------------------------------------------------- #
#  Проверка доступности сервера
# --------------------------------------------------------------------------- #

# Заглушки облачных хостингов: сайта нет, а сервер отвечает «мягкой» ошибкой.
# Такие страницы выглядят как пустая белая (или чёрная) вкладка — их нельзя
# сохранять как «игру».
NOT_FOUND_MARKERS = (
    "there isn't a site hosted at this url",
    "there is no site hosted",
    "this site is not available",
    "site not found",
    "no such app",
    "this deployment does not exist",
    "deployment not found",
    "this project does not exist",
    "404 not found",
    "page not found",
)
SPA_SHELL_MARKERS = (
    '<div id="root"></div>', "<div id='root'></div>",
    '<div id="app"></div>', "<div id='app'></div>",
    '<div id="game"></div>', "<div id='game'></div>",
)
PLACEHOLDER_MARKERS = (
    "nothing to see here",
    "you should probably run",
    "welcome to nginx",
    "apache2 ubuntu default page",
    "iis windows server",
)


def diagnose_page(body, headers=None) -> dict:
    """
    Смотрит на текст страницы и отвечает простым языком, что это за страница.

    Возвращает:
      kind   — 'ok' | 'notfound' | 'placeholder' | 'empty'
      reason — готовое предложение, которое можно показать пользователю
      title  — <title>, если был
    """
    text = body or ""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    low = text.lower()
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]

    for marker in NOT_FOUND_MARKERS:
        if marker in low:
            return {"kind": "notfound", "title": title,
                    "reason": "По этому адресу сайт не размещён: сервер ответил «"
                              + marker.title() + "». Обычно ссылка устарела — открой её "
                              "в браузере и проверь, что он показывает сам сайт, а не пустую "
                              "страницу с такой надписью."}

    for marker in PLACEHOLDER_MARKERS:
        if marker in low:
            return {"kind": "placeholder", "title": title,
                    "reason": f"Сервер отдаёт заглушку («{marker}»), а не игру. Похоже, файлы "
                              "сайта не залиты или сервер поднят не в той папке."}

    # «пустой» корень: несколько килобайт и один пустой div (типичный SPA без JS/сборки)
    visible = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ",
                     text, flags=re.S | re.I)
    visible = re.sub(r"\s+", " ", visible).strip()
    shell = any(mk.replace(" ", "") in low.replace(" ", "") for mk in SPA_SHELL_MARKERS)
    if len(text) < 900 and not visible and not shell:
        return {"kind": "empty", "title": title,
                "reason": "Сервер ответил почти пустой страницей — похоже, по этому адресу "
                          "нет готового сайта (так бывает у заглушек хостинга и у сервисов, "
                          "которые выдают ссылку только на время сессии)."}
    return {"kind": "ok", "title": title, "reason": ""}


def probe(host: str, port: int | None, path: str = "/", timeout: float = 6,
          return_body: bool = False):
    """
    Пробует https, потом http. Возвращает (base_url, title, headers).

    Если та или иная схема/порт ответили ошибкой 400/404, ошибка запоминается,
    а не проглатывается — иначе «нет сайта» превращается в непонятный сбой.
    """
    errs = []
    fb = None            # первый «мягкий» ответ (400/404…) — им пользуемся как подсказкой
    cands: list[tuple[str, int, str]]
    if port:
        cands = [(host, port, "https"), (host, port, "http")]
    else:
        cands = [(host, 443, "https"), (host, 80, "http"), (host, 8080, "http"),
                 (host, 3000, "http"), (host, 5173, "http"), (host, 8000, "http")]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for h, p, scheme in cands:
        url = f"{scheme}://{h}:{p}{path or '/'}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                body = r.read(200_000).decode("utf-8", "replace")
                title = ""
                m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
                if m:
                    title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
                if return_body:
                    return url, title, dict(r.headers), body
                return url, title, dict(r.headers)
        except urllib.error.HTTPError as e:
            try:
                body = e.read(200_000).decode("utf-8", "replace")
            except Exception:                            # noqa: BLE001
                body = ""
            info = diagnose_page(body)
            if e.code in (401, 403, 429):
                title = f"(сервер отвечает, код {e.code} — возможно, ограничивает частоту)"
                if return_body:
                    return url, title, dict(e.headers or {}), body
                return url, title, dict(e.headers or {})
            reason = info["reason"] or f"HTTP {e.code} {e.reason or ''}".strip()
            if fb is None:
                fb = (url, info["title"], dict(e.headers or {}), reason, body)
            errs.append(f"{url} -> HTTP {e.code}" + (f" ({reason})" if info["reason"] else ""))
        except Exception as e:                           # noqa: BLE001
            errs.append(f"{url} -> {type(e).__name__}: {e}")
    # ни один адрес не открылся «по-настоящему», но сервер что-то ответил —
    # отдаём этот ответ, чтобы страница сказала правду: «сайта тут нет»
    if fb is not None:
        url, title, hdrs, _reason, body = fb
        if return_body:
            return url, title, hdrs, body
        return url, title, hdrs
    raise RuntimeError("Сервер не отвечает.\n  " + "\n  ".join(errs))


def parse_target(text: str, port_default: str | None = None) -> str:
    """
    Приводит «как ввёл пользователь» к нормальному базовому URL.

        '26.1.2.3'                         -> http://26.1.2.3:8080/   (если указан порт)
        '26.1.2.3:8080'                    -> http://26.1.2.3:8080/
        '26.1.2.3:8080/game/index.html'    -> http://26.1.2.3:8080/game/index.html
        'https://site.local/mods/'         -> https://site.local/mods/
        'localhost:5173/#/menu'            -> http://localhost:5173/
    """
    text = (text or "").strip().strip('"\'')
    if not text:
        raise ValueError("пустой адрес")
    # чужая схема (ftp://, file://) — не наш случай, говорим об этом прямо
    m_scheme = re.match(r"^([a-z][a-z0-9+.\-]*)://", text, re.I)
    if m_scheme and m_scheme.group(1).lower() not in ("http", "https"):
        raise ValueError(f"поддерживаются только http:// и https://, а не «{m_scheme.group(1)}://»")
    # без схемы: сначала считаем, что http (в Radmin VPN https — редкость),
    # но если порт 443 — https
    if not text.lower().startswith(("http://", "https://")):
        scheme = "http"
        hostpart = text.split("/")[0]
        if hostpart.startswith("["):
            pass                                  # [::1]:8080 — уже в скобках
        elif hostpart.count(":") > 1:
            text = f"[{hostpart}]{text[len(hostpart):]}"   # голый IPv6: ::1 -> [::1]
            hostpart = text.split("/")[0]
        if hostpart.rsplit(":", 1)[-1].strip() == "443":
            scheme = "https"
        text = f"{scheme}://{text}"
    p = urllib.parse.urlsplit(text)
    if not p.hostname:
        raise ValueError(f"не разобрал адрес: {text}")
    port = p.port
    if not port and port_default and str(port_default).strip().isdigit():
        port = int(str(port_default).strip())
    host = p.hostname
    if ":" in host and not host.startswith("["):     # IPv6
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    path = p.path or "/"
    # /game -> /game/ (это каталог), /game/index.html -> как есть
    if not path.endswith("/") and "." not in posixpath.basename(path):
        path += "/"
    # фрагмент (#/menu) в SPA нам не нужен: он не влияет на загрузку файлов
    return urllib.parse.urlunsplit((p.scheme or "http", netloc, path, p.query, ""))


def origin_label(base_url: str) -> str:
    """'http://26.1.2.3:8080/' -> '26.1.2.3_8080' (для имени папки)."""
    p = split_url(base_url)
    host = (p.hostname or "site").replace(":", "_")
    port = p.port or default_port(p.scheme)
    return f"{host}_{port}"


# --------------------------------------------------------------------------- #
#  Локальный сервер (чтобы играть в скачанное)
# --------------------------------------------------------------------------- #

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


class LocalServer:
    """Мини-сервер в отдельном потоке — его удобно включать прямо из окна."""

    def __init__(self, folder: Path, port: int = 8000):
        self.folder = Path(folder)
        self.port = port
        self.httpd: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.stopped = False

    def start(self):
        handler = lambda *a, **kw: QuietHandler(*a, directory=str(self.folder), **kw)  # noqa: E731
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stopped = True
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass


def serve(folder: Path, port: int = 8000):
    if not folder.exists():
        eprint(f"Нет папки: {folder}")
        sys.exit(2)
    try:
        srv = LocalServer(folder, port).start()
    except OSError as e:
        eprint(f"Не смог занять порт {port}: {e}")
        eprint(f"Попробуй другой: python {Path(__file__).name} serve \"{folder.name}\" --port {port + 1}")
        sys.exit(2)
    print(f"Раздаю {folder}", flush=True)
    print(f"  http://localhost:{port}/", flush=True)
    print("  Ctrl+C — остановить", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        srv.stop()


# --------------------------------------------------------------------------- #
#  Режим verify — проверка целостности копии
# --------------------------------------------------------------------------- #

def verify(folder: Path, limit: int = 60) -> int:
    """Ищет в скачанной копии ссылки на файлы, которых нет. 0 — всё на месте."""
    if not folder.exists():
        eprint(f"Нет папки: {folder}")
        return 2
    missing: list[tuple[str, str]] = []
    checked = 0
    for f in sorted(folder.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (".html", ".htm", ".css", ".js", ".json", ".svg"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_here = f.relative_to(folder).as_posix()
        refs = set()
        for m in re.finditer(r"""(?:href|src|poster|data|content)\s*=\s*["']([^"']+)["']""", text, re.I):
            refs.add(m.group(1))
        refs.update(m.group("url") for m in CSS_URL_RE.finditer(text))
        refs.update(harvest_js(text))
        for ref in refs:
            ref = ref.strip()
            if not ref or ref.startswith(("data:", "#", "http:", "https:", "//", "mailto:", "javascript:", "blob:")):
                continue
            ref = ref.split("?")[0].split("#")[0]
            if not ref:
                continue
            # «голое» имя без каталога — почти всегда ключ атласа/спрайта в Phaser,
            # а не путь к файлу: такие не проверяем (иначе тысячи ложных срабатываний)
            if "/" not in ref and "\\" not in ref:
                continue
            if "${" in ref or "{{" in ref:
                continue                      # шаблон в JS, а не путь
            if re.search(r"(^|/)api(/|$)", ref, re.I):
                continue                      # это API, а не файл
            last_seg = ref.rstrip("/").rsplit("/", 1)[-1]
            if "/" in ref or ref.startswith("/"):
                if "." not in last_seg:
                    continue                  # /levels/12345 — динамический маршрут
            raw = urllib.parse.unquote(ref).lstrip("/")
            # путь может считаться и от файла, и от корня сайта — принимаем оба варианта
            candidates = [(f.parent / raw), (folder / raw)]
            checked += 1
            ok = False
            for target in candidates:
                try:
                    target = target.resolve()
                except OSError:
                    continue
                if target.exists() and not (target.is_dir() and not (target / "index.html").exists()):
                    ok = True
                    break
            if not ok:
                missing.append((rel_here, ref))
    # уникализируем
    uniq = sorted(set(missing))
    print(f"Проверил папку: {folder}")
    print(f"  файлов: {sum(1 for x in folder.rglob('*') if x.is_file())}")
    print(f"  ссылок проверено: {checked}")
    print(f"  битых ссылок: {len(uniq)}")
    if uniq:
        print()
        for src, ref in uniq[:limit]:
            print(f"  ✗ {src}  ->  {ref}")
        if len(uniq) > limit:
            print(f"  … и ещё {len(uniq) - limit}")
        print()
        print("Часть таких ссылок — на динамические адреса (/api/...) или на файлы,")
        print("которых на сервере и не было. Если игра запускается — это не критично.")
        return 1
    print("  ✓ все локальные ссылки ведут на существующие файлы")
    return 0


# --------------------------------------------------------------------------- #
#  Режим repo — для проектов, которые статикой не копируются
# --------------------------------------------------------------------------- #

REPOS = {
    "web-dashers": ("https://github.com/web-dashers/web-dashers.github.io.git",
                    "Статическая игра: после клона просто раздай папку командой serve."),
    "web-dashers.github.io": ("https://github.com/web-dashers/web-dashers.github.io.git", ""),
    "osu-web": ("https://github.com/ppy/osu-web.git",
                "PHP/Laravel. Статикой не копируется — его нужно ЗАПУСКАТЬ, а не скачивать. "
                "Самый быстрый путь: docker compose из репозитория (см. README.md репозитория, "
                "раздел 'Installation'), нужны Docker, MySQL, Redis, Node, PHP 8.x."),
}

OSU_NOTE = """
osu-web — это сервер osu!, а не набор html-файлов.
Что это значит на практике:
  * Его страницы генерируются на PHP (Blade-шаблоны) + данные из MySQL.
  * Скачанный статический слепок выдаст тебе разметку страниц, но:
      - не будет входа в аккаунт (сессии/CSRF),
      - не будет работы API (/api/...),
      - не будет загрузки/поиска карт, реплеев, мультиплеера.
  * Правильный путь — развернуть его как приложение (docker) и указать
    в .env адрес из Radmin VPN, тогда друг подключится к твоему инстансу.

Минимально:
    git clone --depth 1 https://github.com/ppy/osu-web.git
    cd osu-web
    cp .env.example .env
    # поднять MySQL + Redis, npm i, php artisan migrate
    # официальный гайд: https://github.com/ppy/osu-web#installation
"""


def clone_repo(name: str, dest: Path | None = None):
    key = name.strip().lower()
    if key not in REPOS:
        eprint(f"Неизвестный репозиторий: {name}. Есть: {', '.join(REPOS)}")
        sys.exit(2)
    url, note = REPOS[key]
    out = dest or (APP_DIR / key)
    print(f"Клонирую {url}\n     -> {out}")
    if out.exists() and any(out.iterdir()):
        print("  Папка уже существует, пропускаю (удали её, если нужно заново).")
    else:
        rc = os.system(f'git clone --depth 1 "{url}" "{out}"')
        if rc != 0:
            eprint("git clone не удался. Проверь, установлен ли git: https://git-scm.com/downloads")
            sys.exit(1)
    if note:
        print("\n" + note)
    if key.startswith("osu-web"):
        print(OSU_NOTE)
    else:
        print(f"\nГотово. Запустить локально:\n  python {Path(__file__).name} serve \"{out.name}\"")


# --------------------------------------------------------------------------- #
#  Интерактивный мастер (консольный)
# --------------------------------------------------------------------------- #

BANNER = f"""
╔══════════════════════════════════════════════════════════════════╗
║  site_mirror {VERSION} — скачиватель игры по IP из Radmin VPN       ║
║  заточен под Web Dashers (статика) и osu-web (PHP)               ║
╚══════════════════════════════════════════════════════════════════╝
"""


def ask(prompt: str, default: str = "") -> str:
    tail = f" [{default}]" if default else ""
    try:
        v = input(f"{prompt}{tail}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return v or default


def wizard():
    print(BANNER)
    print("IP смотри в Radmin VPN (у друга он в списке сети, вида 26.x.x.x)")
    print("Можно ввести и полный URL: http://26.1.2.3:8080/game/\n")
    host = ask("IP или адрес", "26.0.0.1")
    port = ask("Порт", "8080") if ":" not in host.split("://")[-1] else ""
    print()

    try:
        guessed = parse_target(host, port if port.isdigit() else None)
        pu = urllib.parse.urlsplit(guessed)
        base_url, title, headers = probe(pu.hostname, pu.port, pu.path)
    except Exception as e:                           # noqa: BLE001
        eprint(f"\n✗ {e}")
        eprint("\nЧто проверить:")
        eprint("  1) друг запустил сервер и он слушает 0.0.0.0, а не только localhost;")
        eprint("  2) Radmin VPN: вы в одной сети, у друга включён доступ;")
        eprint("  3) порт совпадает (8080? 3000? 8000? 5173?);")
        eprint("  4) попробуй в браузере: " + guessed)
        sys.exit(1)

    print(f"✓ Сервер ответил: {base_url}")
    if title:
        print(f"  Заголовок: {title}")
    srv = headers.get("Server", "")
    if srv:
        print(f"  Server: {srv}")
    print()

    if "laravel" in " ".join(headers.values()).lower() or "osu" in title.lower():
        print("⚠ Похоже на osu-web (Laravel). Статическая копия не будет играбельной.")
        print("  Для osu-web лучше: python site_mirror.py repo osu-web\n")

    name = ask("Имя папки", f"mirror_{origin_label(base_url)}")
    threads = ask("Потоков (больше = быстрее)", "12")
    depth = ask("Глубина обхода (0 = без ограничения)", "0")
    print("\nНачинаю. Это может занять пару минут — не закрывай окно.\n")

    cfg = Config(
        base_url=base_url,
        out_dir=APP_DIR / name,
        threads=int(threads) if threads.isdigit() else 12,
        depth=int(depth) if depth.isdigit() else 0,
    )
    run_mirror(cfg)

    print()
    play = ask("Открыть локальный сервер, чтобы проверить игру? (y/n)", "y").lower()
    if play.startswith(("y", "д")):
        port2 = ask("Порт для локального сервера", "8000")
        serve(APP_DIR / name, int(port2) if port2.isdigit() else 8000)


def run_mirror(cfg: Config, **kw):
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    m = Mirror(cfg, **kw)
    try:
        m.run()
        m.rewrite_all()
        m.report()
    except KeyboardInterrupt:
        print("\n\nОстановлено пользователем. Скачанное осталось в папке.")
        m.rewrite_all()
        m.report()
    return m


def zip_folder(folder: Path) -> Path:
    archive = shutil.make_archive(str(folder), "zip", folder)
    return Path(archive)


# --------------------------------------------------------------------------- #
#  Графический интерфейс (tkinter)
# --------------------------------------------------------------------------- #

BG = "#1b1f27"
PANEL = "#232936"
PANEL2 = "#2b3342"
FG = "#e8ecf3"
MUTED = "#9aa6b8"
ACCENT = "#4f8dfd"
ACCENT2 = "#3b76e8"
OK = "#5ad38a"
WARN = "#f5b942"
BAD = "#ff6b6b"


def import_tk():
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        return tk, ttk, filedialog, messagebox
    except Exception as e:                            # noqa: BLE001
        eprint("Не удалось загрузить tkinter:", e)
        eprint("Windows/macOS: он входит в стандартный Python. Ставь Python с python.org.")
        eprint("Linux (Debian/Ubuntu): sudo apt install python3-tk")
        eprint("Перехожу в консольный режим…\n")
        return None


class MirrorApp:
    """Окно: адрес (IP или URL) → параметры → кнопки → лог и прогресс."""

    def __init__(self, tk, ttk, filedialog, messagebox):
        self.tk, self.ttk, self.filedialog, self.messagebox = tk, ttk, filedialog, messagebox
        self.root = tk.Tk()
        self.root.title(f"site_mirror {VERSION} — скачиватель игры")
        self.root.geometry("980x720")
        self.root.minsize(840, 620)
        self.root.configure(bg=BG)

        self.mirror: Mirror | None = None
        self.server: LocalServer | None = None
        self.worker: threading.Thread | None = None
        self.last_out: Path | None = None
        self.verify_after: Path | None = None
        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._prog: dict = {}

        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._pump)

    # -- оформление ------------------------------------------------------- #

    def _style(self):
        ttk = self.ttk
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2,
                     bordercolor=PANEL2, lightcolor=PANEL2, darkcolor=PANEL2,
                     focuscolor=ACCENT, font=("Segoe UI", 10))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Card.TLabel", background=PANEL, foreground=FG)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        st.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 15))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        st.configure("TEntry", fieldbackground=PANEL2, foreground=FG, insertcolor=FG,
                     bordercolor="#39445a", padding=4)
        st.configure("TCombobox", fieldbackground=PANEL2, foreground=FG,
                     background=PANEL2, arrowcolor=FG, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL2)],
               foreground=[("readonly", FG)], selectbackground=[("readonly", ACCENT)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL2)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        st.configure("TCheckbutton", background=PANEL, foreground=FG)
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                     borderwidth=0, focusthickness=0, padding=(18, 9), font=("Segoe UI Semibold", 10))
        st.map("Accent.TButton", background=[("active", ACCENT2), ("disabled", "#3a4256")])
        st.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                     padding=(14, 8), focusthickness=0)
        st.map("TButton", background=[("active", "#39445a"), ("disabled", "#2a3140")],
               foreground=[("disabled", "#6d7889")])
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL2,
                     bordercolor=PANEL2, lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("Vertical.TScrollbar", background="#39445a", troughcolor="#141821",
                     bordercolor="#141821", arrowcolor=FG, lightcolor="#39445a",
                     darkcolor="#39445a", gripcount=0)
        st.configure("Horizontal.TScrollbar", background="#39445a", troughcolor="#141821",
                     bordercolor="#141821", arrowcolor=FG, lightcolor="#39445a",
                     darkcolor="#39445a", gripcount=0)
        st.map("Vertical.TScrollbar", background=[("active", "#46536d")])
        st.map("Horizontal.TScrollbar", background=[("active", "#46536d")])

    # -- сборка окна ------------------------------------------------------ #

    def _build(self):
        tk, ttk = self.tk, self.ttk
        root = self.root

        # --- область настроек: не выше ~430 px, при нехватке места прокручивается ---
        shell = ttk.Frame(root)
        shell.pack(fill="x")
        self.canvas = tk.Canvas(shell, bg=BG, highlightthickness=0, bd=0, height=320)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb = ttk.Scrollbar(shell, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)

        inner = ttk.Frame(self.canvas)
        self._inner = inner
        win = self.canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: self._fit_canvas())
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(win, width=e.width))

        def _wheel(event):
            # крутим только когда курсор над областью настроек, иначе не мешаем логу
            w = self.root.winfo_containing(event.x_root, event.y_root)
            while w is not None and w is not self.canvas:
                w = getattr(w, "master", None)
            if w is None:
                return
            if self._inner.winfo_reqheight() <= self.canvas.winfo_height():
                return
            step = -1 if (getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0) else 1
            self.canvas.yview_scroll(step, "units")
            return "break"

        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(seq, _wheel, add="+")

        head = ttk.Frame(inner, padding=(18, 14, 18, 6))
        head.pack(fill="x")
        ttk.Label(head, text="Скачиватель игры по IP / URL", style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, text="Radmin VPN, локальный сервер или любой адрес — "
                             "введи его как в браузере и нажми «Скачать».",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        card = ttk.Frame(inner, style="Card.TFrame", padding=14)
        card.pack(fill="x", padx=14, pady=(8, 6))
        card.columnconfigure(1, weight=1)

        # --- адрес ---
        ttk.Label(card, text="Адрес (URL или IP)", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(2, 10), pady=(0, 2))
        self._history = self._load_history()
        self.var_url = tk.StringVar(value=(self._history[0] if self._history else "http://26.0.0.1:8080/"))
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=1, column=0, columnspan=4, sticky="ew")
        row.columnconfigure(0, weight=1)
        self.entry_url = ttk.Entry(row, textvariable=self.var_url, font=("Consolas", 11))
        self.entry_url.grid(row=0, column=0, sticky="ew", ipady=2)
        self.entry_url.bind("<Return>", lambda e: self.start())
        self._enable_paste(self.entry_url)
        self.btn_hist = ttk.Button(row, text="▾", width=3, command=self._show_history)
        self.btn_hist.grid(row=0, column=1, padx=(6, 0))
        # ▼ — история прошлых адресов (показываем меню под кнопкой)
        self._hist_menu = tk.Menu(self.root, tearoff=0, bg=PANEL2, fg=FG,
                                  activebackground=ACCENT, activeforeground="#ffffff",
                                  bd=0, font=("Consolas", 10))
        ttk.Button(row, text="Проверить", command=self.check).grid(row=0, column=2, padx=(8, 0))
        self.var_autocheck = tk.BooleanVar(value=True)
        self._chk(row, "проверять адрес перед скачиванием", self.var_autocheck).grid(
            row=0, column=3, padx=(12, 4))

        ttk.Label(card, text="Примеры:  26.12.34.56:8080   ·   http://26.12.34.56:8080/game/   ·   "
                             "localhost:5173   ·   https://site.local/",
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=4, sticky="w",
                                             padx=2, pady=(6, 10))

        # --- папка назначения ---
        ttk.Label(card, text="Папка для сохранения", style="Card.TLabel").grid(
            row=3, column=0, sticky="w", padx=(2, 10), pady=(0, 2))
        self.var_out = tk.StringVar(value=self._default_out_folder("http://26.0.0.1:8080/"))
        self.entry_out = ttk.Entry(card, textvariable=self.var_out)
        self.entry_out.grid(row=4, column=0, columnspan=3, sticky="ew", padx=2, ipady=2)
        self._enable_paste(self.entry_out)
        ttk.Button(card, text="Обзор…", command=self.browse_out).grid(row=4, column=3, padx=(8, 0), sticky="e")

        # при изменении адреса — обновляем предложенную папку,
        # пока пользователь не тронул её вручную
        self._out_touched = False
        self.var_out.trace_add("write", lambda *a: setattr(self, "_out_touched", True))
        self.var_url.trace_add("write", lambda *a: self._sync_out_folder())

        # --- параметры ---
        params = ttk.Frame(card, style="Card.TFrame")
        params.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        params.columnconfigure(4, weight=1)

        ttk.Label(params, text="Потоков", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=2)
        self.var_threads = tk.StringVar(value="12")
        ttk.Combobox(params, textvariable=self.var_threads, values=("4", "8", "12", "16", "24", "32"),
                     width=5, state="readonly").grid(row=1, column=0, sticky="w", padx=2)

        ttk.Label(params, text="Глубина", style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=(14, 2))
        self.var_depth = tk.StringVar(value="0 (без лимита)")
        ttk.Combobox(params, textvariable=self.var_depth,
                     values=("0 (без лимита)", "1", "2", "3", "5", "8"),
                     width=14, state="readonly").grid(row=1, column=1, sticky="w", padx=(14, 2))

        ttk.Label(params, text="Лимит объёма, МБ", style="Muted.TLabel").grid(row=0, column=2, sticky="w", padx=(14, 2))
        self.var_maxmb = tk.StringVar(value="300")
        ttk.Combobox(params, textvariable=self.var_maxmb,
                     values=("50", "150", "300", "1000", "5000"),
                     width=7).grid(row=1, column=2, sticky="w", padx=(14, 2))

        self.var_assets = tk.BooleanVar(value=True)
        self.var_dirs = tk.BooleanVar(value=True)
        self.var_rewrite = tk.BooleanVar(value=True)
        self.var_serve = tk.BooleanVar(value=True)
        chks = ttk.Frame(card, style="Card.TFrame")
        chks.grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self._chk(chks, "искать ассеты внутри JS/JSON", self.var_assets).grid(
            row=0, column=0, sticky="w")
        self._chk(chks, "обходить каталоги (автоиндекс)", self.var_dirs).grid(
            row=0, column=1, sticky="w", padx=(22, 0))
        self._chk(chks, "переписать ссылки на локальные", self.var_rewrite).grid(
            row=1, column=0, sticky="w", pady=(3, 0))
        self._chk(chks, "после скачивания поднять локальный сервер", self.var_serve).grid(
            row=1, column=1, sticky="w", padx=(22, 0), pady=(3, 0))


        # --- кнопки ---
        btns = ttk.Frame(root, padding=(14, 6, 14, 0))
        btns.pack(fill="x")
        self.btn_start = ttk.Button(btns, text="⬇  Скачать", style="Accent.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="■  Стоп", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_serve = ttk.Button(btns, text="▶  Играть локально", command=self.toggle_serve)
        self.btn_serve.pack(side="left", padx=(8, 0))
        self.var_port = tk.StringVar(value="8000")
        ttk.Label(btns, text="порт", style="Sub.TLabel").pack(side="left", padx=(8, 4))
        self.entry_port = ttk.Entry(btns, textvariable=self.var_port, width=7, justify="center")
        self.entry_port.pack(side="left")
        self._enable_paste(self.entry_port)
        ttk.Button(btns, text="Открыть папку", command=self.open_folder).pack(side="right")

        btns2 = ttk.Frame(root, padding=(14, 6, 14, 2))
        btns2.pack(fill="x")
        self.btn_verify = ttk.Button(btns2, text="Проверить копию", command=self.verify_copy)
        self.btn_verify.pack(side="left")
        self.btn_zip = ttk.Button(btns2, text="Упаковать в ZIP", command=self.make_zip)
        self.btn_zip.pack(side="left", padx=(8, 0))
        ttk.Label(btns2, text="проверка ищет битые ссылки, ZIP удобно передать другу",
                  style="Sub.TLabel").pack(side="left", padx=(12, 0))

        # --- статус + прогресс ---
        stat = ttk.Frame(root, padding=(16, 2, 16, 2))
        stat.pack(fill="x")
        self.var_status = tk.StringVar(value="Готов. Введи адрес и нажми «Скачать».")
        self.lbl_status = tk.Label(stat, textvariable=self.var_status, bg=BG, fg=MUTED,
                                   font=("Segoe UI", 9), anchor="w")
        self.lbl_status.pack(fill="x")
        self.progress = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=16, pady=(2, 6))

        # --- лог ---
        logf = ttk.Frame(root, style="Card.TFrame", padding=2)
        logf.pack(fill="both", expand=True, padx=14, pady=(2, 10))
        self.txt = tk.Text(logf, bg="#141821", fg="#d8e0ea", insertbackground=FG,
                           relief="flat", bd=0, wrap="none", font=("Consolas", 9),
                           padx=10, pady=8, selectbackground=ACCENT)
        ysb = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
        xsb = ttk.Scrollbar(logf, orient="horizontal", command=self.txt.xview)
        self.txt.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.txt.tag_configure("ok", foreground=OK)
        self.txt.tag_configure("warn", foreground=WARN)
        self.txt.tag_configure("bad", foreground=BAD)
        self.txt.tag_configure("muted", foreground="#7d8798")
        self.txt.configure(state="disabled")

        self.log(f"site_mirror {VERSION}. Введи адрес сайта (можно просто IP:порт) и нажми «Скачать».",
                 "muted")
        self.log("Если сервер в Radmin VPN — порт обычно 8080, 3000, 5173 или 8000.", "muted")

    # -- мелкие помощники -------------------------------------------------- #

    def _chk(self, parent, text: str, var):
        """Своя галочка, нарисованная на Canvas: не зависит от шрифтов и темы."""
        tk = self.tk
        box = tk.Frame(parent, bg=PANEL)
        cv = tk.Canvas(box, width=18, height=18, bg=PANEL, bd=0,
                       highlightthickness=0, cursor="hand2")
        cv.pack(side="left")
        lbl = tk.Label(box, text=text, bg=PANEL, fg=FG, font=("Segoe UI", 9),
                       cursor="hand2", bd=0, highlightthickness=0, anchor="w")

        def render(*_a):
            cv.delete("all")
            on = bool(var.get())
            if on:
                cv.create_rectangle(2, 2, 16, 16, fill=ACCENT, outline=ACCENT)
                cv.create_line(5, 9, 8, 12, 13, 5, fill="#ffffff", width=2, capstyle="round")
            else:
                cv.create_rectangle(2, 2, 16, 16, fill=PANEL2, outline="#63728c")
            lbl.configure(fg=FG if on else "#c3ccda")

        def flip(_e=None):
            var.set(not var.get())

        def hover(_e=None):
            cv.configure(bg=PANEL)
            box.configure(bg=PANEL)

        lbl.pack(side="left", padx=(7, 0))
        for w in (cv, lbl):
            w.bind("<Button-1>", flip)
        var.trace_add("write", render)
        render()
        return box

    def _fit_canvas(self):
        """Высота области настроек = её содержимому, но не больше 430 px."""
        try:
            need = self._inner.winfo_reqheight()
        except Exception:  # noqa: BLE001
            return
        cap = 430
        self.canvas.configure(height=min(need, cap))
        if need > cap:
            if not self.vsb.winfo_ismapped():
                self.vsb.pack(side="right", fill="y")
        elif self.vsb.winfo_ismapped():
            self.vsb.pack_forget()

    def _enable_paste(self, widget):
        """В tkinter вставка зависит от ОС: добавляем все привычные комбинации."""
        def paste(_e=None):
            try:
                text = widget.clipboard_get()
            except Exception:  # noqa: BLE001
                try:
                    text = self.root.clipboard_get()
                except Exception:  # noqa: BLE001
                    return
            try:
                if widget.selection_present():
                    widget.delete("sel.first", "sel.last")
            except Exception:  # noqa: BLE001
                pass
            widget.insert("insert", text)

        for seq in ("<Control-v>", "<Control-V>", "<Control-Insert>", "<Shift-Insert>",
                    "<Button-2>", "<Control-Shift-v>", "<Control-Shift-V>"):
            widget.bind(seq, paste)

    # -- история адресов --------------------------------------------------- #

    @staticmethod
    def _history_path() -> Path:
        return Path.home() / ".site_mirror_history.json"

    def _load_history(self) -> list:
        try:
            data = json.loads(self._history_path().read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data][:10]
        except Exception:  # noqa: BLE001
            pass
        return []

    def _show_history(self):
        self._hist_menu.delete(0, "end")
        items = self._history[:10]
        if not items:
            self._hist_menu.add_command(label="пока пусто — адреса появятся здесь", state="disabled")
        seen = set()
        for h in items:
            if h in seen:
                continue
            seen.add(h)
            self._hist_menu.add_command(label=h, command=lambda v=h: self.var_url.set(v))
        try:
            x = self.btn_hist.winfo_rootx()
            y = self.btn_hist.winfo_rooty() + self.btn_hist.winfo_height() + 2
            self._hist_menu.tk_popup(x, y)
        finally:
            self._hist_menu.grab_release()

    def _remember(self, url: str):
        url = (url or "").strip()
        if not url:
            return
        self._history = ([url] + [h for h in self._history if h != url])[:10]
        try:
            self._history_path().write_text(
                json.dumps(self._history, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        try:
            self.btn_hist.configure(text=f"▾ {len(self._history)}")
        except Exception:  # noqa: BLE001
            pass

    def _default_out_folder(self, url: str) -> str:
        try:
            label = origin_label(parse_target(url))
        except Exception:  # noqa: BLE001
            label = "mirror"
        return str(APP_DIR / f"mirror_{label}")

    def _sync_out_folder(self):
        if getattr(self, "_out_touched", False):
            return
        self.var_out.set(self._default_out_folder(self.var_url.get()))

    def log(self, msg: str, tag: str = ""):
        self._log_q.put(f"{tag}\x00{msg}")

    def _pump(self):
        # переносим строки лога из потока в виджет (tkinter не любит чужие потоки)
        try:
            while True:
                item = self._log_q.get_nowait()
                tag, _, msg = item.partition("\x00")
                self.txt.configure(state="normal")
                self.txt.insert("end", msg + "\n", tag or ())
                self.txt.see("end")
                if int(self.txt.index("end-1c").split(".")[0]) > 5000:
                    self.txt.delete("1.0", "500.0")
                self.txt.configure(state="disabled")
        except queue.Empty:
            pass
        self._render_progress()
        self.root.after(120, self._pump)

    def _render_progress(self):
        p = self._prog
        if not p:
            return
        done, seen = p.get("done", 0), p.get("seen", 0)
        queued = p.get("queued", 0)
        total = max(seen, done + queued, 1)
        pct = min(100.0, done * 100.0 / total)
        self.progress["value"] = pct
        err = p.get("errors", 0)
        self.var_status.set(
            f"Скачано: {done}   ·   в очереди: {queued}   ·   найдено ссылок: {seen}   ·   "
            f"объём: {human(p.get('bytes_total', 0))}   ·   ошибок: {err}   ·   {pct:.0f}%")
        self.lbl_status.configure(fg=BAD if err else MUTED)

    def _get_out_dir(self) -> Path:
        raw = self.var_out.get().strip()
        if not raw:
            raw = self._default_out_folder(self.var_url.get())
            self.var_out.set(raw)
        out = Path(raw).expanduser()
        if not out.is_absolute():
            out = (APP_DIR / out).resolve()
        return out

    def _get_int(self, var, default: int) -> int:
        v = str(var.get()).strip().split(" ")[0]
        return int(v) if v.isdigit() else default

    def _set_running(self, running: bool):
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        for b in (self.btn_serve, self.btn_verify, self.btn_zip):
            b.configure(state="disabled" if running else "normal")

    def _on_close(self):
        if self.mirror and self.worker and self.worker.is_alive():
            if not self.messagebox.askokcancel("Идёт скачивание",
                                               "Скачивание ещё идёт. Прервать и закрыть?"):
                return
            self.mirror.stop_flag = True
        if self.server:
            self.server.stop()
        self.root.destroy()

    # -- проверка адреса --------------------------------------------------- #

    def check(self):
        raw = self.var_url.get()
        try:
            base = parse_target(raw, None)
        except Exception as e:  # noqa: BLE001
            self.messagebox.showerror("Адрес не разобран", f"{e}\n\nПример: 26.12.34.56:8080")
            return
        pu = urllib.parse.urlsplit(base)
        self.log(f"Проверяю {base} …", "muted")
        self.var_status.set(f"Проверяю {base} …")

        def work():
            try:
                url, title, headers = probe(pu.hostname, pu.port, pu.path, timeout=8)
            except Exception as e:  # noqa: BLE001
                self.log(f"✗ {e}", "bad")
                self.root.after(0, lambda: self.var_status.set("Сервер не отвечает — проверь IP/порт и Radmin VPN"))
                return
            srv = hget(headers, "Server")
            self.log(f"✓ Сервер ответил: {url}", "ok")
            if title:
                self.log(f"  Заголовок: {title}")
            if srv:
                self.log(f"  Server: {srv}")
            # http/https мог быть угадан неверно — поправим поле
            self.root.after(0, lambda: self._after_check(url, title))

        threading.Thread(target=work, daemon=True).start()

    def _after_check(self, url: str, title: str):
        self.var_status.set(f"Сервер отвечает: {url}" + (f" — «{title}»" if title else ""))
        pu, pt = urllib.parse.urlsplit(url), urllib.parse.urlsplit(parse_target(self.var_url.get(), None))
        if (pu.scheme, pu.port) != (pt.scheme, pt.port):
            self.var_url.set(url)
            self.log(f"  адрес уточнён: {url}", "muted")

    # -- скачивание -------------------------------------------------------- #

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            base = parse_target(self.var_url.get(), None)
        except Exception as e:  # noqa: BLE001
            self.messagebox.showerror("Адрес не разобран", f"{e}\n\nПример: 26.12.34.56:8080")
            return
        self._remember(base)
        out = self._get_out_dir()
        self.last_out = out
        self.progress["value"] = 0
        self._prog = {}
        self._set_running(True)
        self.var_status.set("Проверяю сервер…")

        cfg = Config(
            base_url=base,
            out_dir=out,
            threads=max(1, min(64, self._get_int(self.var_threads, 12))),
            depth=self._get_int(self.var_depth, 0),
            max_bytes=max(10, self._get_int(self.var_maxmb, 300)) * 1024 * 1024,
            max_file_bytes=200 * 1024 * 1024,
            extract_assets=bool(self.var_assets.get()),
            dir_sweep=bool(self.var_dirs.get()),
            rewrite=bool(self.var_rewrite.get()),
        )

        def work():
            try:
                if self.var_autocheck.get():
                    pu = urllib.parse.urlsplit(base)
                    try:
                        url_ok, title, _ = probe(pu.hostname, pu.port, pu.path, timeout=8)
                    except Exception as e:  # noqa: BLE001
                        self.log(f"✗ Сервер недоступен: {e}", "bad")
                        self.log("  Проверь: друг запустил сервер? вы в одной сети Radmin? порт верный?", "muted")
                        self.root.after(0, lambda: self._finish_run(None))
                        return
                    self.log(f"✓ Сервер ответил: {url_ok}" + (f" — «{title}»" if title else ""), "ok")
                    # если http не сработал, а https сработал (или наоборот) — используем рабочий
                    if origin_of(url_ok) != origin_of(base):
                        cfg.base_url = url_ok
                        cfg.origin = origin_of(url_ok)
                        base2 = url_ok
                    else:
                        base2 = base
                    self.root.after(0, lambda: self.var_url.set(base2))

                m = Mirror(cfg,
                           log_cb=lambda s: self.log(s, self._tag_for(s)),
                           progress_cb=self._on_progress)
                self.mirror = m
                m.run()
                if m.stop_flag:
                    self.log("Остановлено пользователем. Скачанное осталось в папке.", "warn")
                m.rewrite_all()
                m.report()
                self.root.after(0, lambda: self._finish_run(m))
            except Exception as e:  # noqa: BLE001
                import traceback
                self.log("Сбой: " + traceback.format_exc(limit=6), "bad")
                self.root.after(0, lambda: self._finish_run(None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    @staticmethod
    def _tag_for(s: str) -> str:
        s = s.strip()
        if s.startswith("✗"):
            return "bad"
        if s.startswith(("✓", "  ✓")):
            return "ok"
        if s.startswith(("!", "  !", "  ⚠", "⚠")):
            return "warn"
        if "индекс каталога" in s:
            return "ok"
        return ""

    def _on_progress(self, snap: dict):
        self._prog = snap

    def _finish_run(self, m: Mirror | None):
        self._set_running(False)
        self.progress["value"] = 100 if m else 0
        if not m:
            self.var_status.set("Не получилось. Смотри лог выше.")
            self.lbl_status.configure(fg=BAD)
            return
        files = sum(1 for p in m.cfg.out_dir.rglob("*") if p.is_file()) if m.cfg.out_dir.exists() else 0
        size = sum(p.stat().st_size for p in m.cfg.out_dir.rglob("*") if p.is_file()) \
            if m.cfg.out_dir.exists() else 0
        self.var_status.set(f"Готово: {files} файлов, {human(size)}, ошибок {len(m.errors)}")
        self.lbl_status.configure(fg=OK if not m.errors else WARN)
        if self.var_serve.get() and files:
            self.toggle_serve(True)

    def stop(self):
        if self.mirror:
            self.mirror.stop_flag = True
            self.log("Прошу потоки остановиться…", "warn")

    # -- локальный сервер -------------------------------------------------- #

    def toggle_serve(self, force_on: bool = False):
        if self.server:
            self.server.stop()
            self.server = None
            self.btn_serve.configure(text="▶  Играть локально")
            self.log("Локальный сервер остановлен.", "muted")
            self.var_status.set("Сервер остановлен.")
            return
        folder = self.last_out or self._get_out_dir()
        if not folder.exists() or not any(folder.iterdir()):
            self.messagebox.showinfo("Папка пуста", "Сначала скачай копию — папка пока пустая.")
            return
        port = self._get_int(self.var_port, 8000)
        try:
            self.server = LocalServer(folder, port).start()
        except OSError as e:
            self.messagebox.showerror("Порт занят", f"{e}\nПопробуй другой порт в поле «Порт сервера».")
            return
        self.btn_serve.configure(text="■  Остановить сервер")
        url = f"http://localhost:{port}/"
        self.log(f"Локальный сервер: {url}   (раздаю {folder})", "ok")
        self.var_status.set(f"Игра доступна: {url}")
        self.lbl_status.configure(fg=OK)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    # -- прочее ------------------------------------------------------------ #

    def browse_out(self):
        d = self.filedialog.askdirectory(initialdir=str(APP_DIR))
        if d:
            self._out_touched = True
            self.var_out.set(d)

    def open_folder(self):
        folder = self.last_out or self._get_out_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}" >/dev/null 2>&1 &')
        except Exception:  # noqa: BLE001
            self.messagebox.showinfo("Папка", str(folder))

    def verify_copy(self):
        folder = self.last_out or self._get_out_dir()
        if not folder.exists():
            self.messagebox.showinfo("Нет папки", f"Папки пока нет:\n{folder}")
            return
        self.log(f"\nПроверяю копию {folder} …", "muted")
        self.var_status.set("Проверяю ссылки в скачанной копии…")

        def work():
            import io
            import contextlib
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    rc = verify(folder, 40)
            except Exception as e:  # noqa: BLE001
                rc = -1
                buf.write(f"сбой: {e}\n")
            for line in buf.getvalue().splitlines():
                self.log("  " + line, "ok" if rc == 0 else ("warn" if rc == 1 else "bad"))
            self.root.after(0, lambda: self.var_status.set(
                "Проверка: всё на месте ✓" if rc == 0 else "Проверка: есть битые ссылки (см. лог)"))

        threading.Thread(target=work, daemon=True).start()

    def make_zip(self):
        folder = self.last_out or self._get_out_dir()
        if not folder.exists():
            self.messagebox.showinfo("Нет папки", f"Папки пока нет:\n{folder}")
            return
        self.log(f"Упаковываю {folder.name} в zip…", "muted")

        def work():
            try:
                if shutil.which("7z"):
                    pass
                archive = zip_folder(folder)
                self.log(f"✓ архив готов: {archive}", "ok")
            except Exception as e:  # noqa: BLE001
                self.log(f"✗ zip не удался: {e}", "bad")

        threading.Thread(target=work, daemon=True).start()

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------- #
#  Веб-интерфейс — локальная страница в браузере (движок: HTML + JS)
# --------------------------------------------------------------------------- #
#  Почему так: в браузере вставка из буфера работает всегда и везде
#  (Ctrl+V, правой кнопкой, кнопка «Вставить», автодополнение из истории),
#  ничего дополнительно ставить не нужно, а интерфейс выглядит нормально
#  на любом экране. Сервер — тот же Python, никаких зависимостей.

UI_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>site_mirror — скачиватель игры</title>
<style>
  :root{
    --bg:#12151c; --panel:#1b2029; --panel2:#232a36; --line:#2e3746;
    --fg:#e8eef7; --muted:#93a0b4; --accent:#4f8dfd; --accent2:#3b76e8;
    --ok:#54d68a; --warn:#f5b942; --bad:#ff6b6b; --mono:'Consolas','JetBrains Mono',ui-monospace,Menlo,monospace;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0; background:radial-gradient(1200px 600px at 70% -10%, #1c2434 0%, var(--bg) 60%);
    color:var(--fg); font:14px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;
    display:flex; flex-direction:column; min-height:100%;
  }
  header{padding:16px 22px 6px; display:flex; align-items:center; gap:14px; flex-wrap:wrap}
  header h1{font-size:19px; margin:0; font-weight:650; letter-spacing:.2px}
  header .sub{color:var(--muted); font-size:12.5px; margin-top:2px}
  .pill{
    margin-left:auto; padding:6px 12px; border-radius:999px; font-size:12.5px;
    background:var(--panel); border:1px solid var(--line); color:var(--muted);
    display:flex; align-items:center; gap:8px; white-space:nowrap;
  }
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
  .dot.run{background:var(--accent); animation:pulse 1.1s infinite}
  .dot.ok{background:var(--ok)} .dot.err{background:var(--bad)} .dot.warn{background:var(--warn)}
  @keyframes pulse{50%{opacity:.35}}

  main{flex:1; display:grid; grid-template-columns:390px minmax(0,1fr); gap:14px; padding:10px 22px 18px; align-items:start}
  @media (max-width:960px){ main{grid-template-columns:1fr} }

  .card{background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px}
  .card h2{margin:0 0 12px; font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); font-weight:600}

  label.f{display:block; font-size:12.5px; color:var(--muted); margin:0 0 5px}
  .row{display:flex; gap:8px; align-items:stretch}
  .wfull{width:100%; margin-top:8px}
  .row > .grow{flex:1; min-width:0}

  input[type=text], input[type=number], select{
    width:100%; background:var(--panel2); color:var(--fg); border:1px solid var(--line);
    border-radius:10px; padding:10px 12px; font:inherit; outline:none; transition:.15s;
  }
  input[type=text]:focus, input[type=number]:focus, select:focus{border-color:var(--accent); box-shadow:0 0 0 3px #4f8dfd26}
  input.mono{font-family:var(--mono); font-size:13px}
  .hint{color:var(--muted); font-size:11.5px; margin-top:6px}
  .hint b{color:#c3d0e4; font-weight:600}

  button{
    background:var(--panel2); color:var(--fg); border:1px solid var(--line); border-radius:10px;
    padding:10px 14px; font:inherit; font-weight:560; cursor:pointer; transition:.15s;
    display:inline-flex; align-items:center; gap:7px; white-space:nowrap;
  }
  button:hover{border-color:#3d4a5f; background:#2a3241}
  button:active{transform:translateY(1px)}
  button:disabled{opacity:.45; cursor:not-allowed; transform:none}
  button.primary{background:var(--accent); border-color:var(--accent); color:#fff}
  button.primary:hover{background:var(--accent2); border-color:var(--accent2)}
  button.ghost{background:transparent}
  button.small{padding:7px 10px; font-size:12.5px; border-radius:9px}
  button.icon{padding:9px 11px}

  .grid2{display:grid; grid-template-columns:1fr 1fr; gap:10px}
  .grid3{display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px}
  .mt{margin-top:12px}

  .checks{display:grid; gap:7px; margin-top:4px}
  .chk{display:flex; gap:9px; align-items:center; cursor:pointer; user-select:none; color:#cfd9e8; font-size:13px}
  .chk input{appearance:none; width:17px;height:17px;border-radius:5px;border:1.5px solid #5b6a83;background:var(--panel2); display:grid; place-items:center; cursor:pointer; margin:0; flex:none}
  .chk input:checked{background:var(--accent); border-color:var(--accent)}
  .chk input:checked::after{content:""; width:9px;height:5px;border-left:2px solid #fff;border-bottom:2px solid #fff;transform:rotate(-45deg) translate(1px,-1px)}

  .actions{display:flex; flex-wrap:wrap; gap:8px; margin-top:14px}
  .actions .primary{flex:1; justify-content:center; padding:12px 14px; font-size:15px}

  /* правая колонка */
  .stats{display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin-bottom:12px}
  @media (max-width:1200px){ .stats{grid-template-columns:repeat(3,1fr)} }
  @media (max-width:700px){ .stats{grid-template-columns:repeat(2,1fr)} }
  .stat{background:var(--panel2); border:1px solid var(--line); border-radius:11px; padding:9px 12px}
  .stat .k{font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em}
  .stat .v{font-size:17px; font-weight:650; font-variant-numeric:tabular-nums; margin-top:2px}
  .stat .v.bad{color:var(--bad)}

  .bar{height:10px; background:var(--panel2); border:1px solid var(--line); border-radius:99px; overflow:hidden}
  .bar > i{display:block; height:100%; width:0%; background:linear-gradient(90deg,var(--accent),#7aa7ff); transition:width .25s}
  .status{margin-top:9px; color:var(--muted); font-size:12.5px; min-height:18px}

  .logwrap{position:relative; margin-top:12px}
  #log{
    height:clamp(280px, 46vh, 560px); overflow:auto; background:#0e1218; border:1px solid var(--line);
    border-radius:12px; padding:12px 14px; font-family:var(--mono); font-size:12.5px; line-height:1.5;
    white-space:pre; color:#c7d2e2; tab-size:4;
  }
  #log .ok{color:var(--ok)} #log .warn{color:var(--warn)} #log .bad{color:var(--bad)} #log .muted{color:#7d8798}
  .logtools{display:flex; gap:8px; align-items:center; margin-top:8px; color:var(--muted); font-size:12px}
  .logtools .spacer{flex:1}
  .toast{
    position:fixed; left:50%; bottom:22px; transform:translateX(-50%) translateY(20px);
    background:#22406e; border:1px solid #3f6cb1; color:#eaf2ff; padding:10px 16px; border-radius:11px;
    font-size:13px; opacity:0; pointer-events:none; transition:.25s; z-index:50; max-width:80vw;
  }
  .toast.show{opacity:1; transform:translateX(-50%) translateY(0)}
  .toast.err{background:#5a2530; border-color:#a04a58}
  .toast.ok{background:#1f4a33; border-color:#3d7d58}
</style>
</head>
<body>
<header>
  <div>
    <h1>Скачиватель игры — по IP или URL</h1>
    <div class="sub">Radmin VPN · локальный сервер · любой адрес из браузера. Вставка: Ctrl+V — и страницы-«рамки» (Arena, CodePen) разворачиваются сами.</div>
  </div>
  <div class="pill"><span class="dot" id="dot"></span><span id="pilltext">готов</span></div>
</header>

<main>
  <!-- ЛЕВАЯ КОЛОНКА -->
  <section class="card">
    <h2>Адрес</h2>
    <label class="f" for="url">URL или IP</label>
    <div class="row">
      <div class="grow">
        <input id="url" class="mono" type="text" list="hist" spellcheck="false" autocomplete="off"
               placeholder="26.12.34.56:8080  ·  http://26.12.34.56:8080/game/">
      </div>
      <button id="btnPaste" title="Вставить из буфера обмена (Ctrl+V)">Вставить</button>
    </div>
    <datalist id="hist"></datalist>
    <div class="hint">Примеры: <b>26.12.34.56:8080</b> · <b>http://26.12.34.56:8080/game/</b> ·
      <b>localhost:5173</b> · <b>https://site.local/</b></div>
    <div class="row mt">
      <button id="btnCheck">Проверить адрес</button>
      <button class="ghost" id="btnOpenInBrowser" title="Открыть этот адрес в новой вкладке">Открыть в браузере</button>
    </div>
    <button class="ghost wfull" id="btnEmbed" title="Если площадка показывает сайт внутри своей рамки — берём внутренний адрес">Встроить сайт из рамки</button>
    <div class="hint" id="checkres"></div>

    <h2 style="margin-top:18px">Папка для сохранения</h2>
    <div class="row">
      <div class="grow"><input id="out" class="mono" type="text" spellcheck="false"></div>
      <button id="btnBrowse" title="Выбрать папку">Обзор…</button>
    </div>

    <h2 style="margin-top:18px">Параметры</h2>
    <div class="grid3">
      <div>
        <label class="f" for="threads">Потоков</label>
        <select id="threads">
          <option>4</option><option>8</option><option selected>12</option>
          <option>16</option><option>24</option><option>32</option>
        </select>
      </div>
      <div>
        <label class="f" for="depth">Глубина</label>
        <select id="depth">
          <option value="0" selected>без лимита</option><option value="1">1</option>
          <option value="2">2</option><option value="3">3</option><option value="5">5</option><option value="8">8</option>
        </select>
      </div>
      <div>
        <label class="f" for="maxmb">Лимит, МБ</label>
        <input id="maxmb" type="number" min="10" step="10" value="300">
      </div>
    </div>

    <div class="checks mt">
      <label class="chk"><input type="checkbox" id="optAssets" checked> искать ассеты внутри JS/JSON</label>
      <label class="chk"><input type="checkbox" id="optDirs" checked> обходить каталоги (автоиндекс)</label>
      <label class="chk"><input type="checkbox" id="optRewrite" checked> переписать ссылки на локальные</label>
      <label class="chk"><input type="checkbox" id="optServe"> после скачивания поднять локальный сервер</label>
    </div>

    <div class="actions">
      <button class="primary" id="btnStart">⬇ Скачать</button>
      <button id="btnStop" disabled>■ Стоп</button>
    </div>
    <div class="actions">
      <button class="small" id="btnPlay">▶ Играть локально</button>
      <input id="port" class="mono" type="text" value="8000" style="width:74px; padding:7px 9px; text-align:center">
      <button class="small" id="btnVerify">Проверить копию</button>
      <button class="small" id="btnZip">ZIP</button>
      <button class="small ghost" id="btnOpen">Открыть папку</button>
    </div>
  </section>

  <!-- ПРАВАЯ КОЛОНКА -->
  <section class="card">
    <h2>Ход работы</h2>
    <div class="stats">
      <div class="stat"><div class="k">Скачано</div><div class="v" id="sDone">0</div></div>
      <div class="stat"><div class="k">В очереди</div><div class="v" id="sQueue">0</div></div>
      <div class="stat"><div class="k">Найдено ссылок</div><div class="v" id="sSeen">0</div></div>
      <div class="stat"><div class="k">Объём</div><div class="v" id="sSize">0 Б</div></div>
      <div class="stat"><div class="k">Ошибки</div><div class="v" id="sErr">0</div></div>
    </div>
    <div class="bar"><i id="barfill"></i></div>
    <div class="status" id="status">Готов. Введи адрес и нажми «Скачать».</div>

    <div class="logwrap">
      <div id="errbox" class="errbox" style="display:none">
        <div class="errhead">
          <b id="errTitle">Ошибки</b>
          <span class="grow"></span>
          <button class="mini" id="errCopy">Скопировать</button>
        </div>
        <div class="errlist" id="errList"></div>
      </div>
      <div id="log"></div>
      <div class="logtools">
        <label class="chk" style="font-size:12px"><input type="checkbox" id="autoscroll" checked> автопрокрутка</label>
        <div class="spacer"></div>
        <button class="small ghost" id="btnCopyLog">Копировать лог</button>
        <button class="small ghost" id="btnClear">Очистить</button>
      </div>
    </div>
  </section>
</main>

<div class="toast" id="toast"></div>

<script>
(() => {
  const $ = id => document.getElementById(id);
  const el = {
    url: $('url'), out: $('out'), hist: $('hist'), threads: $('threads'), depth: $('depth'),
    maxmb: $('maxmb'), assets: $('optAssets'), dirs: $('optDirs'), rewrite: $('optRewrite'),
    serve: $('optServe'), port: $('port'), log: $('log'), dot: $('dot'), pill: $('pilltext'),
    bar: $('barfill'), status: $('status'), checkres: $('checkres'),
    sDone: $('sDone'), sQueue: $('sQueue'), sSeen: $('sSeen'), sSize: $('sSize'), sErr: $('sErr'),
    btnStart: $('btnStart'), btnStop: $('btnStop'), btnCheck: $('btnCheck'), btnPlay: $('btnPlay'),
    btnVerify: $('btnVerify'), btnZip: $('btnZip'), btnOpen: $('btnOpen'), btnBrowse: $('btnBrowse'),
    btnPaste: $('btnPaste'), autoscroll: $('autoscroll'), toast: $('toast'),
    btnOpenInBrowser: $('btnOpenInBrowser'), btnEmbed: $('btnEmbed'),
    errbox: $('errbox'), errList: $('errList'), errTitle: $('errTitle'), errCopy: $('errCopy'),
  };

  let state = {job: null, server: null, cfg: {docs: '', app_dir: ''}};
  let rendered = 0;         // сколько строк лога уже выведено
  let currentJobId = null;  // id текущего задания (чтобы начать лог с нуля при новом)
  let busy = false;         // идёт скачивание
  let pollTimer = null;

  // ---------- утилиты ----------
  function toast(msg, kind) {
    el.toast.textContent = msg;
    el.toast.className = 'toast show ' + (kind || '');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.toast.className = 'toast ' + (kind || ''), 3400);
  }
  function human(n) {
    if (!n) return '0 Б';
    const u = ['Б', 'КБ', 'МБ', 'ГБ']; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + u[i];
  }
  async function api(path, body) {
    const opt = body === undefined ? {method: 'GET'} :
      {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
    const r = await fetch(path, opt);
    let data = {};
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }
  function setBusy(v) {
    busy = v;
    el.btnStart.disabled = v;
    el.btnStop.disabled = !v;
    el.btnCheck.disabled = v;
  }
  function setPill(kind, text) {
    el.dot.className = 'dot ' + kind;
    el.pill.textContent = text;
  }

  // ---------- лог ----------
  function addLines(lines) {
    const frag = document.createDocumentFragment();
    for (const ln of lines) {
      const d = document.createElement('div');
      if (ln.k) d.className = ln.k;
      d.textContent = ln.m;
      frag.appendChild(d);
    }
    el.log.appendChild(frag);
    while (el.log.childElementCount > 3000) el.log.removeChild(el.log.firstChild);
    if (el.autoscroll.checked) el.log.scrollTop = el.log.scrollHeight;
  }
  function clearLog() { el.log.innerHTML = ''; rendered = 0; }
  $('btnClear').onclick = clearLog;
  $('btnCopyLog').onclick = () => {
    const text = Array.from(el.log.children).map(d => d.textContent).join('\n');
    navigator.clipboard.writeText(text)
      .then(() => toast('Лог скопирован в буфер', 'ok'))
      .catch(() => toast('Не дал доступ к буферу — выдели и скопируй вручную', 'err'));
  };

  // ---------- история адресов ----------
  function renderHistory(items) {
    el.hist.innerHTML = '';
    for (const h of items) {
      const o = document.createElement('option');
      o.value = h; el.hist.appendChild(o);
    }
  }

  // ---------- состояние ----------
  // Панель «что именно не получилось» — по-русски и с адресами
  let errShown = 0;
  // безопасный вывод текста в HTML (адреса могут содержать &, <, кавычки)
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => (
      {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]
    ));
  }
  function renderErrors(job) {
    const items = (job && job.error_items) || [];
    const total = (job && job.errors) || 0;
    if (!job || (!total && !(job.notes && job.notes.length))) {
      if (errShown) { el.errbox.style.display = 'none'; errShown = 0; }
      return;
    }
    const notes = job.notes || [];
    el.errTitle.textContent = total
      ? `Не скачалось: ${total} ${plural(total, 'файл', 'файла', 'файлов')}`
      : 'Замечания';
    // перерисовываем только если набор изменился, чтобы не мигало
    const sig = total + ':' + items.length + ':' + notes.join('|');
    if (sig === errShown) { el.errbox.style.display = 'block'; return; }
    errShown = sig;
    const rows = [];
    for (const n of notes) rows.push(`<div>💡 ${esc(n)}</div>`);
    for (const it of items.slice(0, 60)) {
      rows.push(`<div><b>${esc(it.url || '')}</b><br><span class="why">${esc(it.err || '')}</span></div>`);
    }
    if (items.length > 60) rows.push(`<div>… и ещё ${items.length - 60}</div>`);
    el.errList.innerHTML = rows.join('');
    el.errbox.style.display = 'block';
  }

  function plural(n, one, few, many) {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  }

  el.errCopy.onclick = async () => {
    const t = Array.from(el.errList.querySelectorAll('div')).map(d => d.innerText).join('\n');
    try { await navigator.clipboard.writeText(t); toast('Список ошибок скопирован'); }
    catch (e) { toast('Не удалось скопировать — выдели текст мышью', 'err'); }
  };

  function applyState(s) {
    state = s;
    renderHistory(s.history || []);
    const p = s.progress || {};
    el.sDone.textContent = p.done ?? 0;
    el.sQueue.textContent = p.queued ?? 0;
    el.sSeen.textContent = p.seen ?? 0;
    el.sSize.textContent = human(p.bytes_total ?? 0);
    el.sErr.textContent = p.errors ?? 0;
    el.sErr.className = 'v' + ((p.errors ?? 0) > 0 ? ' bad' : '');

    const total = Math.max(p.seen || 0, (p.done || 0) + (p.queued || 0), 1);
    const pct = Math.min(100, ((p.done || 0) * 100) / total);
    el.bar.style.width = pct.toFixed(1) + '%';

    const job = s.job;
    if (job && job.state === 'running') {
      setBusy(true);
      setPill('run', 'скачиваю… ' + pct.toFixed(0) + '%');
      el.status.textContent = 'Скачивание идёт — окно можно свернуть, работа продолжится.';
    } else if (job && job.state === 'done') {
      setBusy(false);
      const suffix = s.server ? ' · сервер включён' : '';
      setPill((job.errors || 0) ? 'warn' : 'ok',
              ((job.errors || 0) ? 'готово, с ошибками' : 'готово') + suffix);
      el.status.textContent = `Готово: ${job.files} файлов, ${human(job.size)}` +
        (job.errors ? `, ошибок ${job.errors}` : '') + (job.seconds ? `, за ${job.seconds.toFixed(1)} c` : '');
      el.bar.style.width = '100%';
    } else if (job && job.state === 'stopped') {
      setBusy(false);
      setPill('warn', 'остановлено');
      el.status.textContent = 'Остановлено. Скачанное осталось в папке.';
    } else if (job && job.state === 'error') {
      setBusy(false);
      setPill('err', 'ошибка');
      el.status.textContent = 'Сбой: ' + (job.error || 'см. лог');
    } else {
      setBusy(false);
      setPill(s.server ? 'ok' : '', s.server ? 'локальный сервер включён' : 'готов');
      if (s.server) el.status.textContent = 'Игра доступна: ' + s.server.url;
    }
    el.btnPlay.textContent = s.server ? '■ Остановить сервер' : '▶ Играть локально';

    renderErrors(job);

    // лог: докидываем только новые строки (сервер отдаёт «всё после last»)
    const jobId = (job && job.id) || null;
    if (jobId !== currentJobId) {
      // новое скачивание — начинаем лог с чистого листа
      currentJobId = jobId;
      clearLog();
      rendered = 0;
      schedule(60);
      return;
    }
    const lines = (job && job.lines) || {items: [], total: 0};
    if ((lines.total || 0) < rendered) { clearLog(); rendered = 0; }
    if (lines.items && lines.items.length) addLines(lines.items);
    if (lines.total) rendered = Math.max(rendered, lines.total);

    if (job && job.state === 'running') schedule(400);
    else if (document.hidden) schedule(3000);
    else schedule(1200);
  }
  function schedule(ms) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, ms);
  }
  // когда вкладка скрыта, опрос реже: не мешаем браузеру «уснуть»
  document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(0); });
  async function poll() {
    try {
      const s = await api('/api/state?last=' + rendered);
      applyState(s);
    } catch (e) {
      setPill('err', 'сервер интерфейса не отвечает');
      schedule(2500);
    }
  }

  // ---------- проверка адреса ----------
  async function check() {
    const url = el.url.value.trim();
    if (!url) { toast('Введи адрес', 'err'); return; }
    el.checkres.textContent = 'проверяю…';
    el.btnCheck.disabled = true;
    try {
      const r = await api('/api/check', {url});
      el.checkres.textContent = '✓ ' + (r.title ? r.title : r.final);
      el.url.value = r.final;
      toast('Сервер отвечает: ' + r.final, 'ok');
      syncOutFolder();
    } catch (e) {
      el.checkres.textContent = '✗ не отвечает';
      toast('Не достучался: ' + e.message, 'err');
    } finally {
      el.btnCheck.disabled = false;
    }
  }

  // ---------- папка ----------
  let outTouched = false;
  function syncOutFolder() {
    if (outTouched) return;
    const url = el.url.value.trim();
    if (!url) return;
    api('/api/suggest_out', {url}).then(r => { if (!outTouched) el.out.value = r.out; }).catch(() => {});
  }

  // ---------- запуск ----------
  async function start() {
    if (busy) return;
    const url = el.url.value.trim();
    if (!url) { toast('Введи адрес сервера', 'err'); el.url.focus(); return; }
    clearLog();
    try {
      const r = await api('/api/start', {
        url,
        out: el.out.value.trim(),
        threads: +el.threads.value,
        depth: +el.depth.value,
        maxmb: +el.maxmb.value || 300,
        assets: el.assets.checked,
        dirs: el.dirs.checked,
        rewrite: el.rewrite.checked,
        serve_after: el.serve.checked,
        port: +el.port.value || 8000,
      });
      if (r.out) el.out.value = r.out;
      setBusy(true);
      setPill('run', 'проверяю сервер…');
      schedule(250);
    } catch (e) {
      toast(e.message, 'err');
    }
  }
  async function stop() {
    try { await api('/api/stop', {}); toast('Останавливаю…', 'warn'); } catch (e) {}
  }
  async function togglePlay() {
    try {
      const r = await api('/api/serve', {
        folder: el.out.value.trim(),
        port: +el.port.value || 8000,
        action: state.server ? 'stop' : 'start',
      });
      if (r.url) { toast('Игра: ' + r.url, 'ok'); window.open(r.url, '_blank'); }
      else toast('Сервер остановлен', 'ok');
      schedule(200);
    } catch (e) { toast(e.message, 'err'); }
  }
  async function verifyCopy() {
    toast('Проверяю копию…');
    try {
      const r = await api('/api/verify', {folder: el.out.value.trim()});
      toast(r.summary, r.ok ? 'ok' : 'err');
      schedule(200);
    } catch (e) { toast(e.message, 'err'); }
  }
  async function makeZip() {
    toast('Упаковываю…');
    try {
      const r = await api('/api/zip', {folder: el.out.value.trim()});
      toast('Архив: ' + r.archive, 'ok');
    } catch (e) { toast(e.message, 'err'); }
  }
  async function openFolder() {
    try { await api('/api/open', {folder: el.out.value.trim()}); }
    catch (e) { toast(e.message, 'err'); }
  }
  async function browse() {
    try {
      const r = await api('/api/pickfolder', {});
      if (r.folder) { el.out.value = r.folder; outTouched = true; }
    } catch (e) { toast('Диалог недоступен — впиши путь руками', 'err'); }
  }
  async function pasteFromClipboard() {
    try {
      const t = await navigator.clipboard.readText();
      if (t) {
        el.url.value = t.trim();
        syncOutFolder();
        toast('Вставлено');
      } else toast('Буфер пуст');
    } catch (e) {
      toast('Браузер не дал доступ к буферу — жми Ctrl+V в поле', 'err');
      el.url.focus();
    }
  }

  // ---------- обработчики ----------
  el.btnStart.onclick = start;
  el.btnStop.onclick = stop;
  el.btnCheck.onclick = check;
  el.btnPlay.onclick = togglePlay;
  el.btnVerify.onclick = verifyCopy;
  el.btnZip.onclick = makeZip;
  el.btnOpen.onclick = openFolder;
  el.btnBrowse.onclick = browse;
  el.btnPaste.onclick = pasteFromClipboard;
  // Площадки-«обёртки» (Arena, CodePen…) держат игру в iframe.
  // Берём адрес этого iframe и скачиваем уже его.
  el.btnEmbed.onclick = async () => {
    const u = el.url.value.trim();
    if (!u) { toast('Сначала введи адрес', 'err'); return; }
    toast('Смотрю, есть ли внутри рамка с игрой…');
    let html = '';
    try {
      const r = await fetch("/api/embed?url=" + encodeURIComponent(u)).then(r => r.json());
      if (r.ok && r.inner) {
        el.url.value = r.inner;
        toast('Нашёл внутренний адрес: ' + r.inner);
        return;
      }
      if (r.ok && !r.inner && r.note) { toast(r.note, 'warn'); return; }
      toast(r.error || 'Рамку не нашёл', 'err');
    } catch (e) { toast('Не получилось: ' + e, 'err'); }
  };

  el.btnOpenInBrowser.onclick = () => {
    const u = el.url.value.trim();
    if (!u) { toast('Сначала введи адрес', 'err'); return; }
    window.open(/^https?:\/\//i.test(u) ? u : ('http://' + u), '_blank');
  };
  document.addEventListener('paste', () => setTimeout(syncOutFolder, 30));
  el.url.addEventListener('input', () => setTimeout(syncOutFolder, 350));
  el.url.addEventListener('keydown', e => { if (e.key === 'Enter') start(); });
  el.out.addEventListener('input', () => { outTouched = true; });
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); start(); }
  });

  // первый запрос состояния
  poll();
})();
</script>
</body>
</html>
"""


def _tag_for(s: str) -> str:
    s = s.strip()
    if s.startswith("✗"):
        return "bad"
    if s.startswith(("✓", "  ✓")):
        return "ok"
    if s.startswith(("!", "  !", "  ⚠", "⚠")):
        return "warn"
    if "индекс каталога" in s:
        return "ok"
    return ""


class WebJob:
    """Одно скачивание: живой лог, прогресс и состояние для страницы."""

    MAX_LINES = 4000

    def __init__(self, cfg: Config, serve_after: bool = False, serve_port: int = 8000):
        self.cfg = cfg
        self.serve_after = serve_after
        self.serve_port = serve_port
        self.state = "running"          # running | done | stopped | error
        self.error = ""
        self.mirror: Mirror | None = None
        self.lines: list[dict] = []
        self.line_no = 0
        self.progress: dict = {}
        self.started = time.time()
        self.finished: float | None = None
        self.files = 0
        self.size = 0
        self.errors = 0
        self.error_items: list[dict] = []
        self.notes: list[str] = []       # пояснения «почему не вышло»
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.job_id = f"{int(time.time() * 1000):x}"

    # -- лог ------------------------------------------------------------- #
    def add_line(self, msg: str, tag: str | None = None):
        with self.lock:
            self.line_no += 1
            self.lines.append({"n": self.line_no, "k": tag if tag is not None else _tag_for(msg),
                               "m": msg})
            if len(self.lines) > self.MAX_LINES:
                del self.lines[: len(self.lines) - self.MAX_LINES]

    def lines_since(self, last: int, limit: int = 800) -> tuple[list, int]:
        with self.lock:
            items = [ln for ln in self.lines if ln["n"] > last]
            return items[-limit:], self.line_no

    def on_progress(self, snap: dict):
        with self.lock:
            self.progress = dict(snap)

    @staticmethod
    def files_is_empty(m) -> bool:
        """Игра не сохранилась: нет ни одной HTML-страницы (favicon и отчёт не в счёт)."""
        try:
            if any(rel.lower().endswith((".html", ".htm")) for rel in m.map.values()):
                return False
            out = m.cfg.out_dir
            if not out.exists():
                return True
            return not any(p.suffix.lower() in (".html", ".htm") for p in out.rglob("*") if p.is_file())
        except Exception:  # noqa: BLE001
            return False

    def add_note(self, text: str):
        with self.lock:
            if text not in self.notes:
                self.notes.append(text)

    # -- работа ---------------------------------------------------------- #
    def run(self):
        try:
            m = Mirror(self.cfg, log_cb=self.add_line, progress_cb=self.on_progress)
            self.mirror = m
            # Подсказка «сайта тут нет» появляется из самого Mirror (см. handle),
            # поэтому отдельная проверка перед стартом не нужна: она бы
            # останавливала скачивание раньше времени.
            try:
                m.run()
            except KeyboardInterrupt:
                m.stop_flag = True
            if m.stop_flag:
                self.state = "stopped"
                self.add_line("Остановлено пользователем. Скачанное осталось в папке.", "warn")
            m.ensure_index()
            m.rewrite_all()
            m.report()
            self.errors = max(m.errors_total, len(m.errors))
            self.error_items = [{"url": u, "err": e} for u, e in m.errors[:300]]

            # --- объясняем по-человечески, что пошло не так ---
            errs_text = " ".join(e for _u, e in m.errors).lower()
            if m.errors and self.files_is_empty(m):
                if "не размещён" in errs_text or "there isn" in errs_text:
                    self.add_note("Сайт по этому адресу не выложен — скачивать нечего. "
                                  "Проверь ссылку в браузере: там должна открываться игра, "
                                  "а не заглушка хостинга.")
                elif "404" in errs_text or "not found" in errs_text:
                    self.add_note("По этому адресу ничего не нашлось (404). Проверь ссылку: "
                                  "возможно, она устарела или в ней опечатка — открой её в браузере.")
                else:
                    self.add_note("Не удалось ничего скачать. Скорее всего, адрес недоступен "
                                  "или сервер требует авторизацию.")
            elif m.errors:
                self.add_note("Часть файлов не скачалась — по каждому адресу причина ниже.")

            folder = self.cfg.out_dir
            self.files = sum(1 for p in folder.rglob("*") if p.is_file()) if folder.exists() else 0
            self.size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()) \
                if folder.exists() else 0
            if self.state == "running":
                self.state = "done"

            if self.serve_after and self.files:
                try:
                    start_local_server(folder, self.serve_port)
                    self.add_line(f"Локальный сервер: http://localhost:{self.serve_port}/", "ok")
                except OSError as e:
                    self.add_line(f"Не смог занять порт {self.serve_port}: {e}", "bad")
        except Exception as e:  # noqa: BLE001
            import traceback
            self.state = "error"
            self.error = f"{type(e).__name__}: {e}"
            self.add_line("Сбой: " + traceback.format_exc(limit=6), "bad")
        finally:
            self.finished = time.time()

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def to_json(self, last_line: int = 0) -> dict:
        items, total = self.lines_since(last_line)
        return {
            "id": self.job_id,
            "state": self.state,
            "error": self.error,
            "files": self.files,
            "size": self.size,
            "errors": self.errors,
            "error_items": self.error_items,
            "notes": self.notes,
            "seconds": (self.finished or time.time()) - self.started,
            "base_url": self.cfg.base_url,
            "out": str(self.cfg.out_dir),
            "lines": {"items": items, "total": total},
        }


# --- общее состояние интерфейса --------------------------------------------- #

UI_STATE = {
    "job": None,          # WebJob | None
    "server": None,       # LocalServer | None
    "server_url": "",
    "server_folder": "",
    "history": [],
}


def history_path() -> Path:
    return Path.home() / ".site_mirror_history.json"


def load_history() -> list:
    try:
        data = json.loads(history_path().read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data][:12]
    except Exception:  # noqa: BLE001
        pass
    return []


def save_history(items: list):
    try:
        history_path().write_text(json.dumps(items[:12], ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    except OSError:
        pass


def remember_url(url: str):
    items = [url] + [h for h in UI_STATE["history"] if h != url]
    UI_STATE["history"] = items[:12]
    save_history(UI_STATE["history"])


def start_local_server(folder: Path, port: int) -> str:
    stop_local_server()
    srv = LocalServer(folder, port).start()
    UI_STATE["server"] = srv
    UI_STATE["server_url"] = f"http://localhost:{port}/"
    UI_STATE["server_folder"] = str(folder)
    return UI_STATE["server_url"]


def stop_local_server():
    srv = UI_STATE.get("server")
    if srv:
        srv.stop()
    UI_STATE["server"] = None
    UI_STATE["server_url"] = ""
    UI_STATE["server_folder"] = ""


def _resolve_folder(raw: str) -> Path:
    raw = (raw or "").strip().strip('"')
    if not raw:
        raise ValueError("не указана папка")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (APP_DIR / p).resolve()
    return p


# --- HTTP-сервер интерфейса -------------------------------------------------- #

class UIHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"site_mirror-ui/{VERSION}"
    protocol_version = "HTTP/1.1"

    # -- отправка ответов -------------------------------------------------- #
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400):
        self._json({"ok": False, "error": msg}, code)

    def log_message(self, fmt, *args):   # не засоряем консоль запросами страницы
        pass

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    # -- GET --------------------------------------------------------------- #
    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, UI_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._state()
            return
        if path == "/api/embed":
            try:
                self._embed()
            except ValueError as e:
                self._err(str(e))
            except Exception as e:  # noqa: BLE001
                self._err(f"{type(e).__name__}: {e}", 500)
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._err("нет такого адреса", 404)

    def _state(self):
        job = UI_STATE.get("job")
        try:
            last = int(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                       .get("last", ["0"])[0])
        except ValueError:
            last = 0
        # клиент каждый раз просит «всё после последней строки»
        payload = {
            "job": job.to_json(last) if job else None,
            "progress": job.progress if job else {},
            "server": ({"url": UI_STATE["server_url"], "folder": UI_STATE["server_folder"]}
                       if UI_STATE.get("server") else None),
            "history": UI_STATE["history"],
            "app_dir": str(APP_DIR),
            "version": VERSION,
        }
        self._json(payload)

    # -- POST -------------------------------------------------------------- #
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        data = self._body()
        try:
            if path == "/api/check":
                self._check(data)
            elif path == "/api/suggest_out":
                self._suggest_out(data)
            elif path == "/api/start":
                self._start(data)
            elif path == "/api/stop":
                self._stop()
            elif path == "/api/serve":
                self._serve(data)
            elif path == "/api/verify":
                self._verify(data)
            elif path == "/api/zip":
                self._zip(data)
            elif path == "/api/open":
                self._open(data)
            elif path == "/api/pickfolder":
                self._pickfolder()
            elif path == "/api/embed":
                self._embed()
            else:
                self._err("нет такого метода", 404)
        except ValueError as e:
            self._err(str(e))
        except Exception as e:  # noqa: BLE001
            self._err(f"{type(e).__name__}: {e}", 500)

    # -- обработчики ------------------------------------------------------- #
    def _check(self, data: dict):
        base = parse_target(data.get("url", ""))
        pu = urllib.parse.urlsplit(base)
        url_ok, title, headers = probe(pu.hostname, pu.port, pu.path, timeout=8)
        self._json({
            "ok": True,
            "base": base,
            "final": url_ok,
            "title": title or "",
            "server": hget(headers, "Server"),
        })

    def _embed(self):
        """Ищет iframe внутри страницы-рамки и возвращает адрес настоящего сайта."""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        raw = (qs.get("url") or [""])[0]
        base = parse_target(raw)
        pu = urllib.parse.urlsplit(base)
        _u, _t, _h, body = probe(pu.hostname, pu.port, pu.path, timeout=10, return_body=True)
        inner = None
        m = re.search(r"<iframe[^>]+src=\s*[\"']([^\"']+)[\"']", body, re.I)
        if m:
            inner = urllib.parse.urljoin(base, m.group(1).strip())
            if inner.split("?")[0].rstrip("/") == base.split("?")[0].rstrip("/") and "embed" not in inner:
                inner = inner + ("&" if "?" in inner else "?") + "embed=true"
        if not inner:
            self._json({"ok": True, "inner": "",
                        "note": "Это обычная страница, рамки с игрой внутри нет — "
                                "её и надо скачивать, адрес уже правильный."})
            return
        self._json({"ok": True, "inner": inner})

    def _suggest_out(self, data: dict):
        try:
            base = parse_target(data.get("url", ""))
            name = f"mirror_{origin_label(base)}"
        except Exception:  # noqa: BLE001
            name = "mirror"
        self._json({"out": str(APP_DIR / name)})

    def _start(self, data: dict):
        job = UI_STATE.get("job")
        if job and job.state == "running":
            self._err("Скачивание уже идёт — сначала нажми «Стоп»")
            return
        base = parse_target(data.get("url", ""))
        remember_url(base)

        raw_out = (data.get("out") or "").strip()
        out = _resolve_folder(raw_out) if raw_out else APP_DIR / f"mirror_{origin_label(base)}"

        threads = int(data.get("threads") or 12)
        depth = int(data.get("depth") or 0)
        maxmb = max(10, int(data.get("maxmb") or 300))
        serve_after = bool(data.get("serve_after"))
        serve_port = int(data.get("port") or 8000)

        cfg = Config(
            base_url=base,
            out_dir=out,
            threads=max(1, min(64, threads)),
            depth=max(0, depth),
            max_bytes=maxmb * 1024 * 1024,
            extract_assets=bool(data.get("assets", True)),
            dir_sweep=bool(data.get("dirs", True)),
            rewrite=bool(data.get("rewrite", True)),
        )
        out.mkdir(parents=True, exist_ok=True)
        job = WebJob(cfg, serve_after=serve_after, serve_port=serve_port)
        UI_STATE["job"] = job
        job.start()
        self._json({"ok": True, "out": str(out)})

    def _stop(self):
        job = UI_STATE.get("job")
        if job and job.mirror:
            job.mirror.stop_flag = True
        self._json({"ok": True})

    def _serve(self, data: dict):
        action = data.get("action") or "start"
        if action == "stop":
            stop_local_server()
            self._json({"ok": True})
            return
        folder = _resolve_folder(data.get("folder", ""))
        if not folder.exists() or not any(folder.iterdir()):
            self._err(f"Папка пустая или её нет: {folder}")
            return
        port = int(data.get("port") or 8000)
        try:
            url = start_local_server(folder, port)
        except OSError as e:
            self._err(f"Порт {port} занят ({e}). Поменяй порт в поле рядом с кнопкой.")
            return
        self._json({"ok": True, "url": url})

    def _verify(self, data: dict):
        folder = _resolve_folder(data.get("folder", ""))
        if not folder.exists():
            self._err(f"Папки нет: {folder}")
            return
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = verify(folder, 40)
        job = UI_STATE.get("job")
        for line in buf.getvalue().splitlines():
            if job:
                job.add_line("  " + line, "ok" if rc == 0 else ("warn" if rc == 1 else "bad"))
        summary = ("Проверка: всё на месте ✓" if rc == 0
                   else "Проверка: есть битые ссылки — смотри лог" if rc == 1
                   else "Проверка не удалась")
        self._json({"ok": rc == 0, "summary": summary})

    def _zip(self, data: dict):
        folder = _resolve_folder(data.get("folder", ""))
        if not folder.exists():
            self._err(f"Папки нет: {folder}")
            return
        archive = zip_folder(folder)
        self._json({"ok": True, "archive": str(archive)})

    def _open(self, data: dict):
        folder = _resolve_folder(data.get("folder", ""))
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}" >/dev/null 2>&1 &')
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"не смог открыть папку: {e}") from e
        self._json({"ok": True})

    def _pickfolder(self):
        """Нативный диалог выбора папки — это локальная программа, так можно."""
        mods = import_tk()
        if not mods:
            self._err("tkinter нет — впиши путь руками")
            return
        tk, _ttk, filedialog, _mb = mods
        result: dict = {}

        def ask():
            try:
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                folder = filedialog.askdirectory(title="Куда сохранить копию")
                root.destroy()
                result["folder"] = folder or ""
            except Exception as e:  # noqa: BLE001
                result["error"] = str(e)

        t = threading.Thread(target=ask)
        t.start()
        t.join(timeout=120)
        if result.get("error"):
            self._err(result["error"])
        else:
            self._json({"ok": True, "folder": result.get("folder", "")})


def web_main(port: int = 8765, host: str = "127.0.0.1", open_browser: bool = True):
    """Поднимает локальный сервер интерфейса и открывает страницу в браузере."""
    UI_STATE["history"] = load_history()

    httpd = None
    last_err = None
    for p in [port, *range(port + 1, port + 12)]:
        try:
            httpd = http.server.ThreadingHTTPServer((host, p), UIHandler)
            port = p
            break
        except OSError as e:
            last_err = e
    if httpd is None:
        eprint(f"Не смог занять порт: {last_err}")
        sys.exit(2)

    url = f"http://localhost:{port}/"
    print()
    print(f"  Интерфейс открыт в браузере: {url}")
    print(f"  Адрес сервера интерфейса:     http://{host}:{port}/")
    print("  Это локальная программа: всё скачивается на этот компьютер.")
    print("  Закрыть: Ctrl+C в этом окне.")
    print()

    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        stop_local_server()
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


def gui_main(web: bool = True, port: int = 8765) -> bool:
    """Графический интерфейс.

    По умолчанию — веб-страница в браузере (вставка текста работает как везде).
    Ключ --tk открывает старое окно на tkinter, если оно кому-то нужно.
    """
    if web:
        web_main(port=port)
        return True

    mods = import_tk()
    if not mods:
        return False
    tk, ttk, filedialog, messagebox = mods
    try:
        app = MirrorApp(tk, ttk, filedialog, messagebox)
    except tk.TclError as e:
        eprint(f"Окно не открылось: {e}")
        return False
    app.run()
    return True


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="site_mirror.py",
        description="Скачивает игровой веб-сайт (по IP:порт из Radmin VPN или по URL) "
                    "в локальную папку. Без аргументов открывает интерфейс: "
                    "страницу в браузере (--web, по умолчанию) или окно на tkinter (--tk).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
примеры:
  python site_mirror.py                                             # интерфейс в браузере
  python site_mirror.py --web --port 9000                           # интерфейс на другом порту
  python site_mirror.py --tk                                        # старое окно tkinter
  python site_mirror.py --cli                                       # мастер в консоли
  python site_mirror.py mirror 26.12.34.56 8080
  python site_mirror.py mirror http://26.12.34.56:8080/game/ --threads 20 -o my_game
  python site_mirror.py serve my_game --port 8000
  python site_mirror.py verify my_game
  python site_mirror.py repo web-dashers
  python site_mirror.py repo osu-web
""")
    p.add_argument("--version", action="version", version=f"site_mirror {VERSION}")
    p.add_argument("--cli", action="store_true", help="не открывать интерфейс, а запустить консольный мастер")
    p.add_argument("--web", action="store_true",
                   help="интерфейс-страница в браузере (так работает по умолчанию)")
    p.add_argument("--tk", action="store_true", help="старый интерфейс на tkinter")
    p.add_argument("--port", type=int, default=8765,
                   help="порт локального интерфейса (по умолчанию 8765)")
    sub = p.add_subparsers(dest="cmd")

    m = sub.add_parser("mirror", help="скачать сайт по IP:порт или URL")
    m.add_argument("host", help="IP / host:port / полный URL (http://26.12.34.56:8080/game/)")
    m.add_argument("port", nargs="?", help="порт (если не указан в адресе)")
    m.add_argument("-o", "--out", help="папка назначения")
    m.add_argument("-t", "--threads", type=int, default=12)
    m.add_argument("-d", "--depth", type=int, default=0, help="0 = без ограничения")
    m.add_argument("--delay", type=float, default=0.0, help="пауза между запросами, сек")
    m.add_argument("--timeout", type=float, default=25)
    m.add_argument("--retries", type=int, default=3, help="повторы при сбоях/429")
    m.add_argument("--max-speculative", type=int, default=400,
                   help="сколько «догадок» о путях разрешено, прежде чем перестать угадывать")
    m.add_argument("--max-files", type=int, default=50_000)
    m.add_argument("--max-mb", type=int, default=300, help="лимит объёма, МБ")
    m.add_argument("--no-assets", action="store_true", help="не искать ассеты внутри JS/JSON")
    m.add_argument("--no-rewrite", action="store_true", help="не переписывать ссылки")
    m.add_argument("--no-probe", action="store_true", help="не пробовать стандартные пути")
    m.add_argument("--external", action="store_true", help="ходить и на другие хосты (осторожно)")
    m.add_argument("--extra", default="", help="доп. URL через запятую или путь к файлу со списком")
    m.add_argument("--insecure", action="store_true", help="игнорировать самоподписанный HTTPS")
    m.add_argument("--zip", action="store_true", help="после копирования упаковать папку в .zip")
    m.add_argument("--no-dir-sweep", action="store_true",
                   help="не обходить каталоги (автоиндекс — лучший способ добрать музыку и уровни)")
    m.add_argument("--no-expand", action="store_true",
                   help="не досматривать соседние файлы в нумерованных наборах")
    m.add_argument("--expand-limit", type=int, default=5000,
                   help="сколько имён досгенерировать максимум")

    s = sub.add_parser("serve", help="раздать скачанную папку локально")
    s.add_argument("folder")
    s.add_argument("--port", type=int, default=8000)

    v = sub.add_parser("verify", help="проверить скачанную копию на битые ссылки")
    v.add_argument("folder")
    v.add_argument("--limit", type=int, default=60, help="сколько битых ссылок показать")

    r = sub.add_parser("repo", help="склонировать репозиторий (для osu-web)")
    r.add_argument("name", help="web-dashers | osu-web")
    r.add_argument("-o", "--out", help="папка назначения")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # Без аргументов — интерфейс. Он не требует ни tkinter, ни дисплея:
    # поднимаем страницу в браузере.
    if not argv:
        try:
            gui_main(web=True)
        except OSError as e:
            eprint(f"Не смог запустить интерфейс: {e}")
            sys.exit(2)
        return

    if argv[0] == "--cli":
        wizard()
        return

    # python site_mirror.py --web --port 9000   (или --tk)
    if argv[0] in ("--web", "--tk"):
        want_web = argv[0] == "--web"
        port = 8765
        rest = argv[1:]
        if "--port" in rest:
            i = rest.index("--port")
            if i + 1 < len(rest) and rest[i + 1].isdigit():
                port = int(rest[i + 1])
        try:
            if want_web:
                gui_main(web=True, port=port)
            elif not gui_main(web=False):
                eprint("tkinter недоступен — открой веб-интерфейс: python site_mirror.py --web")
                sys.exit(2)
        except OSError as e:
            eprint(f"Не смог запустить интерфейс: {e}")
            sys.exit(2)
        return

    # Аргумент без подкоманды тоже считаем адресом: python site_mirror.py 26.1.2.3:8080
    if argv[0] not in ("mirror", "serve", "verify", "repo", "-h", "--help", "--version") \
            and not argv[0].startswith("-"):
        argv = ["mirror", *argv]

    args = build_parser().parse_args(argv)

    if args.cmd == "serve":
        serve(Path(args.folder).resolve(), args.port)
        return

    if args.cmd == "verify":
        sys.exit(verify(Path(args.folder).resolve(), args.limit))

    if args.cmd == "repo":
        clone_repo(args.name, Path(args.out).resolve() if args.out else None)
        return

    if args.cmd == "mirror":
        # «host port» отдельным аргументом — поддержка старого синтаксиса
        host = args.host
        if args.port and ":" not in host.split("://")[-1].split("/")[0]:
            host = f"{host}:{args.port}"
        base_url = parse_target(host, args.port)
        pu = urllib.parse.urlsplit(base_url)
        try:
            url_ok, title, _ = probe(pu.hostname, pu.port, pu.path)
            print(f"✓ {url_ok} {('- ' + title) if title else ''}")
            # если http/https угадан не так — берём рабочий вариант
            if origin_of(url_ok) != origin_of(base_url):
                base_url = url_ok
            # сервер мог перекинуть на другой путь (например / -> /game/)
            if urllib.parse.urlsplit(url_ok).path not in ("", "/") and pu.path in ("", "/"):
                base_url = url_ok
        except Exception as e:                       # noqa: BLE001
            eprint(f"✗ Сервер недоступен: {e}")
            sys.exit(1)

        extra = []
        if args.extra:
            if Path(args.extra).exists():
                extra = [l.strip() for l in Path(args.extra).read_text(encoding="utf-8").splitlines()
                         if l.strip() and not l.startswith("#")]
            else:
                extra = [x.strip() for x in args.extra.split(",") if x.strip()]

        out = Path(args.out) if args.out else APP_DIR / f"mirror_{origin_label(base_url)}"

        cfg = Config(
            base_url=base_url, out_dir=out.resolve(),
            threads=args.threads, depth=args.depth, delay=args.delay,
            timeout=args.timeout, max_files=args.max_files,
            retries=args.retries, max_speculative=args.max_speculative,
            max_bytes=args.max_mb * 1024 * 1024,
            extract_assets=not args.no_assets,
            rewrite=not args.no_rewrite,
            probe_common=not args.no_probe,
            verify_certs=not args.insecure,
            dir_sweep=not args.no_dir_sweep,
            expand=not args.no_expand,
            expand_limit=args.expand_limit,
            follow_external=args.external,
            extra_urls=extra,
        )
        run_mirror(cfg)
        if args.zip:
            print("\nУпаковываю в zip…")
            archive = zip_folder(cfg.out_dir)
            print(f"  архив: {archive}")
        print(f"\nПапка: {cfg.out_dir}")
        print(f'Запуск: python {Path(__file__).name} serve "{cfg.out_dir.name}"')
        return

    build_parser().print_help()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано.")
