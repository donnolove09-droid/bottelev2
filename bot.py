import html
import mimetypes
import os
import re
import sqlite3
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, request, send_from_directory

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "data.db")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
PORT = int(os.environ.get("PORT", "10000"))
ADMIN_KEY = os.environ.get("ADMIN_KEY", "change-me")
FILE_LIMIT = 50 * 1024 * 1024  # giới hạn upload của Telegram Bot API

os.makedirs(UPLOAD_DIR, exist_ok=True)
app = Flask(__name__, static_folder=".")
app.config["MAX_CONTENT_LENGTH"] = FILE_LIMIT + 2 * 1024 * 1024

COMMANDS = [
    ("help", "Trợ giúp"),
    ("id", "Lấy ID phòng chat / người dùng"),
    ("star", "Chức năng Star"),
    ("file", "Kho file - nhận file từ bot"),
    ("aov", "Chức năng AOV"),
    ("mod", "Chức năng Moderator"),
    ("ff", "Chức năng Free Fire"),
    ("ban", "Cấm thành viên"),
    ("unl", "Gỡ cấm thành viên"),
]


# ---------------------------------------------------------------- DB
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, col, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        title TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        UNIQUE(bot_id, chat_id),
        FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        message TEXT NOT NULL,
        interval_seconds INTEGER NOT NULL DEFAULT 3600,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_sent REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        stored TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        mime TEXT,
        created REAL NOT NULL DEFAULT 0
    );
    """)
    # migrate từ bản cũ
    ensure_column(conn, "bots", "username", "TEXT")
    ensure_column(conn, "chats", "type", "TEXT")
    ensure_column(conn, "chats", "username", "TEXT")
    ensure_column(conn, "chats", "last_seen", "REAL DEFAULT 0")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- Telegram helpers
def auth():
    return request.headers.get("X-Admin-Key") == ADMIN_KEY


def tg(token, method, payload=None):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload or {},
            timeout=40,
        )
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


def send_text(token, chat_id, text, **kwargs):
    payload = {"chat_id": chat_id, "text": text}
    payload.update(kwargs)
    return tg(token, "sendMessage", payload)


def setup_commands(token):
    return tg(token, "setMyCommands", {
        "commands": [{"command": c, "description": d} for c, d in COMMANDS]
    })


def get_bot_token(bot_id):
    conn = db()
    row = conn.execute(
        "SELECT token FROM bots WHERE id=? AND enabled=1", (bot_id,)
    ).fetchone()
    conn.close()
    return row["token"] if row else None


def is_admin(token, chat_id, user_id):
    if not user_id:
        return False
    r = tg(token, "getChatMember", {"chat_id": chat_id, "user_id": user_id})
    return bool(r.get("ok")) and r.get("result", {}).get("status") in ("administrator", "creator")


def tg_response(res):
    """Trả JSON + status code đúng theo kết quả Telegram."""
    if res.get("ok"):
        return jsonify(res)
    return jsonify({**res, "error": res.get("description", "Telegram báo lỗi")}), 400


# ---------------------------------------------------------------- Chat discovery
def chat_label(chat):
    if chat.get("title"):
        return chat["title"]
    name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
    if name:
        return name
    return ("@" + chat["username"]) if chat.get("username") else str(chat.get("id"))


def remember_chat(bot_id, chat):
    if not chat or chat.get("id") is None:
        return
    conn = db()
    conn.execute("""
        INSERT INTO chats(bot_id,chat_id,title,type,username,last_seen) VALUES(?,?,?,?,?,?)
        ON CONFLICT(bot_id,chat_id) DO UPDATE SET
            title=excluded.title, type=excluded.type,
            username=excluded.username, last_seen=excluded.last_seen
    """, (bot_id, str(chat["id"]), chat_label(chat), chat.get("type"),
          chat.get("username"), time.time()))
    conn.commit()
    conn.close()


def forget_chat(bot_id, chat_id):
    conn = db()
    conn.execute("DELETE FROM chats WHERE bot_id=? AND chat_id=?", (bot_id, str(chat_id)))
    conn.commit()
    conn.close()


def id_card(chat, user=None, replied=None):
    lines = [
        "🆔 <b>THÔNG TIN ID</b>",
        "",
        f"💬 Phòng chat: <b>{html.escape(chat_label(chat))}</b>",
        f"📌 Chat ID: <code>{chat.get('id')}</code>",
        f"🏷️ Loại: {html.escape(str(chat.get('type', '?')))}",
    ]
    if user:
        lines.append(f"👤 ID của bạn: <code>{user.get('id')}</code>")
    if replied:
        lines.append(f"↩️ ID người được reply: <code>{replied.get('id')}</code>")
    lines += ["", "Chạm vào số ID để sao chép."]
    return "\n".join(lines)


# ---------------------------------------------------------------- Files
def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def pick_method(name, size, as_document):
    ext = os.path.splitext(name)[1].lower()
    if as_document:
        return "sendDocument", "document"
    if ext in (".jpg", ".jpeg", ".png", ".webp") and size <= 10 * 1024 * 1024:
        return "sendPhoto", "photo"
    if ext == ".gif":
        return "sendAnimation", "animation"
    if ext in (".mp4", ".mov", ".m4v"):
        return "sendVideo", "video"
    if ext in (".mp3", ".m4a"):
        return "sendAudio", "audio"
    return "sendDocument", "document"


def send_file(token, chat_id, path, name, caption="", as_document=False, reply_to=None):
    method, field = pick_method(name, os.path.getsize(path), as_document)

    def attempt(m, f):
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        if reply_to:
            data["reply_to_message_id"] = reply_to
        try:
            with open(path, "rb") as fh:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/{m}",
                    data=data, files={f: (name, fh)}, timeout=300,
                )
            return r.json()
        except Exception as e:
            return {"ok": False, "description": str(e)}

    res = attempt(method, field)
    if not res.get("ok") and method != "sendDocument":
        res = attempt("sendDocument", "document")  # fallback: gửi dạng file gốc
    return res


def get_file_row(file_id):
    conn = db()
    row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    conn.close()
    return row


def save_upload(f):
    name = os.path.basename((f.filename or "file").replace("\\", "/")).strip() or "file"
    ext = os.path.splitext(name)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
        ext = ""
    stored = uuid.uuid4().hex + ext
    path = os.path.join(UPLOAD_DIR, stored)
    f.save(path)
    size = os.path.getsize(path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    conn = db()
    cur = conn.execute(
        "INSERT INTO files(name,stored,size,mime,created) VALUES(?,?,?,?,?)",
        (name, stored, size, mime, time.time()),
    )
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return fid


def files_menu(token, chat_id, reply_to):
    conn = db()
    rows = conn.execute("SELECT id,name,size FROM files ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    if not rows:
        send_text(token, chat_id, "📁 Kho file đang trống.\nUpload file ở dashboard (tab FILE).",
                  reply_to_message_id=reply_to)
        return
    keyboard = [[{"text": f"📄 {r['name'][:38]} · {human_size(r['size'])}",
                  "callback_data": f"f:{r['id']}"}] for r in rows]
    send_text(token, chat_id, "📁 KHO FILE\nChọn file bên dưới để nhận:",
              reply_to_message_id=reply_to, reply_markup={"inline_keyboard": keyboard})


# ---------------------------------------------------------------- Command handling
def command_help():
    return (
        "📋 MENU LỆNH\n\n"
        "/help - Trợ giúp\n"
        "/id - Lấy ID phòng chat\n"
        "/star - Chức năng Star\n"
        "/file - Kho file (nhận file từ bot)\n"
        "/aov - Chức năng AOV\n"
        "/mod - Chức năng Moderator\n"
        "/ff - Chức năng Free Fire\n"
        "/ban - Cấm thành viên\n"
        "/unl - Gỡ cấm thành viên\n"
    )


def handle_callback(bot_id, token, cb):
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    data = cb.get("data", "")
    remember_chat(bot_id, chat)
    if not (data.startswith("f:") and data[2:].isdigit() and chat.get("id")):
        tg(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
        return
    row = get_file_row(int(data[2:]))
    path = row and os.path.join(UPLOAD_DIR, row["stored"])
    if not row or not os.path.exists(path):
        tg(token, "answerCallbackQuery", {
            "callback_query_id": cb["id"], "text": "File không còn tồn tại", "show_alert": True})
        return
    tg(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "📤 Đang gửi file..."})
    send_file(token, chat["id"], path, row["name"])


def handle_update(bot_id, token, update):
    # Bot được thêm / bị xóa khỏi nhóm
    mcm = update.get("my_chat_member")
    if mcm:
        chat = mcm.get("chat") or {}
        new = (mcm.get("new_chat_member") or {}).get("status")
        old = (mcm.get("old_chat_member") or {}).get("status")
        if new in ("left", "kicked"):
            forget_chat(bot_id, chat.get("id"))
        else:
            remember_chat(bot_id, chat)
            if old in ("left", "kicked") and chat.get("type") in ("group", "supergroup"):
                send_text(token, chat["id"], id_card(chat), parse_mode="HTML")
        return

    if update.get("callback_query"):
        handle_callback(bot_id, token, update["callback_query"])
        return

    msg = update.get("message") or update.get("edited_message") or update.get("channel_post")
    if not msg:
        return
    remember_chat(bot_id, msg.get("chat"))
    if "text" not in msg:
        return

    text = msg["text"].strip()
    if not text.startswith("/"):
        return

    raw = text.split()[0]
    command = raw[1:].split("@")[0].lower()
    args = text.split()[1:]
    chat = msg["chat"]
    chat_id = chat["id"]
    user = msg.get("from", {})
    user_id = user.get("id")
    reply_to = msg.get("message_id")

    if command in ("help", "commands", "start"):
        send_text(token, chat_id, command_help(), reply_to_message_id=reply_to)

    elif command == "id":
        replied = (msg.get("reply_to_message") or {}).get("from")
        send_text(token, chat_id, id_card(chat, user, replied),
                  parse_mode="HTML", reply_to_message_id=reply_to)

    elif command == "star":
        send_text(token, chat_id, "⭐ STAR\n\nChức năng Star đang sẵn sàng.", reply_to_message_id=reply_to)

    elif command == "file":
        if args and args[0].isdigit():
            row = get_file_row(int(args[0]))
            path = row and os.path.join(UPLOAD_DIR, row["stored"])
            if row and os.path.exists(path):
                send_file(token, chat_id, path, row["name"], reply_to=reply_to)
            else:
                send_text(token, chat_id, "❌ Không tìm thấy file.", reply_to_message_id=reply_to)
        else:
            files_menu(token, chat_id, reply_to)

    elif command == "aov":
        send_text(token, chat_id, "🎮 AOV\n\nMenu AOV đang sẵn sàng.", reply_to_message_id=reply_to)

    elif command == "ff":
        send_text(token, chat_id, "🔥 FREE FIRE\n\nMenu Free Fire đang sẵn sàng.", reply_to_message_id=reply_to)

    elif command == "mod":
        if is_admin(token, chat_id, user_id):
            send_text(
                token, chat_id,
                "🛡️ MODERATOR\n\n/ban - Ban thành viên\n/unl - Gỡ ban\n/status - Kiểm tra bot",
                reply_to_message_id=reply_to,
            )
        else:
            send_text(token, chat_id, "⛔ Lệnh này chỉ dành cho quản trị viên.", reply_to_message_id=reply_to)

    elif command == "status":
        send_text(token, chat_id, "🟢 Bot đang hoạt động.", reply_to_message_id=reply_to)

    elif command in ("ban", "unl"):
        if not is_admin(token, chat_id, user_id):
            send_text(token, chat_id, "⛔ Bạn cần là quản trị viên để dùng lệnh này.", reply_to_message_id=reply_to)
            return

        target_id = None
        if msg.get("reply_to_message"):
            target_id = msg["reply_to_message"].get("from", {}).get("id")
        elif args:
            try:
                target_id = int(args[0])
            except ValueError:
                pass

        if not target_id:
            send_text(
                token, chat_id,
                f"ℹ️ Dùng /{command} bằng cách reply tin nhắn của thành viên hoặc: /{command} USER_ID",
                reply_to_message_id=reply_to,
            )
            return

        if command == "ban":
            result = tg(token, "banChatMember", {"chat_id": chat_id, "user_id": target_id})
            answer = "✅ Đã ban thành viên." if result.get("ok") else "❌ Không thể ban: " + str(result.get("description", "unknown"))
        else:
            result = tg(token, "unbanChatMember", {"chat_id": chat_id, "user_id": target_id, "only_if_banned": True})
            answer = "✅ Đã gỡ ban." if result.get("ok") else "❌ Không thể gỡ ban: " + str(result.get("description", "unknown"))

        send_text(token, chat_id, answer, reply_to_message_id=reply_to)


def poll_bot(bot_id, token):
    offset = 0
    setup_commands(token)
    me = tg(token, "getMe")
    if me.get("ok"):
        conn = db()
        conn.execute("UPDATE bots SET username=? WHERE id=?", (me["result"].get("username"), bot_id))
        conn.commit()
        conn.close()
    while True:
        if get_bot_token(bot_id) != token:  # bot đã bị xóa/tắt -> dừng poller
            return
        try:
            result = tg(token, "getUpdates", {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": ["message", "edited_message", "channel_post",
                                    "my_chat_member", "callback_query"],
            })
            if result.get("ok"):
                for update in result.get("result", []):
                    offset = max(offset, update["update_id"] + 1)
                    try:
                        handle_update(bot_id, token, update)
                    except Exception as e:
                        print("handle_update error:", e)
            else:
                time.sleep(3)
        except Exception:
            time.sleep(3)


def start_pollers():
    conn = db()
    rows = conn.execute("SELECT id,token FROM bots WHERE enabled=1").fetchall()
    conn.close()
    for row in rows:
        threading.Thread(target=poll_bot, args=(row["id"], row["token"]), daemon=True).start()


# ---------------------------------------------------------------- API
@app.before_request
def protect_api():
    if request.path.startswith("/api/") and not auth():
        return jsonify({"ok": False, "error": "Sai ADMIN_KEY"}), 401


@app.errorhandler(413)
def too_large(_):
    return jsonify({"ok": False, "error": "File vượt quá 50MB (giới hạn của Telegram Bot API)"}), 413


@app.get("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.get("/api/bots")
def bots():
    conn = db()
    rows = conn.execute("SELECT id,name,username,enabled FROM bots ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(x) for x in rows])


@app.post("/api/bots")
def add_bot():
    data = request.json or {}
    name, token = data.get("name", "").strip(), data.get("token", "").strip()
    if not name or not token:
        return jsonify({"ok": False, "error": "name và token là bắt buộc"}), 400

    me = tg(token, "getMe")
    if not me.get("ok"):
        return jsonify({"ok": False, "error": "Bot token không hợp lệ", "telegram": me}), 400

    commands = setup_commands(token)
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO bots(name,token,username) VALUES(?,?,?)",
            (name, token, me["result"].get("username")),
        )
        conn.commit()
        bot_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"ok": False, "error": "Token đã tồn tại"}), 409
    conn.close()

    threading.Thread(target=poll_bot, args=(bot_id, token), daemon=True).start()
    return jsonify({"ok": True, "id": bot_id, "bot": me["result"], "commands": commands})


@app.delete("/api/bots/<int:bot_id>")
def delete_bot(bot_id):
    conn = db()
    conn.execute("DELETE FROM chats WHERE bot_id=?", (bot_id,))
    conn.execute("DELETE FROM schedules WHERE bot_id=?", (bot_id,))
    conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/api/bots/<int:bot_id>/commands")
def reset_commands(bot_id):
    token = get_bot_token(bot_id)
    if not token:
        return jsonify({"ok": False, "error": "Bot không tồn tại hoặc đang tắt"}), 404
    return tg_response(setup_commands(token))


CHAT_COLS = "c.id,c.bot_id,c.chat_id,c.title,c.type,c.username,c.last_seen,b.name bot_name,b.username bot_username"


@app.get("/api/chats")
def all_chats():
    conn = db()
    rows = conn.execute(f"""
        SELECT {CHAT_COLS} FROM chats c JOIN bots b ON b.id=c.bot_id
        ORDER BY c.last_seen DESC, c.id DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(x) for x in rows])


@app.delete("/api/chats/<int:cid>")
def delete_chat(cid):
    conn = db()
    conn.execute("DELETE FROM chats WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.get("/api/bots/<int:bot_id>/chats")
def chats(bot_id):
    conn = db()
    rows = conn.execute(
        "SELECT id,chat_id,title,type,enabled FROM chats WHERE bot_id=? ORDER BY last_seen DESC, id DESC",
        (bot_id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(x) for x in rows])


@app.post("/api/bots/<int:bot_id>/check-chat")
def check_chat(bot_id):
    """Thêm phòng chat thủ công: nhận ID, @username hoặc link t.me/xxx."""
    data = request.json or {}
    chat_id = str(data.get("chat_id", "")).strip()
    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$", chat_id)
    if m:
        chat_id = "@" + m.group(1)
    if not chat_id:
        return jsonify({"ok": False, "error": "Thiếu chat_id"}), 400
    token = get_bot_token(bot_id)
    if not token:
        return jsonify({"ok": False, "error": "Bot không tồn tại"}), 404

    result = tg(token, "getChat", {"chat_id": chat_id})
    if not result.get("ok"):
        return jsonify({**result, "error": result.get("description", "Không tìm thấy chat")}), 400

    remember_chat(bot_id, result["result"])
    return jsonify({"ok": True, "chat": result["result"]})


@app.get("/api/bots/<int:bot_id>/chat/<path:chat_id>/permissions")
def permissions(bot_id, chat_id):
    token = get_bot_token(bot_id)
    if not token:
        return jsonify({"ok": False, "error": "Bot không tồn tại"}), 404
    me = tg(token, "getMe")
    if not me.get("ok"):
        return jsonify(me)
    return jsonify(tg(token, "getChatMember", {"chat_id": chat_id, "user_id": me["result"]["id"]}))


@app.post("/api/send")
def send_message():
    data = request.json or {}
    bot_id = data.get("bot_id")
    chat_id = str(data.get("chat_id", ""))
    message = data.get("message", "")
    if not bot_id or not chat_id or not message:
        return jsonify({"ok": False, "error": "Thiếu bot_id/chat_id/message"}), 400
    token = get_bot_token(bot_id)
    if not token:
        return jsonify({"ok": False, "error": "Bot không tồn tại hoặc đang tắt"}), 404
    return tg_response(send_text(token, chat_id, message))


# ---- Files
@app.get("/api/files")
def list_files():
    conn = db()
    rows = conn.execute("SELECT id,name,size,mime,created FROM files ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(x) for x in rows])


@app.post("/api/files")
def upload_file():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "Chưa chọn file"}), 400
    return jsonify({"ok": True, "id": save_upload(f)})


@app.delete("/api/files/<int:fid>")
def delete_file(fid):
    row = get_file_row(fid)
    if row:
        try:
            os.remove(os.path.join(UPLOAD_DIR, row["stored"]))
        except OSError:
            pass
        conn = db()
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/send-file")
def send_file_api():
    """multipart/form-data: bot_id, chat_id, caption, as_document, save, và (file | file_id)."""
    f = request.form
    token = get_bot_token(f.get("bot_id", type=int))
    chat_id = (f.get("chat_id") or "").strip()
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "Thiếu bot hoặc phòng chat"}), 400

    as_doc = f.get("as_document") in ("1", "true", "on")
    upload = request.files.get("file")
    temp_path = None

    if upload:
        if f.get("save") in ("1", "true", "on"):
            fid = save_upload(upload)
            row = get_file_row(fid)
            path, name = os.path.join(UPLOAD_DIR, row["stored"]), row["name"]
        else:
            name = os.path.basename((upload.filename or "file").replace("\\", "/")) or "file"
            temp_path = os.path.join(UPLOAD_DIR, "tmp_" + uuid.uuid4().hex)
            upload.save(temp_path)
            path = temp_path
    else:
        row = get_file_row(f.get("file_id", type=int) or 0)
        if not row:
            return jsonify({"ok": False, "error": "Chưa chọn file"}), 400
        path, name = os.path.join(UPLOAD_DIR, row["stored"]), row["name"]

    try:
        if not os.path.exists(path):
            return jsonify({"ok": False, "error": "File không còn trên server"}), 404
        if os.path.getsize(path) > FILE_LIMIT:
            return jsonify({"ok": False, "error": "File vượt quá 50MB"}), 413
        res = send_file(token, chat_id, path, name, f.get("caption", ""), as_doc)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
    return tg_response(res)


# ---- Schedules
@app.get("/api/schedules")
def schedules():
    conn = db()
    rows = conn.execute("""
        SELECT s.*,b.name bot_name FROM schedules s
        JOIN bots b ON b.id=s.bot_id ORDER BY s.id DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(x) for x in rows])


@app.post("/api/schedules")
def add_schedule():
    d = request.json or {}
    required = ["bot_id", "chat_id", "message", "interval_seconds"]
    if any(k not in d for k in required):
        return jsonify({"ok": False, "error": "Thiếu dữ liệu"}), 400
    try:
        interval = max(60, int(d["interval_seconds"]))
    except Exception:
        return jsonify({"ok": False, "error": "interval_seconds không hợp lệ"}), 400

    conn = db()
    conn.execute("""
        INSERT INTO schedules(bot_id,chat_id,message,interval_seconds)
        VALUES(?,?,?,?)
    """, (d["bot_id"], str(d["chat_id"]), d["message"], interval))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.delete("/api/schedules/<int:sid>")
def delete_schedule(sid):
    conn = db()
    conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


def scheduler():
    while True:
        try:
            now = time.time()
            conn = db()
            rows = conn.execute("""
                SELECT s.*,b.token FROM schedules s
                JOIN bots b ON b.id=s.bot_id
                WHERE s.enabled=1 AND b.enabled=1
            """).fetchall()
            for r in rows:
                if now - float(r["last_sent"] or 0) >= int(r["interval_seconds"]):
                    res = send_text(r["token"], r["chat_id"], r["message"])
                    if res.get("ok"):
                        conn.execute("UPDATE schedules SET last_sent=? WHERE id=?", (now, r["id"]))
                        conn.commit()
            conn.close()
        except Exception:
            pass
        time.sleep(10)


init_db()
threading.Thread(target=scheduler, daemon=True).start()
start_pollers()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
