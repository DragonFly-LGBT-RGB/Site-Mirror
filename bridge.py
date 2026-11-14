#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bridge.py — локальный мост для https://dragonfly-lgbt-rgb.github.io/Site-Mirror/

Зачем: GitHub Pages — статический сайт, браузер не может скачать произвольный
сайт из-за CORS, а публичные прокси не видят адреса вроде 26.x.x.x (Radmin VPN).
Мост поднимается на твоём компьютере и даёт странице доступ к настоящему
движку site_mirror.py v1.4: качает сайт, переписывает ссылки, упаковывает ZIP.

Запуск:
    python bridge.py                 # http://127.0.0.1:8787
    python bridge.py --port 9000

На сайте выбери режим «Локальный мост» и вставь адрес, который покажет консоль.
Никаких внешних сервисов: файлы не покидают твой компьютер.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / "_bridge_work"
VERSION = "1.4"


# ── подключаем настоящий движок из site_mirror.py ──────────────────────────
def load_engine():
    for candidate in (HERE / "site_mirror.py", HERE / "download" / "site_mirror.py"):
        if not candidate.exists():
            continue
        spec = importlib.util.spec_from_file_location("site_mirror_engine", candidate)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["site_mirror_engine"] = mod
        spec.loader.exec_module(mod)
        return mod
    raise RuntimeError(
        "Не нашёл site_mirror.py. Положи его рядом с bridge.py "
        "(или распакуй site-mirror.zip целиком в одну папку)."
    )


ENGINE = load_engine()

# Небольшая страховка для почти пустых страниц с ассетами: в v1.4
# diagnose_page() считает их «пустыми» и уходит в бесконечный ряд ?embed=true.
# Если в разметке есть реальные <img>/<link>/<script>, считаем страницу рабочей.
_original_diagnose = ENGINE.diagnose_page


def _diagnose_page(body, headers=None):
    result = _original_diagnose(body, headers)
    if result.get("kind") == "empty":
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
        if re.search(r"<(?:img|link|script|iframe|source|video|audio|canvas)\b", text, re.I):
            return {"kind": "ok", "title": result.get("title", ""), "reason": ""}
    return result


ENGINE.diagnose_page = _diagnose_page

LOCK = threading.Lock()
STATE: dict = {
    "state": "idle",            # idle | running | done | error
    "url": "", "base_url": "",
    "log": [], "progress": {}, "notes": [],
    "files": 0, "bytes": 0, "errors": 0,
    "zip": None, "error": "", "started": 0, "finished": 0,
    "mirror": None,
}


def add_log(msg: str) -> None:
    line = str(msg).rstrip()
    with LOCK:
        STATE["log"].append(line)
        if len(STATE["log"]) > 800:
            del STATE["log"][:200]


def snapshot() -> dict:
    with LOCK:
        return {
            "state": STATE["state"], "url": STATE["url"], "base_url": STATE["base_url"],
            "progress": dict(STATE["progress"]), "files": STATE["files"],
            "bytes": STATE["bytes"], "errors": STATE["errors"],
            "log": list(STATE["log"][-400:]), "notes": list(STATE["notes"]),
            "has_zip": bool(STATE["zip"]), "error": STATE["error"],
            "started": STATE["started"], "finished": STATE["finished"],
            "version": VERSION,
        }


def run_job(data: dict) -> None:
    global STATE
    with LOCK:
        STATE.update({
            "state": "running", "url": str(data.get("url") or ""), "base_url": "",
            "log": [], "notes": [], "progress": {}, "files": 0, "bytes": 0,
            "errors": 0, "zip": None, "error": "", "started": time.time(), "finished": 0,
            "mirror": None,
        })

    mirror = None
    try:
        raw = STATE["url"]
        base = ENGINE.parse_target(raw)
        with LOCK:
            STATE["base_url"] = base
        add_log(f"▶ Адрес: {base}")

        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir(parents=True, exist_ok=True)
        out = WORK / "site"

        def as_int(key, default, lo, hi):
            try:
                return max(lo, min(hi, int(data.get(key, default))))
            except (TypeError, ValueError):
                return default

        cfg = ENGINE.Config(
            base_url=base,
            out_dir=out,
            threads=as_int("threads", 12, 1, 32),
            depth=as_int("depth", 0, 0, 20),
            timeout=as_int("timeout", 20, 5, 90),
            retries=as_int("retries", 2, 0, 6),
            max_files=as_int("max_files", 5000, 10, 50000),
            max_bytes=as_int("max_mb", 300, 1, 2048) * 1024 * 1024,
            extract_assets=bool(data.get("assets", True)),
            rewrite=True,
            probe_common=bool(data.get("probe", True)),
            dir_sweep=bool(data.get("dir_sweep", True)),
            expand=bool(data.get("expand", True)),
            follow_external=bool(data.get("external", False)),
            verify_certs=bool(data.get("verify_certs", True)),
            extra_urls=[],
        )

        def on_progress(snap):
            with LOCK:
                STATE["progress"] = dict(snap)
                STATE["errors"] = int(snap.get("errors_total") or snap.get("errors") or 0)

        mirror = ENGINE.Mirror(cfg, log_cb=add_log, progress_cb=on_progress)
        with LOCK:
            STATE["mirror"] = mirror

        mirror.run()
        mirror.rewrite_all()
        mirror.report(write_file=True)
        try:
            mirror.ensure_index()
        except Exception:  # noqa: BLE001
            pass

        files = [p for p in out.rglob("*") if p.is_file()] if out.exists() else []
        total = sum(p.stat().st_size for p in files)
        archive = ENGINE.zip_folder(out)

        with LOCK:
            STATE["files"] = len(files)
            STATE["bytes"] = total
            STATE["zip"] = Path(archive)
            STATE["errors"] = max(STATE["errors"], len(getattr(mirror, "errors", [])))
            STATE["state"] = "done"
            STATE["finished"] = time.time()
        add_log(f"✓ Готово: {len(files)} файлов, {total / 1024:.1f} КБ → {Path(archive).name}")
        add_log("Можно нажимать «Скачать ZIP» на странице.")

    except Exception as e:  # noqa: BLE001
        with LOCK:
            STATE["state"] = "error"
            STATE["error"] = f"{type(e).__name__}: {e}"
            STATE["finished"] = time.time()
        add_log(f"✗ Ошибка: {e}")
        add_log(traceback.format_exc(limit=3))


# ── HTTP + CORS (включая Private Network Access для Chrome) ────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"site-mirror-bridge/{VERSION}"

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Cache-Control", "no-store")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/ping":
            self._json({"ok": True, "version": VERSION, "engine": ENGINE.VERSION,
                        "bridge": "site_mirror bridge"})
            return
        if path == "/api/status":
            self._json({"ok": True, **snapshot()})
            return
        if path == "/api/download":
            with LOCK:
                archive = STATE["zip"]
            if not archive or not Path(archive).exists():
                self._json({"ok": False, "error": "Архив ещё не готов"}, 404)
                return
            data = Path(archive).read_bytes()
            self._send(200, data, "application/zip", {
                "Content-Disposition": 'attachment; filename="Site-Mirror.zip"',
            })
            return
        if path in ("/", "/index.html"):
            html = ("<!doctype html><meta charset='utf-8'><title>site_mirror bridge</title>"
                    "<body style='background:#0b101b;color:#eaf1ff;font:15px system-ui;padding:40px'>"
                    "<h1>Локальный мост запущен ✓</h1>"
                    "<p>Вернись на <a style='color:#7aadff' href='https://dragonfly-lgbt-rgb.github.io/Site-Mirror/'>"
                    "Site-Mirror</a>, выбери режим «Локальный мост» и вставь адрес этого сервера.</p>"
                    "<p><code>http://127.0.0.1:" + str(PORT) + "</code></p></body>")
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._json({"ok": False, "error": "нет такого адреса"}, 404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except Exception:  # noqa: BLE001
            data = {}

        if path == "/api/mirror":
            with LOCK:
                busy = STATE["state"] == "running"
            if busy:
                self._json({"ok": False, "error": "Уже идёт скачивание. Дождись окончания."}, 409)
                return
            if not str(data.get("url") or "").strip():
                self._json({"ok": False, "error": "Не указан адрес сайта"}, 400)
                return
            threading.Thread(target=run_job, args=(data,), daemon=True).start()
            self._json({"ok": True, "state": "running"})
            return

        if path == "/api/stop":
            with LOCK:
                m = STATE["mirror"]
            if m is not None:
                m.stop_flag = True
                add_log("Остановлено пользователем.")
            self._json({"ok": True})
            return

        self._json({"ok": False, "error": "нет такого метода"}, 404)


PORT = 8787


def main():
    global PORT
    ap = argparse.ArgumentParser(description="Локальный мост для Site-Mirror")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    PORT = args.port

    srv = ThreadingHTTPServer((args.host, PORT), Handler)
    url = f"http://{args.host}:{PORT}"
    print()
    print("  site_mirror · локальный мост " + VERSION)
    print("  ─────────────────────────────────────────────────────")
    print(f"  Адрес для вставки на сайте:  {url}")
    print("  Страница:  https://dragonfly-lgbt-rgb.github.io/Site-Mirror/")
    print("  Движок:    site_mirror.py v" + ENGINE.VERSION)
    print("  Файлы не покидают этот компьютер. Закрыть: Ctrl+C")
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
