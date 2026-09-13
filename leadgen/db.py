import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT,
    raw_text TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    source_ref TEXT,
    intent TEXT,
    company_name TEXT,
    contact_name TEXT,
    phone TEXT,
    email TEXT,
    telegram_username TEXT,
    location TEXT,
    area_sqm TEXT,
    budget TEXT,
    confidence REAL,
    notes TEXT,
    status TEXT DEFAULT 'new',
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def raw_item_seen(conn: sqlite3.Connection, source: str, source_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM raw_items WHERE source = ? AND source_id = ?", (source, source_id)
    ).fetchone()
    return row is not None


def save_raw_item(conn: sqlite3.Connection, source: str, source_id: str, url: Optional[str], raw_text: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO raw_items (source, source_id, url, raw_text, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (source, source_id, url, raw_text, now_iso()),
    )


def save_lead(conn: sqlite3.Connection, dedup_key: str, source: str, source_ref: Optional[str], fields: dict) -> bool:
    """Returns True if a new lead row was inserted, False if it was a duplicate."""
    try:
        conn.execute(
            """
            INSERT INTO leads (
                dedup_key, source, source_ref, intent, company_name, contact_name,
                phone, email, telegram_username, location, area_sqm, budget,
                confidence, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dedup_key,
                source,
                source_ref,
                fields.get("intent"),
                fields.get("company_name"),
                fields.get("contact_name"),
                fields.get("phone"),
                fields.get("email"),
                fields.get("telegram_username"),
                fields.get("location"),
                fields.get("area_sqm"),
                fields.get("budget"),
                fields.get("confidence"),
                fields.get("notes"),
                now_iso(),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def count_leads(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
