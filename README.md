# Site-Mirror · GitHub Pages + Discord Embed

**Адрес сайта:** https://dragonfly-lgbt-rgb.github.io/Site-Mirror/  
**Репозиторий:** https://github.com/dragonfly-lgbt-rgb/Site-Mirror

Статический сайт на базе **`site_mirror.py` v1.4**, который:

1. **сам скачивает указанный сайт и отдаёт готовый `.zip` на устройство** — вставь адрес, обходчик соберёт HTML/CSS/JS/картинки/музыку, достанет ассеты из JS/JSON, перепишет ссылки, развернёт iframe-«рамки» и упакует всё в архив;
2. **красиво выглядит при отправке ссылки в Discord** — полный набор Open Graph / Twitter-метатегов, картинка 1200×630 и blurple-полоска `theme-color`;
3. **готов к публикации на GitHub Pages** — без сборщиков, без npm, без внешних CDN.

> `.nojekyll` отключает Jekyll, чтобы служебные файлы и папки не пропали при деплое.

---

## 🚀 Быстрый деплой

### 1. Создай репозиторий

Залей **содержимое этой папки** в корень `main` репозитория `Site-Mirror`:

```bash
git init
git add .
git commit -m "Site-Mirror v1.4"
git remote add origin https://github.com/dragonfly-lgbt-rgb/Site-Mirror.git
git push -u origin main
```

### 2. Подставь свой адрес

Discord читает только **абсолютные** URL из исходного HTML. Для этого проекта уже известен адрес:

```bash
python configure.py --user dragonfly-lgbt-rgb --repo Site-Mirror
python configure.py --check
```

`configure.py` обновляет `index.html`, `404.html`, `embed-tags.html`, `BUNDLE_README.md`, `oembed.json`, `robots.txt`, `sitemap.xml`, `files.json`, этот README — **пересобирает** `download/site-mirror.zip` и (если установлен Pillow) перегенерирует `assets/og-image.png` с новым адресом внизу картинки.

Другие варианты:

```bash
python configure.py --user LOGIN --repo my-repo
python configure.py --url https://mydomain.example/ --repo-url https://github.com/dragonfly-lgbt-rgb/Site-Mirror
```

### 3. Включи GitHub Pages

**Вариант A:** Settings → Pages → **Deploy from a branch** → `main` / `root`.  
**Вариант B:** `.github/workflows/pages.yml` уже деплоит через Actions — выбери источник **GitHub Actions**.

Через 1–3 минуты сайт будет на https://dragonfly-lgbt-rgb.github.io/Site-Mirror/

### 4. Проверь embed

Отправь ссылку в Discord — появится карточка. Discord кэширует превью по URL: после правок добавь `?v=2`.  
Для превью **самого репозитория**: Settings → Social preview → загрузи `assets/og-image.png`.

---

## 🌐 Как сайт скачивает сайт → ZIP

Главная секция `#mirror` работает в двух режимах.

### ◎ Публичный прокси (без установки)

Браузер с публичной страницы не может читать чужой сайт напрямую из-за CORS, поэтому движок на странице использует **цепочку публичных CORS-прокси** с автопереключением (`allorigins → codetabs → corsproxy.io → cors.lol`) и прямой `fetch` в начале.

Что делает браузерный обходчик:

| Шаг | Что происходит |
| --- | --- |
| Нормализация | `example.com`, `host:port`, `http://…` → пробует `https`, потом `http` |
| Обход |HTML-страницы, `<link>/<script>/<img>/srcset/poster/data-*`, CSS `url()` и `@import`, строки-ассеты в JS/JSON |
| Рамки | `<iframe>` у Arena/CodePen → внутренний адрес, при необходимости `?embed=true` (логика v1.4) |
| Диагностика | «сайта нет», заглушка nginx, 404 — понятным сообщением в лог |
| Пути | абсолютные URL → относительные, `index.html` гарантирован даже для `/?embed=true` |
| Лимиты | глубина, макс. файлов, макс. МБ, 4 потока, кнопка «Стоп» сохраняет уже скачанное |
| Архив | настоящий ZIP (CRC32, UTF-8 имена) + `_MIRROR_REPORT.md` → сразу на устройство |

**Ограничение:** публичные прокси не видят приватные сети (`26.x.x.x` Radmin VPN, `localhost`, `192.168.*`) и иногда ограничены по частоте. Для них — второй режим.

### ▣ Локальный мост `bridge.py` (полное зеркало)

```bash
# распакуй site-mirror.zip в одну папку (там site_mirror.py + bridge.py)
python bridge.py                 # http://127.0.0.1:8787
```

На сайте выбери «Локальный мост», вставь адрес из консоли, нажми «Проверить» → «Скачать сайт в ZIP».

Мост — это тонкий HTTP-сервер с CORS и `Access-Control-Allow-Private-Network: true` (для Chrome), который вызывает **настоящий движок `site_mirror.py` v1.4**: 12 потоков, ретраи, автоиндексы каталогов, догадки об ассетах, `ensure_index()`, переписывание ссылок, человечные ошибки, `_MIRROR_REPORT.md`. Затем `zip_folder()` упаковывает папку, а страница забирает ZIP через `/api/download`.

API моста: `GET /api/ping`, `POST /api/mirror`, `GET /api/status`, `GET /api/download`, `POST /api/stop`.  
В мосту есть небольшая страховка: почти пустые страницы с `<img>/<link>/<script>` не уходят в бесконечный ряд `?embed=true` (особенность `diagnose_page()` v1.4).  
**Файлы не покидают твой компьютер** — ни на GitHub Pages, ни на прокси.

> 💡 Если браузер всё же блокирует запрос к `localhost` с HTTPS-страницы (Private Network Access), открой сайт по `http://` в локальной сети или используй режим «Публичный прокси» для публичных адресов. Мост остаётся самым надёжным способом для Radmin VPN.

### Готовый архив проекта

Кнопка «Архив проекта .zip» отдаёт `download/site-mirror.zip` (статический файл, атрибут `download`, без JS и бэкенда):

| Файл | Назначение |
| --- | --- |
| `site_mirror.py` | скрипт v1.4 (177 КБ) |
| `bridge.py` | локальный мост для GitHub Pages |
| `README.md` | быстрый старт, флаги, ограничения, SHA-256 |
| `start.bat` / `start.sh` | запуск веб-интерфейса |
| `embed-tags.html` | готовые OG-метатеги |
| `requirements.txt` | заглушка «зависимостей нет» |

Отдельно лежит `download/site_mirror.py`.

### Сборщик ZIP на странице

Блок `#archiver` собирает архив из файлов проекта, файлов с устройства (drag & drop) или по списку URL. Собственный ZIP-writer: local headers, central directory, EOCD, CRC32, UTF-8, метод Store. Внутрь кладётся `_MIRROR_REPORT.md`. Всё считается в браузере, ничего не загружается на сервер.

---

## 🎨 Discord Embed

Все теги — в исходном `<head>` `index.html`, без JavaScript:

```html
<meta property="og:type" content="website">
<meta property="og:title" content="site_mirror — скачать сайт в ZIP">
<meta property="og:description" content="Вставь адрес — сайт сам обойдёт страницы…">
<meta property="og:url" content="https://dragonfly-lgbt-rgb.github.io/Site-Mirror/">
<meta property="og:image" content="https://dragonfly-lgbt-rgb.github.io/Site-Mirror/assets/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="theme-color" content="#5865F2">
<meta name="twitter:card" content="summary_large_image">
```

| Что | Где | Зачем |
| --- | --- | --- |
| `og:title` / `og:description` | `index.html` | заголовок и текст карточки |
| `og:image` (абсолютный!) | `assets/og-image.png`, 1200×630, ~150 КБ | большая картинка |
| `theme-color` `#5865F2` | `index.html` | цвет полоски слева |
| `twitter:card=summary_large_image` | `index.html` | fallback для краулеров |
| JSON-LD `SoftwareApplication` | `index.html` | `downloadUrl` на ZIP |
| `oembed.json` | корень | oEmbed-ответ |
| Живой макет сообщения Discord | секция `#embed` | как выглядит ссылка в чате |

Картинка перегенерируется: `python tools/make_og.py` (Pillow). Архив: `python tools/build_bundle.py`.

---

## 🆕 Что учтено из `site_mirror.py` v1.4

* iframe-«рамки» предпросмотра (Arena, CodePen) и `?embed=true`;
* `ensure_index()` — рабочий `index.html`, даже если игра живёт по `/?embed=true`;
* `diagnose_page()` — «сайта нет», заглушка nginx, пустая страница, 404;
* панель ошибок в веб-интерфейсе, `/api/embed`, `Referer`, счётчики попыток;
* фильтр MIME-строк и CSS-классов при поиске ассетов;
* CLI-флаги в таблице на странице (`--zip`, `--threads`, `--no-assets`, …);
* ограничения: PHP/Laravel, авторизация и WebSocket статикой не копируются.

---

## 🗂 Структура

```
.
├── index.html                 # hero, зеркало сайта → ZIP, архив, сборщик, embed, CLI
├── bridge.py                  # локальный мост (движок v1.4 без CORS)
├── 404.html                   # 404 с теми же OG-тегами
├── embed-tags.html            # шпаргалка по метатегам
├── BUNDLE_README.md           # README внутри ZIP
├── configure.py               # заменяет USERNAME, пересобирает ZIP и OG
├── oembed.json / files.json   # oEmbed и список файлов
├── robots.txt / sitemap.xml
├── .nojekyll
├── assets/
│   ├── og-image.png           # 1200×630 для Discord
│   ├── icon-512.png
│   └── favicon.svg
├── download/
│   ├── site-mirror.zip        # ⬅ архив проекта (вкл. bridge.py)
│   └── site_mirror.py         # скрипт отдельным файлом
├── tools/
│   ├── make_og.py             # генерация OG-картинки (Pillow)
│   └── build_bundle.py        # сборка ZIP
└── .github/workflows/pages.yml
```

---

## 🧪 Локальный предпросмотр и проверка

```bash
python -m http.server 8000          # http://localhost:8000/
python bridge.py                    # второй терминал, http://127.0.0.1:8787
```

На странице вставь `http://127.0.0.1:8000/` (или любую тестовую папку) и проверь оба режима. Готовый ZIP должен открываться, содержать `index.html` и `_MIRROR_REPORT.md`.

## ⚠ Этикет

Используй инструмент только для своих или дружеских серверов и с согласия владельца. Скрипт не обходит авторизацию и по умолчанию не ходит на чужие хосты (`--external` — осознанный риск). Публичные CORS-прокси — сторонние сервисы: не передавай через них приватные данные.
