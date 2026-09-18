#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Оглавление телеграм-канала» · автоимпорт из публичного веб-превью
==================================================================
Собирает оглавление из ВАШИХ постов: читает публичную веб-версию канала
(https://t.me/s/<канал>) и складывает реальные посты в docs/posts.json.
Приватным каналам это недоступно — для них путь через экспорт Telegram
Desktop (конвейер «Импорт истории» + import/result.json).

Использование:
    python pipeline/import_public.py --channel my_channel --limit 300

Флаги:
    --limit     сколько последних постов собрать (по умолчанию 300)
    --keep      сколько постов канала хранить в оглавлении (по умолчанию 900;
                чужие записи — RSS и т.п. — лимит не касается)
    --replace   не сливать с текущим оглавлением, а заменить его постами канала
                (RSS-записи при этом уходят — остаются только ваши посты)

Посты без текста подписываются «📷 Фотопост» и т.п., сервисные сообщения
и удалённые посты пропускаются.

Повторный запуск безопасен: слияние БЕЗ ПОТЕРЬ (урок 11.9.2026) — старые посты
канала сохраняются, свежая версия поста из веба важнее старой. Раньше совпадающий
id удалялся из свежей выборки: новый пост жил в оглавлении один прогон и исчезал,
а частичная страница (кэш/сбой) заменяла собой всю историю каталога. Диагностика
каждого прогона пишется в docs/import_meta.json (её же читает сообщение коммита).

Удалённые из канала посты тоже уходят из оглавления (задача 145): прогон
фиксирует зону обхода [min_seen..max_seen] — самый низкий id, который веб-превью
реально показало. «Пропавший» пост из этой зоны удаляется только после прямой
переверификации (t.me/<канал>/<id>?embed=1) и только если кандидатов немного:
предохранитель (не больше 40% зоны обхода за прогон) не даёт частичному рендеру
повторить урок 11.9.2026 — массовую чистку за один прогон сделать невозможно.
"""
import argparse
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
PHOTO_TITLE = "📷 Фотопост"
VIDEO_TITLE = "🎬 Видеопост"
AUDIO_TITLE = "🎧 Аудио"
FILE_TITLE = "📎 Файл"
POLL_TITLE = "📊 Опрос"

# Порядок важен: от частного к общему (опрос — это не просто «медиа»)
# Кружочки (видеосообщения) несут класс roundvideo — это тоже видео-контент:
# в оглавлении они попадают в ту же карточку «Видео» (нормализация ниже).
MEDIA_KINDS = [
    ("poll", POLL_TITLE), ("photo", PHOTO_TITLE), ("roundvideo", VIDEO_TITLE),
    ("video", VIDEO_TITLE),
    ("audio", AUDIO_TITLE), ("sticker", "🧩 Стикер"), ("document", FILE_TITLE),
]

# Урок 11.9.2026: края Telegram отдают машинам датацентров (GitHub Actions)
# устаревший рендер страницы — заголовки no-store не спасают. Лечим байт-бастером
# в URL и «живым» браузерным UA с запретом кэша на нашей стороне.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Предохранитель удаления (задача 145): за один прогон нельзя снять больше
# этой доли постов зоны обхода (и больше PRUNE_MAX_ABS штук) — иначе это
# похоже на частичный рендер, а не на честную чистку.
PRUNE_MAX_SHARE = 0.4
PRUNE_MAX_ABS = 60


def log(m):
    print(m, flush=True)


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def strip_hashtags(s):
    return clean(re.sub(r"#[\wа-яё]+", "", s or "", flags=re.I))


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def post_exists(chat, pid):
    """Переверификация «пропавшего» поста — задача 145. Прямая embed-страница
    поста t.me/<канал>/<id>: у живого поста там свой виджет с data-post.
    Любая сетевая беда трактуется как «пост жив» — удаляем только по факту."""
    try:
        html = fetch(f"https://t.me/{chat}/{pid}?embed=1&mode=tme")
    except Exception:
        return True
    return f'data-post="{chat}/{pid}"' in html


def _txt_from_html(raw_html):
    """HTML поста → читаемый текст (<br> → перенос, теги долой, сущности обратно)."""
    s = re.sub(r"<br ?/?>", "\n", raw_html or "")
    s = re.sub(r"<[^>]+>", "", s)
    return html_mod.unescape(s).strip()


def parse_page(page_html, chat):
    """Страница t.me/s/ → (записи, имя канала). Сервисные/удалённые пропускаем."""
    posts = []
    name_m = re.search(r'<meta property="og:title" content="([^"]*)"', page_html)
    ch_name = html_mod.unescape(name_m.group(1)).strip() if name_m else ""
    for block in re.split(r'<div class="tgme_widget_message_wrap', page_html)[1:]:
        mid = re.search(r'data-post="' + re.escape(chat) + r'/(\d+)"', block)
        if not mid:
            continue
        pid = int(mid.group(1))
        # Урок 12.9.2026: «message_media_not_supported» — скрытый fallback-блок,
        # который Telegram кладёт ВНУТРЬ обычных постов (у видео — всегда, у свежих
        # рендеров — в каждом сообщении). Пропуск по нему молча выбрасывал ВСЕ
        # видео-посты, а на свежем рендере обнулял импорт целиком (живая проверка
        # t.me/s/durov: 0 постов из 20). Настоящие пустышки и так отсеивает
        # проверка «нет текста и нет медиа» ниже — двойной фильтр был вреден.
        if "service_message" in block:
            continue   # сервисные сообщения (закрепы, вступления и т.п.)
        t = re.search(r'<time datetime="([^"]+)"', block)
        when = None
        if t:
            try:
                when = datetime.fromisoformat(t.group(1)).astimezone(MSK)
            except ValueError:
                when = None
        raw = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        text = _txt_from_html(raw.group(1)) if raw else ""
        if "в одном каталоге" in text or "in one catalog" in text:
            continue   # сам закреплённый пост с кнопкой оглавления — не контент
        if not text and not any(k in block for k, _ in MEDIA_KINDS):
            continue   # удалённый пост/пустышка
        kind = "text"
        for key, label in MEDIA_KINDS:
            if ("tgme_widget_message_" + key) in block:
                kind = key
                break
        if kind == "roundvideo":
            kind = "video"   # кружочки — тот же тип «Видео» в оглавлении
        hashtags = re.findall(r"#([\wа-яё]+)", text, flags=re.I)
        lines = [x for x in (text.split("\n") if text else []) if x.strip()]
        if lines:
            title = clean(lines[0])
            if title.startswith("http") and len(lines) > 1:
                # первая строка — голая ссылка: заголовок берем из содержательной строки
                title = clean(lines[1])
                preview = strip_hashtags(" ".join(lines[1:]))
            elif title.startswith("#") and len(lines) > 1:
                preview = strip_hashtags(" ".join(lines[1:]))
            else:
                title = strip_hashtags(title) or ("#" + hashtags[0] if hashtags else title)
                preview = strip_hashtags(" ".join(lines[1:]))
            if len(title) > 110:
                title = title[:109].rstrip() + "…"
            preview = strip_hashtags(preview)
        else:
            title = dict(MEDIA_KINDS).get(kind, PHOTO_TITLE)
            preview = ""
        posts.append({
            "id": pid,
            "date": when.strftime("%Y-%m-%d") if when else "",
            "time": when.strftime("%H:%M") if when else "",
            "title": title,
            "preview": preview[:180],
            "tags": ["#" + h for h in hashtags[:4]],
            "kind": kind,
            "url": f"https://t.me/{chat}/{pid}",
            "src": "",
        })
    return posts, ch_name


def collect_posts(chat, limit):
    """Веб-превью → (посты, имя канала, min_seen). Ходим назад по ?before=,
    пока не наберём limit постов. Пустую страницу перепроверяем до 2 раз: серия
    служебных постов (закрепы) или каприз края CDN не должны обрывать сбор
    (урок 11.9.2026). min_seen — самый низкий id, который прогон реально видел:
    это зона обхода для проверенного удаления (задача 145)."""
    posts, ch_name = [], ""
    before = None
    nonce = int(time.time() * 1000)
    empty_retries = 0
    for _ in range(40):   # ~40 страниц × 20 постов — с запасом выше лимита
        qs = (f"before={before}&" if before else "") + f"_={nonce}"
        nonce += 1
        try:
            page = fetch(f"https://t.me/s/{chat}?{qs}")
        except Exception as e:
            log(f"!! не удалось открыть https://t.me/s/{chat} ({e})")
            return None, ch_name, 0
        batch, ch_name = parse_page(page, chat)
        if not batch:
            empty_retries += 1
            if empty_retries > 2:
                break
            time.sleep(0.8)
            continue
        empty_retries = 0
        posts += batch
        if len(posts) >= limit:
            break
        nxt = min(p["id"] for p in batch)
        if before == nxt - 1 or nxt <= 1:
            break
        before = nxt - 1
        time.sleep(0.4)
    min_seen = min((p["id"] for p in posts), default=0)
    return posts, ch_name, min_seen


def merge_posts(old_posts, fetched_posts, chat, keep, covered_min=0, verify=None):
    """Слияние БЕЗ ПОТЕРЬ: посты канала = старые ∪ свежие (свежая версия важнее),
    чужие записи (RSS и т.п.) сохраняются всегда. Возвращаем (merged, prev_max,
    added, max_seen, removed). Урок 11.9.2026: раньше совпадающий id УДАЛЯЛСЯ из
    свежей выборки — новый пост исчезал при следующем прогоне; а частичная
    выборка заменяла собой всю историю. Теперь только дополнение и обновление
    по id.

    Исключение — ПРОВЕРЕННОЕ удаление (задача 145): старый пост канала, который
    не пришёл в зоне обхода (id >= covered_min), снимается с оглавления, но
    только если (а) кандидатов не больше предохранителя и (б) прямая проверка
    поста подтвердила, что его больше нет. verify — инъекция для тестов,
    по умолчанию post_exists."""
    own_prefix = f"https://t.me/{chat}/"

    def _old_id(p):
        u = str(p.get("url", ""))
        if u.startswith(own_prefix):
            m = re.search(r"/(\d+)$", u)
            if m:
                return int(m.group(1))
        return None

    others = [p for p in old_posts if _old_id(p) is None]
    prev_max = 0
    old_ids = set()
    old_own = {}
    by_id = {}
    for p in old_posts:
        oid = _old_id(p)
        if oid is not None:
            by_id[oid] = p
            old_ids.add(oid)
            old_own[oid] = p
            prev_max = max(prev_max, oid)
    for p in fetched_posts:
        by_id[p["id"]] = p   # свежая версия из веба важнее
    added = sum(1 for p in fetched_posts if p["id"] not in old_ids)
    max_seen = max((p["id"] for p in fetched_posts), default=0)
    fetched_ids = {p["id"] for p in fetched_posts}

    # ── проверенное удаление (задача 145) ──────────────────────────────────
    removed = 0
    if covered_min > 0 and old_own:
        if verify is None:
            verify = lambda pid: post_exists(chat, pid)   # noqa: E731
        covered = [oid for oid in old_own if oid >= covered_min]
        candidates = [oid for oid in covered if oid not in fetched_ids]
        max_drop = max(3, int(PRUNE_MAX_SHARE * len(covered)))
        if PRUNE_MAX_ABS:
            max_drop = min(max_drop, PRUNE_MAX_ABS)
        if len(candidates) > max_drop:
            log(f"Предохранитель удаления: пропавших {len(candidates)} при лимите "
                f"{max_drop} — похоже на частичный рендер, ничего не удаляю")
        else:
            for oid in candidates:
                try:
                    alive = verify(oid)
                except Exception:
                    alive = True   # проверка не удалась — пост сохраняем
                if alive:
                    continue
                by_id.pop(oid, None)
                removed += 1
                log(f"Удалён из оглавления пропавший пост {chat}/{oid} "
                    "(в канале его больше нет — перепроверено напрямую)")
            if removed:
                log(f"Итог чистки: убрано удалённых постов — {removed}")

    def _key(p):
        return (p.get("date", ""), p.get("time", ""), str(p.get("id", "")))

    channel_posts = sorted(by_id.values(), key=_key, reverse=True)
    if keep and len(channel_posts) > keep:
        log(f"Лимит хранения: оставили новейшие {keep} постов канала "
            f"(старых убрали: {len(channel_posts) - keep})")
        channel_posts = channel_posts[:keep]
    merged = others + channel_posts
    merged.sort(key=_key, reverse=True)
    return merged, prev_max, added, max_seen, removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True, help="юзернейм канала без @")
    ap.add_argument("--posts", default="docs/posts.json")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--keep", type=int, default=900,
                    help="сколько постов канала хранить в оглавлении")
    ap.add_argument("--replace", action="store_true",
                    help="заменить оглавление постами канала (RSS-записи уходят)")
    args = ap.parse_args()

    chat = args.channel.strip().lstrip("@").replace("https://t.me/", "").rstrip("/")

    doc = {"version": 1, "updated_at": "", "channel": {"name": chat,
                                                       "url": f"https://t.me/{chat}"},
           "posts": []}
    if os.path.exists(args.posts) and not args.replace:
        try:
            with open(args.posts, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            pass
    old = doc.get("posts") or []

    posts, ch_name, min_seen = collect_posts(chat, args.limit)
    if posts is None:
        return 1   # сеть недоступна — шаг упадёт, расписание придёт снова
    if not posts:
        log(f"!! https://t.me/s/{chat} не содержит постов: канал приватный, пустой "
            "или свежесозданный. Публичный путь недоступен — используйте экспорт "
            "Telegram Desktop (гайд, раздел «Импорт истории»).")
        return 1
    posts = posts[:args.limit]
    log(f"Собрано постов из веб-превью @{chat}: {len(posts)}")

    merged, prev_max, added, max_seen, removed = merge_posts(
        old, posts, chat, args.keep, covered_min=min_seen)
    doc["posts"] = merged
    doc["updated_at"] = datetime.now(MSK).isoformat(timespec="seconds")
    if ch_name:
        doc.setdefault("channel", {})["name"] = ch_name
    doc.setdefault("channel", {})["url"] = f"https://t.me/{chat}"
    os.makedirs(os.path.dirname(args.posts) or ".", exist_ok=True)
    with open(args.posts, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    # Диагностика прогона: видно в коммите (сообщение) и в файле — удобно и
    # владельцу, и поддержке: сразу видно, что веб-превью отдало и что добавилось.
    stale = bool(max_seen and prev_max and max_seen <= prev_max)
    commit_message = (f"импорт: постов в оглавлении {len(merged)}, "
                      f"новых +{added} (старший id {max_seen})")
    if removed:
        commit_message += f", удалённых убрано {removed}"
    meta = {
        "ts": doc["updated_at"],
        "channel": chat,
        "fetched": len(posts),
        "max_seen": max_seen,
        "prev_max": prev_max,
        "added": added,
        "removed": removed,
        "total": len(merged),
        "commit_message": commit_message,
    }
    with open(os.path.join(os.path.dirname(args.posts) or ".", "import_meta.json"),
              "w", encoding="utf-8") as mf:
        json.dump(meta, mf, ensure_ascii=False, indent=2)
    if stale:
        log("Веб-превью не показало постов новее уже импортированных "
            "(кэш/задержка Telegram) — следующий прогон проверит снова.")
    log(f"Готово: в оглавлении {len(merged)} постов (канал «{doc['channel'].get('name')}») — "
        "сайт обновится после деплоя Pages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
