#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Оглавление телеграм-канала» · кнопка оглавления в канале
=========================================
Публикует в канал пост с кнопкой «📖 Оглавление» и закрепляет его.
Запускается из GitHub Actions (workflow button.yml) или вручную локально.

Как это работает (схема 11.9.2026; кейс босса: url-кнопка на Pages
открывалась в браузере, приложение вне Telegram показывало
«Telegram bridge unavailable»):
  web_app-кнопки в постах каналов ЗАПРЕЩЕНЫ («Available in private chats
  only»), поэтому кнопка в канале — обычная url-кнопка. Ведёт она на
  ПРЯМУЮ ССЫЛКУ Mini App (https://t.me/<бот>/<имя> — клиент создаёт её в
  BotFather на шаге /newapp и присылает боту на последнем шаге установки):
  Telegram открывает такую ссылку как Mini App ПРЯМО ВНУТРИ мессенджера,
  с мостом и на весь экран — как в живом демо. Запасной вариант (прямая
  ссылка не передана) — адрес Pages (--url): страница откроется во
  встроенном браузере, а встроенный в приложение баннер выведет читателя
  на правильный вход. Ссылки ?startapp по-прежнему не используются:
  они открывают Main Mini App, который у части клиентов показывает
  «Bot invalid».

  Дополнительно скрипт:
    1. привязывает Pages-адрес к боту как меню-кнопку (setChatMenuButton) —
       удобно в личном чате с ботом, повтор безвреден, ничего не требует;
    2. публикует пост с кнопкой и закрепляет его. Если закреплён наш старый
       кнопочный пост — он удаляется (повторный запуск не плодит дубли).

Аргументы:
    --url   https://<логин>.github.io/<репо>/   адрес сайта оглавления (Pages):
            запасной вариант кнопки и адрес меню-кнопки бота
    --app-link  https://t.me/<бот>/<имя>  прямая ссылка Mini App (шаг /newapp):
            если задана, КНОПКА В КАНАЛЕ ведёт на неё
    --text  надпись на кнопке (по умолчанию «📖 Оглавление»)
    --caption  текст над кнопкой
    --no-pin   не закреплять пост

Секреты: BOT_TOKEN, CHANNEL_USERNAME (те же, что у импорта истории).
"""
import argparse
import json
import os
import re
import sys
import urllib.request

def http_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="адрес сайта оглавления на GitHub Pages")
    ap.add_argument("--app-link", default="",
                    help="прямая ссылка Mini App t.me/<бот>/<имя> — кнопка ведёт на неё")
    ap.add_argument("--text", default="📖 Оглавление")
    ap.add_argument("--caption", default="Все посты канала — в одном каталоге.\nПоиск по темам, датам и словам 👇")
    ap.add_argument("--no-pin", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("BOT_TOKEN", "")
    chat = (os.environ.get("CHANNEL_USERNAME", "") or "").strip().lstrip("@").replace("https://t.me/", "")
    if not token or not chat:
        print("!! нужны BOT_TOKEN и CHANNEL_USERNAME (окружение или GitHub Secrets)")
        return 1
    if not args.url.startswith("https://"):
        print("!! --url должен начинаться с https:// (адрес GitHub Pages)")
        return 1
    api = f"https://api.telegram.org/bot{token}"
    our_marker = args.caption.split("\n")[0]

    # Прямая ссылка Mini App: формат https://t.me/<бот>/<имя> + проверка хозяина
    # (кнопка должна открывать Mini App ЭТОГО бота; урок 8.9.2026 — чужой/примерный
    # t.me/<бот>/<имя> закреплял кнопку на несуществующее приложение).
    app = (args.app_link or "").strip()
    if app and not re.fullmatch(r"https://t\.me/[A-Za-z0-9_]{4,64}/[A-Za-z0-9_]{1,64}/?", app):
        print(f"!! --app-link не похож на прямую ссылку Mini App (нужно https://t.me/<бот>/<имя>): {app!r} — игнорирую")
        app = ""
    if app:
        try:
            me = http_json(api + "/getMe", {})
            uname = ((me.get("result") or {}).get("username") or "").strip().lstrip("@")
            link_bot = app.split("t.me/", 1)[1].split("/", 1)[0]
            if uname and link_bot.lower() != uname.lower():
                print(f"!! --app-link ведёт на чужого бота (t.me/{link_bot}/…, а бот — @{uname}) — игнорирую")
                app = ""
        except Exception as e:
            print(f"·· getMe недоступен ({e}) — проверку хозяина --app-link пропускаю")
    button_url = app or args.url   # Mini App — прямо в Telegram; без него — адрес Pages

    def is_our_post(pm) -> bool:
        """Закреплённый пост — наш кнопочный? (текст-маркер или кнопка с нашей надписью)"""
        if not pm:
            return False
        if (pm.get("text") or "").startswith(our_marker):
            return True
        for row in ((pm.get("reply_markup") or {}).get("inline_keyboard") or []):
            for btn in row:
                if btn.get("text") == args.text:
                    return True
        return False

    if app:
        print("✓ кнопка ведёт на прямую ссылку Mini App (оглавление откроется ПРЯМО В Telegram):",
              button_url)
    else:
        print("✓ кнопка ведёт на сайт оглавления (страница во встроенном браузере;",
              button_url + ") — на весь экран каталог открывает меню-кнопка бота")

    # 1. Меню-кнопка бота (личный чат с ботом): открывает сайт оглавления.
    #    На кнопку в канале НЕ влияет. Повтор безвреден.
    menu = http_json(api + "/setChatMenuButton", {
        "menu_button": {"type": "web_app", "text": args.text,
                        "web_app": {"url": args.url}},
    })
    print("✓ меню-кнопка бота привязана к сайту оглавления" if menu.get("ok")
          else f"!! setChatMenuButton: {menu.get('description')} — на кнопку в канале не влияет")

    # 2. Старый кнопочный пост: если он закреплён — удаляем (не плодим дубли).
    try:
        info = http_json(api + "/getChat", {"chat_id": "@" + chat})
        pm = (info.get("result") or {}).get("pinned_message")
        if is_our_post(pm):
            old = http_json(api + "/deleteMessage",
                            {"chat_id": "@" + chat, "message_id": pm["message_id"]})
            print("✓ старый кнопочный пост удалён" if old.get("ok")
                  else f"!! старый пост не удалён: {old.get('description')} — снимаю закреп")
            if not old.get("ok"):
                http_json(api + "/unpinChatMessage", {"chat_id": "@" + chat})
    except Exception as e:
        print(f"!! проверка старого закрепа: {e} — продолжаю")

    # 3. Пост с обычной url-кнопкой (web_app-кнопки в каналах запрещены;
    #    прямая ссылка t.me/<бот>/<имя> открывает Mini App из url-кнопки)
    resp = http_json(api + "/sendMessage", {
        "chat_id": "@" + chat,
        "text": args.caption,
        "reply_markup": {"inline_keyboard": [[
            {"text": args.text, "url": button_url}
        ]]},
    })
    if not resp.get("ok"):
        print(f"!! Telegram: {resp.get('description')}")
        return 1
    msg_id = resp["result"]["message_id"]
    print(f"✓ пост с кнопкой опубликован: t.me/{chat}/{msg_id}")

    # 4. Закрепление
    if not args.no_pin:
        pin = http_json(api + "/pinChatMessage", {
            "chat_id": "@" + chat, "message_id": msg_id, "disable_notification": True,
        })
        print("✓ закреплён в канале" if pin.get("ok") else f"!! не закрепился: {pin.get('description')} — закрепите вручную")
    print(f"\nГотово: у читателей канала кнопка «{args.text}» открывает оглавление.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
