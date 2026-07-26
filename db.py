"""用户账号存储（SQLite）。密码只存哈希，绝不存明文。"""
import sqlite3, json, os
from config import DB_PATH


def _conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                roles TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )


def get_user(username: str):
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not r:
            return None
        return {
            "id": r["id"],
            "username": r["username"],
            "password_hash": r["password_hash"],
            "roles": json.loads(r["roles"]),
        }


def create_user(username: str, password_hash: str, roles: list):
    with _conn() as c:
        c.execute(
            "INSERT INTO users(username, password_hash, roles) VALUES(?,?,?)",
            (username, password_hash, json.dumps(roles)),
        )


def set_password(username: str, password_hash: str):
    with _conn() as c:
        c.execute("UPDATE users SET password_hash=? WHERE username=?", (password_hash, username))


def list_users():
    with _conn() as c:
        rows = c.execute("SELECT username, roles, created_at FROM users ORDER BY id").fetchall()
        return [
            {"username": r["username"], "roles": json.loads(r["roles"]), "created_at": r["created_at"]}
            for r in rows
        ]
