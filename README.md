# site_mirror · GitHub Pages + Discord Embed

Статический сайт на базе **`site_mirror.py` v1.4**, который:

1. **отдаёт файлы на устройство в `.zip`** — большой кнопкой «Скачать .zip» (готовый архив проекта) и интерактивным сборщиком ZIP прямо в браузере;
2. **красиво выглядит при отправке ссылки в Discord** — полный набор Open Graph / Twitter-метатегов, картинка 1200×630 и blurple-полоска `theme-color`;
3. **готов к публикации на GitHub Pages** — без сборщиков, без npm, без внешних CDN.

> Всё, что нужно для работы, уже лежит в репозитории. `.nojekyll` отключает Jekyll, чтобы файлы вроде `_MIRROR_REPORT.md` и папки `tools/` не пропали при деплое.

---

## 🚀 Быстрый деплой

### 1. Создай репозиторий

Например `site-mirror`. Залей в него **содержимое этой папки** в корень `main`:

```bash
git init
git add .
git commit -m "site_mirror pages v1.4"
git remote add origin https://github.com/USERNAME/site-mirror.git
git push -u origin main
```

### 2. Подставь свой адрес

Discord читает только **абсолютные** URL из исходного HTML, поэтому замени плейсхолдер `USERNAME`:

```bash
python configure.py --user ТВОЙ_ЛОГИН                 # репозиторий по умолчанию: site-mirror
python configure.py --user ТВОЙ_ЛОГИН --repo my-repo  # другое имя репозитория
python configure.py --url https://mydomain.example/ --repo-url https://github.com/USERNAME/site-mirror
python configure.py --check                           # проверить, не остался ли USERNAME
```

Скрипт обновляет `index.html`, `404.html`, `embed-tags.html`, `BUNDLE_README.md`, `oembed.json`, `robots.txt`, `sitemap.xml`, `files.json`, этот README — **пересобирает** `download/site-mirror.zip`, чтобы ссылки внутри архива тоже были правильными, и (если установлен Pillow) перегенерирует `assets/og-image.png` с новым адресом внизу картинки.

Особый случай: если репозиторий называется `ТВОЙ_ЛОГИН.github.io`, адрес страницы будет `https://ТВОЙ_ЛОГИН.github.io/` (без `/site-mirror/`) — `configure.py` учтёт это автоматически.

### 3. Включи GitHub Pages

**Вариант A (простой):** Settings → Pages → **Deploy from a branch** → `main` / `root`.

**Вариант B (уже в репозитории):** `.github/workflows/pages.yml` деплоит через Actions. В Settings → Pages выбери источник **GitHub Actions**. Workflow срабатывает на каждый push в `main`.

Через 1–3 минуты сайт будет на `https://USERNAME.github.io/site-mirror/`.

### 4. Проверь embed

- Отправь ссылку в любой канал Discord — появится карточка с картинкой.
- Discord кэширует превью по URL: после правок добавь `?v=2` к ссылке в сообщении.
- Для превью **самого репозитория** на GitHub: Settings → Social preview → загрузи `assets/og-image.png`.
- Проверить разметку можно через `view-source:` страницы или сервисы вроде opengraph.xyz / Facebook Sharing Debugger.

---

## 📦 Как сайт скачивает файлы в ZIP

### Готовая кнопка (работает всегда)

```html
<a href="download/site-mirror.zip" download="site-mirror.zip">Скачать .zip</a>
```

GitHub Pages отдаёт архив как обычный статический файл — без бэкенда, без авторизации и без JS. Внутри `download/site-mirror.zip`:

| Файл | Назначение |
| --- | --- |
| `site_mirror.py` | сам скрипт v1.4 (177 КБ) |
| `README.md` | быстрый старт, флаги, ограничения, SHA-256 |
| `start.bat` / `start.sh` | запуск веб-интерфейса одним кликом / одной командой |
| `embed-tags.html` | готовые OG-метатеги |
| `requirements.txt` | заглушка «зависимостей нет» для CI |

Отдельно лежит `download/site_mirror.py` — если архив не нужен.

### Сборщик ZIP на странице

Блок **«Сборщик ZIP»** собирает настоящий архив в браузере (собственный ZIP-writer: local headers, central directory, EOCD, CRC32, флаг UTF-8 для имён, метод Store). Три источника:

| Вкладка | Откуда берёт файлы |
| --- | --- |
| 📦 Из проекта | те же файлы с этого сайта (`fetch` same-origin) |
| 💻 С устройства | drag & drop / `<input type=file>` через File API |
| 🔗 По ссылке | список URL; повторяет логику v1.4 для iframe-«рамок» и `?embed=true` |

В каждый архив автоматически кладётся `_MIRROR_REPORT.md` с датой, составом и объёмом. Файлы **не отправляются ни на какой сервер** — всё считается в памяти вкладки.

> ⚠ **CORS.** Браузер не может скачать произвольный `http://26.x.x.x:8080` из-за политики same-origin. Для настоящего зеркала сайта по IP / Radmin VPN используй Python-скрипт: `python site_mirror.py mirror http://26.12.34.56:8080 --zip`. У него нет ограничений CORS, зато есть многопоточность, обход каталогов и переписывание ссылок. Об этом же говорит подсказка прямо на странице.

---

## 🎨 Discord Embed

Все теги находятся в `<head>` `index.html` — **в исходном HTML**, без JavaScript:

```html
<meta property="og:type" content="website">
<meta property="og:title" content="site_mirror — скачать сайт в ZIP">
<meta property="og:description" content="Один ZIP со всеми файлами сайта…">
<meta property="og:url" content="https://USERNAME.github.io/site-mirror/">
<meta property="og:image" content="https://USERNAME.github.io/site-mirror/assets/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="theme-color" content="#5865F2">
<meta name="twitter:card" content="summary_large_image">
```

| Что | Где | Зачем |
| --- | --- | --- |
| `og:title` / `og:description` | `index.html` | заголовок и текст карточки |
| `og:image` (абсолютный!) | `assets/og-image.png`, 1200×630, ~150 КБ | большая картинка |
| `theme-color` `#5865F2` | `index.html` | цвет вертикальной полоски слева |
| `twitter:card=summary_large_image` | `index.html` | fallback для краулеров |
| JSON-LD `SoftwareApplication` | `index.html` | `downloadUrl` на ZIP для поисковиков |
| `oembed.json` | корень | oEmbed-ответ для сервисов, которые его умеют |
| Живой макет сообщения Discord | секция `#embed` | показывает, как выглядит ссылка в чате |

Картинку можно перегенерировать: `python tools/make_og.py` (нужен Pillow, только для генерации, не для сайта). Архив пересобирается: `python tools/build_bundle.py`.

---

## 🆕 Что учтено из обновлённого `site_mirror.py` v1.4

* Разворачивание iframe-«рамок» предпросмотра (Arena, CodePen) и режим `?embed=true`;
* `ensure_index()` — рабочий `index.html`, даже если игра живёт по `/?embed=true`;
* `diagnose_page()` — человечные сообщения про «сайта нет», заглушки nginx и 404;
* панель ошибок в веб-интерфейсе, `/api/embed`, `Referer`, счётчики попыток;
* фильтр MIME-строк и CSS-классов при поиске ассетов;
* CLI-флаги в таблице на странице (`--zip`, `--threads`, `--no-assets`, …);
* честные ограничения: PHP/Laravel, авторизация и WebSocket статикой не копируются.

---

## 🗂 Структура

```
.
├── index.html                 # сайт: hero, скачивание, сборщик ZIP, embed, CLI
├── 404.html                   # страница 404 с теми же OG-тегами
├── embed-tags.html            # шпаргалка по метатегам (можно скачать)
├── BUNDLE_README.md           # README, который кладётся внутрь ZIP
├── configure.py               # заменяет USERNAME и пересобирает ZIP
├── oembed.json                # oEmbed-ответ
├── robots.txt / sitemap.xml   # для индексации
├── files.json                 # список файлов проекта для сборщика
├── .nojekyll                  # отключает Jekyll
├── assets/
│   ├── og-image.png           # 1200×630 для Discord
│   ├── icon-512.png           # apple-touch-icon / social preview
│   └── favicon.svg
├── download/
│   ├── site-mirror.zip        # ⬅ основной скачиваемый архив
│   └── site_mirror.py         # скрипт отдельным файлом
├── tools/
│   ├── make_og.py             # генерация OG-картинки (Pillow)
│   └── build_bundle.py        # сборка ZIP
└── .github/workflows/pages.yml
```

---

## 🧪 Локальный предпросмотр

```bash
cd site-mirror-pages
python -m http.server 8000
# http://localhost:8000/
```

Проверь: кнопка «Скачать .zip» сохраняет архив; сборщик ZIP во вкладке «С устройства» работает даже без сети; `view-source:` показывает абсолютные `og:image`; `og-image.png` отдаётся с кодом 200.

## ⚠ Этикет

Используй инструмент только для своих или дружеских серверов и с согласия владельца. Скрипт не обходит авторизацию и по умолчанию не ходит на чужие хосты (`--external` — осознанный риск).
