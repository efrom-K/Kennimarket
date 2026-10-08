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

-- Ваши объекты (склады в продаже) — для подбора под запрос.
CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    address TEXT,
    direction TEXT,
    mkad_km REAL,
    area_sqm REAL,
    class TEXT,
    price_rub REAL,
    ceiling_m REAL,
    gates TEXT,
    heating TEXT,
    presentation_url TEXT,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

-- Первое сообщение лиду: анкета запроса от модели, подбор объектов, черновик.
CREATE TABLE IF NOT EXISTS lead_profiles (
    lead_id INTEGER PRIMARY KEY REFERENCES leads(id),
    profile_json TEXT NOT NULL,
    matches_json TEXT,
    draft TEXT,
    draft_edited TEXT,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first release; ALTERed into existing DBs by init_db.
NEW_LEAD_COLUMNS = ["posted_at TEXT", "buyer_type TEXT", "comment TEXT", "sent_at TEXT", "lead_kind TEXT"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn(db_path: str):
    # Автокоммит: долгий скан можно прервать Ctrl+C без потери уже собранного.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        for col in NEW_LEAD_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # колонка уже есть


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
                confidence, notes, posted_at, buyer_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                fields.get("posted_at"),
                fields.get("buyer_type"),
                now_iso(),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def count_leads(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row and row[0] is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
