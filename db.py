import sqlite3
import os

import app_paths

DB_PATH = app_paths.db_path()
SYSTEM_PROMPT_PATH = app_paths.system_prompt_resource_path()

HISTORY_WINDOW = 20
SUMMARY_THRESHOLD = 30


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init():
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                model TEXT NOT NULL DEFAULT 'glm-5',
                image_model TEXT,
                summary TEXT
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                about TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS cron_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                schedule TEXT NOT NULL,
                command TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run TEXT,
                next_run TEXT,
                last_result TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                image_b64 TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_chat_history_user
                ON chat_history(user_id, created_at);
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(user_settings)").fetchall()]
        if "summary" not in cols:
            conn.execute("ALTER TABLE user_settings ADD COLUMN summary TEXT")
        if "image_model" not in cols:
            conn.execute("ALTER TABLE user_settings ADD COLUMN image_model TEXT")
        
        chat_cols = [r["name"] for r in conn.execute("PRAGMA table_info(chat_history)").fetchall()]
        if "image_b64" not in chat_cols:
            conn.execute("ALTER TABLE chat_history ADD COLUMN image_b64 TEXT")


def has_user_profile(user_id: int) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row is not None


def get_user_profile(user_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT name, about, created_at, updated_at FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "about": row["about"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def upsert_user_profile(user_id: int, name: str, about: str) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO user_profiles (user_id, name, about) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   name = excluded.name,
                   about = excluded.about,
                   updated_at = datetime('now')""",
            (user_id, name.strip(), about.strip()),
        )


def add_cron_job(
    user_id: int,
    name: str,
    schedule: str,
    command: str,
    timezone: str,
    next_run: str,
) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO cron_jobs (user_id, name, schedule, command, timezone, next_run)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, name, schedule, command, timezone, next_run),
        )
        return int(cur.lastrowid)


def list_cron_jobs(user_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, name, schedule, command, timezone, enabled, last_run, next_run, last_result
               FROM cron_jobs
               WHERE user_id = ?
               ORDER BY id ASC""",
            (user_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "schedule": r["schedule"],
            "command": r["command"],
            "timezone": r["timezone"],
            "enabled": bool(r["enabled"]),
            "last_run": r["last_run"],
            "next_run": r["next_run"],
            "last_result": r["last_result"],
        }
        for r in rows
    ]


def remove_cron_job(user_id: int, job_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM cron_jobs WHERE user_id = ? AND id = ?",
            (user_id, job_id),
        )
        return cur.rowcount > 0


def get_due_cron_jobs(now_iso: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, user_id, name, schedule, command, timezone, next_run
               FROM cron_jobs
               WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ?
               ORDER BY next_run ASC""",
            (now_iso,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "user_id": r["user_id"],
            "name": r["name"],
            "schedule": r["schedule"],
            "command": r["command"],
            "timezone": r["timezone"],
            "next_run": r["next_run"],
        }
        for r in rows
    ]


def update_cron_run(job_id: int, last_run: str, next_run: str, last_result: str) -> None:
    with _conn() as conn:
        conn.execute(
            """UPDATE cron_jobs
               SET last_run = ?, next_run = ?, last_result = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (last_run, next_run, last_result, job_id),
        )


def read_system_prompt() -> str | None:
    if not os.path.isfile(SYSTEM_PROMPT_PATH):
        return None
    with open(SYSTEM_PROMPT_PATH, "r") as f:
        content = f.read().strip()
    return content if content else None


def get_model(user_id: int) -> str:
    with _conn() as conn:
        row = conn.execute(
            "SELECT model FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row and row["model"]:
            return row["model"]
    env_model = os.getenv("MAIN_MODEL", "").strip()
    if env_model:
        return env_model
    import model_router
    return model_router.best_main_model()


def get_image_model(user_id: int) -> str:
    with _conn() as conn:
        row = conn.execute(
            "SELECT image_model FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row and row["image_model"]:
            return row["image_model"]
    env_model = os.getenv("IMAGE_MODEL", "").strip()
    if env_model:
        return env_model
    import model_router
    return model_router.best_image_model()


def set_model(user_id: int, model: str) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO user_settings (user_id, model) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET model = excluded.model""",
            (user_id, model),
        )


def set_image_model(user_id: int, image_model: str) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO user_settings (user_id, image_model) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET image_model = excluded.image_model""",
            (user_id, image_model),
        )


def get_summary(user_id: int) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT summary FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row and row["summary"]:
            return row["summary"]
    return None


def set_summary(user_id: int, summary: str) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO user_settings (user_id, model, summary) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET summary = excluded.summary""",
            (user_id, get_model(user_id), summary),
        )


def add_message(user_id: int, role: str, content: str, image_b64: str | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO chat_history (user_id, role, content, image_b64) VALUES (?, ?, ?, ?)",
            (user_id, role, content, image_b64),
        )


def count_messages(user_id: int) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM chat_history WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["cnt"]


def get_history(user_id: int, limit: int = HISTORY_WINDOW) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT role, content, image_b64 FROM chat_history
               WHERE user_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"], "image_b64": r["image_b64"]} for r in reversed(rows)]


def get_older_messages(user_id: int, keep_recent: int = HISTORY_WINDOW) -> list[dict]:
    with _conn() as conn:
        total = count_messages(user_id)
        if total <= keep_recent:
            return []
        offset = total - keep_recent
        rows = conn.execute(
            """SELECT role, content, image_b64 FROM chat_history
               WHERE user_id = ?
               ORDER BY created_at ASC
               LIMIT ?""",
            (user_id, offset),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"], "image_b64": r["image_b64"]} for r in rows]


def compact_history(user_id: int, keep_recent: int = HISTORY_WINDOW) -> int:
    keep = max(0, int(keep_recent))
    with _conn() as conn:
        if keep == 0:
            cur = conn.execute(
                "DELETE FROM chat_history WHERE user_id = ?",
                (user_id,),
            )
            return int(cur.rowcount or 0)

        cur = conn.execute(
            """DELETE FROM chat_history
               WHERE user_id = ?
                 AND id NOT IN (
                     SELECT id FROM chat_history
                     WHERE user_id = ?
                     ORDER BY id DESC
                     LIMIT ?
                 )""",
            (user_id, user_id, keep),
        )
        return int(cur.rowcount or 0)


def clear_history(user_id: int) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM chat_history WHERE user_id = ?",
            (user_id,),
        )
        conn.execute(
            "UPDATE user_settings SET summary = NULL WHERE user_id = ?",
            (user_id,),
        )
        return cur.rowcount
