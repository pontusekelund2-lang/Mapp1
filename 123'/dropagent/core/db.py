"""SQLite-databas för DropAgent. All data sparas lokalt i data/dropagent.db."""
import json
import os
import sqlite3
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("DROPAGENT_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "dropagent.db")

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    message TEXT DEFAULT '',
    result_url TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS trend_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    focus TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    products_json TEXT DEFAULT '[]',
    research_text TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sparad',
    trend_json TEXT DEFAULT '{}',
    listing_json TEXT DEFAULT '{}',
    purchase_price REAL DEFAULT 0,
    shipping_cost REAL DEFAULT 0,
    supplier_url TEXT DEFAULT '',
    image_urls TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    shopify_id TEXT DEFAULT '',
    shopify_admin_url TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    message_id TEXT UNIQUE,
    imap_uid TEXT DEFAULT '',
    references_hdr TEXT DEFAULT '',
    attachments TEXT DEFAULT '',
    from_addr TEXT,
    from_name TEXT,
    subject TEXT,
    body TEXT,
    received_at TEXT,
    order_name TEXT DEFAULT '',
    order_json TEXT DEFAULT '',
    ai_json TEXT DEFAULT '',
    reply_text TEXT DEFAULT '',
    refund_amount REAL DEFAULT 0,
    refunded REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ny',
    error TEXT DEFAULT '',
    handled_at TEXT,
    handled_note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    purpose TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    web_searches INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ae_cache (
    key TEXT PRIMARY KEY,
    value TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS ae_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shopify_id TEXT NOT NULL UNIQUE,
    shopify_name TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ny',
    reason TEXT DEFAULT '',
    order_total REAL DEFAULT 0,
    cost_estimate REAL DEFAULT 0,
    items_json TEXT DEFAULT '[]',
    address_json TEXT DEFAULT '{}',
    ae_order_ids TEXT DEFAULT '',
    ae_status TEXT DEFAULT '',
    tracking_json TEXT DEFAULT '[]',
    tracking_pushed INTEGER DEFAULT 0,
    placed_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ny',
    trend_json TEXT DEFAULT '{}',
    competitors_json TEXT DEFAULT '{}',
    supplier_json TEXT DEFAULT '{}',
    price_json TEXT DEFAULT '{}',
    product_id INTEGER,
    reason TEXT DEFAULT '',
    error TEXT DEFAULT '',
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS retirements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'ny',
    reason TEXT DEFAULT '',
    stats_json TEXT DEFAULT '{}',
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    path TEXT,
    caption TEXT DEFAULT '',
    script_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'utkast',
    platforms_json TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT DEFAULT 'info',
    text TEXT
);
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# Kolumner som lagts till i senare versioner (läggs till automatiskt i befintliga databaser)
MIGRATIONS = {
    "products": {"suppliers_json": "TEXT DEFAULT '{}'", "backup_url": "TEXT DEFAULT ''", "uploaded_images": "TEXT DEFAULT ''",
                 "active_since": "TEXT DEFAULT ''", "retired_at": "TEXT DEFAULT ''", "sales_json": "TEXT DEFAULT '{}'",
                 "seo_json": "TEXT DEFAULT '{}'", "content_json": "TEXT DEFAULT '{}'",
                 # för automatiska ordrar: exakt AliExpress-annons, variant och fraktsätt
                 "ae_product_id": "TEXT DEFAULT ''", "ae_sku_attr": "TEXT DEFAULT ''", "ae_sku_label": "TEXT DEFAULT ''",
                 "ae_shipping_code": "TEXT DEFAULT ''", "ae_unit_price": "REAL DEFAULT 0"},
}


def init():
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.commit()


def query(sql, params=()):
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    with _lock, connect() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def insert(table, data):
    cols = ", ".join(data.keys())
    marks = ", ".join("?" for _ in data)
    return execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))


def update(table, row_id, data):
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", tuple(data.values()) + (row_id,))


def log(text, level="info"):
    insert("activity", {"created_at": now(), "level": level, "text": text})


def loads(text, default=None):
    try:
        return json.loads(text) if text else default
    except (ValueError, TypeError):
        return default
