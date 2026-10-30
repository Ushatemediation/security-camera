#!/usr/bin/env python3
"""HomeCamera Server — приём клипов с камеры и веб-галерея для просмотра.

API (для приложения на ПК с камерой):
  POST /api/upload           multipart: file, kind=video|photo, camera, note, ts
                             заголовок  X-Token: <HC_TOKEN>
  GET  /api/clips?limit=100  список (X-Token)
  POST /api/delete/<id>      удалить клип (X-Token)

Веб (для человека):
  GET /                      галерея (Basic auth)
  GET /media/<путь>          сам файл (Basic auth)
  GET /health                проверка живости
"""

import datetime
import os
import shutil
import sqlite3
import threading
import time
import uuid
from functools import wraps

from flask import (Flask, Response, abort, jsonify, render_template_string,
                   request, send_from_directory)

DATA = os.environ.get("HC_DATA", "/var/lib/homecamera")
MEDIA = os.path.join(DATA, "media")
DB = os.path.join(DATA, "index.db")
TOKEN = os.environ.get("HC_TOKEN", "")
USER = os.environ.get("HC_USER", "admin")
PASS = os.environ.get("HC_PASS", "")

MAX_MB = int(os.environ.get("HC_MAX_MB", "300"))
RETENTION_DAYS = int(os.environ.get("HC_RETENTION_DAYS", "30"))
MAX_GB = float(os.environ.get("HC_MAX_GB", "22"))
CLEAN_EVERY = 3600

ALLOWED = {".mp4": "video", ".mov": "video", ".jpg": "photo", ".jpeg": "photo", ".png": "photo"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HomeCamera — записи</title>
<style>
 body{background:#101012;color:#ececec;font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:16px}
 h1{font-size:18px;margin:0 0 4px}
 .sub{color:#8a8a8a;font-size:13px;margin-bottom:20px}
 .day{margin:26px 0 10px;font-size:14px;color:#7fb6ff;font-weight:600}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
 .card{background:#1a1a1d;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px #0006}
 video,img{width:100%;display:block;background:#000;max-height:320px;object-fit:contain}
 .meta{padding:8px 10px;font-size:12px;color:#a0a0a0;display:flex;justify-content:space-between;gap:8px}
 .empty{color:#777;padding:60px 0;text-align:center}
 a{color:#7fb6ff}
 form{display:inline}
 button{background:none;border:0;color:#e06c6c;cursor:pointer;font-size:12px;padding:0}
</style></head><body>
<h1>HomeCamera · записи</h1>
<div class="sub">{{ total }} файлов · {{ size_gb }} ГБ · хранение {{ days }} дней</div>
{% if groups %}
  {% for day, items in groups %}
    <div class="day">{{ day }}</div>
    <div class="grid">
    {% for c in items %}
      <div class="card">
        {% if c.kind == 'photo' %}
          <img src="/media/{{ c.rel }}" loading="lazy" alt="">
        {% else %}
          <video src="/media/{{ c.rel }}" controls preload="metadata"></video>
        {% endif %}
        <div class="meta">
          <span>{{ c.ts[11:16] }} · {{ c.camera or 'камера' }}{% if c.note %} · {{ c.note }}{% endif %}</span>
          <span>{{ (c.size / 1048576) | round(1) }} МБ</span>
        </div>
      </div>
    {% endfor %}
    </div>
  {% endfor %}
{% else %}
  <div class="empty">Пока пусто — записи появятся, когда камера пришлёт первое видео.</div>
{% endif %}
</body></html>"""


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(MEDIA, exist_ok=True)
    c = conn()
    c.execute("""CREATE TABLE IF NOT EXISTS clips(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rel TEXT UNIQUE, kind TEXT, camera TEXT, note TEXT,
        ts TEXT, size INTEGER, created TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_clips_ts ON clips(ts)")
    c.commit()
    c.close()


def cleanup():
    """Удаляет старые записи и следит за лимитом диска."""
    try:
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=RETENTION_DAYS)).isoformat()
        c = conn()
        for row in c.execute("SELECT id, rel FROM clips WHERE ts < ?", (cutoff,)).fetchall():
            try:
                os.remove(os.path.join(MEDIA, row["rel"]))
            except OSError:
                pass
            c.execute("DELETE FROM clips WHERE id = ?", (row["id"],))
        total = c.execute("SELECT COALESCE(SUM(size),0) FROM clips").fetchone()[0]
        while total > MAX_GB * 1024 ** 3:
            row = c.execute("SELECT id, rel, size FROM clips ORDER BY ts ASC LIMIT 1").fetchone()
            if row is None:
                break
            try:
                os.remove(os.path.join(MEDIA, row["rel"]))
            except OSError:
                pass
            c.execute("DELETE FROM clips WHERE id = ?", (row["id"],))
            total -= row["size"]
        c.commit()
        c.close()
    except Exception:
        pass


def cleaner_loop():
    while True:
        time.sleep(CLEAN_EVERY)
        cleanup()


def check_auth(u, p):
    import hmac
    return hmac.compare_digest(u, USER) and hmac.compare_digest(p, PASS)


def requires_auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        auth = request.authorization
        if not auth or not check_auth(auth.username or "", auth.password or ""):
            return Response("Требуется вход", 401,
                            {"WWW-Authenticate": 'Basic realm="HomeCamera"'})
        return f(*a, **kw)
    return wrapper


def token_ok():
    import hmac
    given = request.headers.get("X-Token", "")
    return bool(TOKEN) and hmac.compare_digest(given, TOKEN)


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.post("/api/upload")
def upload():
    if not token_ok():
        abort(401)
    f = request.files.get("file")
    if f is None:
        abort(400, "нет файла")
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in ALLOWED:
        abort(415, "неподдерживаемый формат")
    kind = request.form.get("kind") or ALLOWED[ext]
    camera = (request.form.get("camera") or "")[:64]
    note = (request.form.get("note") or "")[:200]
    ts = request.form.get("ts") or datetime.datetime.utcnow().isoformat(timespec="seconds")
    day = ts[:10] if len(ts) >= 10 else datetime.date.today().isoformat()
    rel = f"{day}/{uuid.uuid4().hex}{ext}"
    path = os.path.join(MEDIA, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f.save(path)
    size = os.path.getsize(path)
    c = conn()
    cur = c.execute(
        "INSERT INTO clips(rel,kind,camera,note,ts,size,created) VALUES(?,?,?,?,?,?,?)",
        (rel, kind, camera, note, ts, size,
         datetime.datetime.utcnow().isoformat(timespec="seconds")))
    c.commit()
    cid = cur.lastrowid
    c.close()
    return jsonify(ok=True, id=cid, size=size)


@app.get("/api/clips")
def api_clips():
    if not token_ok():
        abort(401)
    limit = min(int(request.args.get("limit", 100)), 500)
    c = conn()
    rows = c.execute("SELECT * FROM clips ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])


@app.post("/api/delete/<int:cid>")
def api_delete(cid):
    if not token_ok():
        abort(401)
    c = conn()
    row = c.execute("SELECT rel FROM clips WHERE id = ?", (cid,)).fetchone()
    if row is None:
        c.close()
        abort(404)
    try:
        os.remove(os.path.join(MEDIA, row["rel"]))
    except OSError:
        pass
    c.execute("DELETE FROM clips WHERE id = ?", (cid,))
    c.commit()
    c.close()
    return jsonify(ok=True)


@app.get("/media/<path:rel>")
@requires_auth
def media(rel):
    if ".." in rel:
        abort(400)
    return send_from_directory(MEDIA, rel, conditional=True)


@app.get("/")
@requires_auth
def index():
    c = conn()
    rows = c.execute("SELECT * FROM clips ORDER BY ts DESC LIMIT 300").fetchall()
    total, size = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM clips").fetchone()
    c.close()
    groups, order = {}, []
    for r in rows:
        day = (r["ts"] or "")[:10]
        if day not in groups:
            groups[day] = []
            order.append(day)
        groups[day].append(r)
    return render_template_string(
        PAGE,
        groups=[(d, groups[d]) for d in order],
        total=total,
        size_gb=round(size / 1024 ** 3, 2),
        days=RETENTION_DAYS,
    )


init_db()
cleanup()
threading.Thread(target=cleaner_loop, daemon=True, name="cleaner").start()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
