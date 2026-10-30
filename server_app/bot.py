#!/usr/bin/env python3
"""Telegram-бот HomeCamera — работает на сервере круглосуточно.

Команды:
  /start, /help   — что умеет
  /status         — состояние сервера и архива
  /last           — последняя запись
  /clips [N]      — N последних записей (по умолчанию 3, максимум 10)

Отвечает только chat_id из BOT_ALLOWED (белый список), остальным — молчит.
"""

import logging
import os
import sqlite3
import time

import requests

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {
    int(x) for x in os.environ.get("BOT_ALLOWED", "").replace(" ", "").split(",") if x.strip()
}
DATA = os.environ.get("HC_DATA", "/var/lib/homecamera")
MEDIA = os.path.join(DATA, "media")
DB = os.path.join(DATA, "index.db")
API = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
START = time.time()


def clips(limit=3):
    try:
        c = sqlite3.connect(DB, timeout=10)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM clips WHERE kind='video' ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        c.close()
        return rows
    except Exception as e:
        log.warning("db: %s", e)
        return []


def stats():
    try:
        c = sqlite3.connect(DB, timeout=10)
        n, size = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM clips").fetchone()
        c.close()
    except Exception:
        n, size = 0, 0
    return n, size, int(time.time() - START)


def send(chat_id, text):
    try:
        requests.post(f"{API}/sendMessage",
                      data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                      timeout=30)
    except Exception as e:
        log.warning("sendMessage: %s", e)


def send_video(chat_id, row, caption):
    path = os.path.join(MEDIA, row["rel"])
    if not os.path.exists(path):
        send(chat_id, "Файл уже удалён с сервера.")
        return
    try:
        with open(path, "rb") as fh:
            r = requests.post(
                f"{API}/sendVideo",
                data={"chat_id": chat_id, "caption": caption, "supports_streaming": True},
                files={"video": (os.path.basename(path), fh, "video/mp4")},
                timeout=300,
            )
        if r.status_code != 200:
            log.warning("sendVideo %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("sendVideo: %s", e)


def handle(msg):
    chat_id = msg["chat"]["id"]
    if ALLOWED and chat_id not in ALLOWED:
        log.warning("чужой chat_id %s — игнорирую", chat_id)
        return
    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text else ""

    if cmd in ("/start", "/help"):
        send(chat_id,
             "<b>HomeCamera</b> — сервер на связи.\n\n"
             "/status — состояние\n"
             "/last — последняя запись\n"
             "/clips 5 — последние записи")
    elif cmd == "/status":
        n, size, up = stats()
        h, m = divmod(up // 60, 60)
        send(chat_id, f"Сервер: <b>онлайн</b>\nАптайм: {h}ч {m}м\n"
                      f"Записей: {n}\nАрхив: {size / 1048576:.0f} МБ")
    elif cmd == "/last":
        rows = clips(1)
        if not rows:
            send(chat_id, "Записей пока нет.")
        else:
            r = rows[0]
            send_video(chat_id, r, f"{r['ts'][:16].replace('T', ' ')} · {r['camera'] or 'камера'}")
    elif cmd == "/clips":
        parts = text.split()
        n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 3
        rows = clips(min(max(n, 1), 10))
        if not rows:
            send(chat_id, "Записей пока нет.")
        for r in rows:
            send_video(chat_id, r, f"{r['ts'][:16].replace('T', ' ')} · {r['camera'] or 'камера'}")
    else:
        send(chat_id, "Не понял команду. Напиши /help")


def main():
    log.info("bot started (allowed=%s)", ALLOWED or "все")
    try:
        me = requests.get(f"{API}/getMe", timeout=20).json()
        log.info("bot identity: %s", me.get("result", {}).get("username"))
    except Exception as e:
        log.warning("getMe: %s", e)

    offset = None
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API}/getUpdates", params=params, timeout=70)
            data = r.json()
            if not data.get("ok"):
                log.warning("getUpdates: %s", str(data)[:200])
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        handle(upd["message"])
                    except Exception as e:
                        log.warning("handle: %s", e)
        except Exception as e:
            log.warning("loop: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
