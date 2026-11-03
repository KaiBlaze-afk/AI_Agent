import json
import sqlite3
import uuid
from datetime import datetime
from config import DB_PATH


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                think_text TEXT,
                logs_json TEXT,
                steps_json TEXT,
                images_json TEXT,
                created_at TEXT
            )
            """
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "images_json" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN images_json TEXT DEFAULT '[]'")
        if "think_text" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN think_text TEXT DEFAULT ''")


def db_get_sessions() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, created_at FROM sessions ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def db_create_session(title: str = "New Chat") -> dict:
    sid = str(uuid.uuid4())[:8]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at) VALUES (?, ?, ?)",
            (sid, title, now),
        )
    return {"id": sid, "title": title, "created_at": now}


def db_delete_session(sid: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))


def db_get_messages(sid: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content, think_text, logs_json, steps_json, images_json FROM messages WHERE session_id = ? ORDER BY id ASC",
            (sid,),
        ).fetchall()
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "think": r["think_text"] or "",
                "logs": json.loads(r["logs_json"] or "[]"),
                "steps": json.loads(r["steps_json"] or "[]"),
                "images": json.loads(r["images_json"] or "[]"),
            }
            for r in rows
        ]


def db_save_message(
    sid: str, role: str, content: str, think: str = "", logs=None, steps=None, images=None
):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, think_text, logs_json, steps_json, images_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                role,
                content,
                think or "",
                json.dumps(logs or []),
                json.dumps(steps or []),
                json.dumps(images or []),
                now,
            ),
        )
        if role == "user":
            cur = conn.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
            if cur and cur[0] == "New Chat":
                clean_title = content.strip().split("\n")[0][:36]
                conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (clean_title, sid))


# Ensure database tables are created on module import
init_db()
