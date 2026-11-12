"""SQLite: пользователи, устройства, файлы, журнал."""
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    folder TEXT UNIQUE NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS devices(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_seen INTEGER,
    last_ip TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS uploads(
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    taken_at INTEGER,
    created_at INTEGER NOT NULL
);
-- файлы никогда не удаляются вместе с пользователем (нет ON DELETE CASCADE)
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    taken_at INTEGER,
    created_at INTEGER NOT NULL,
    mirrored INTEGER NOT NULL DEFAULT 0,
    last_verified INTEGER,
    UNIQUE(user_id, sha256)
);
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    user_id INTEGER,
    event TEXT NOT NULL,
    ip TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id);
CREATE INDEX IF NOT EXISTS idx_files_unmirrored ON files(mirrored);
"""


def connect(cfg) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(cfg.db_path, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextmanager
def conn(cfg):
    c = connect(cfg)
    try:
        yield c
    finally:
        c.close()


def init(cfg) -> None:
    with conn(cfg) as c:
        c.executescript(SCHEMA)


def audit(c: sqlite3.Connection, user_id, event: str, ip: str | None = None, detail: str | None = None) -> None:
    c.execute(
        "INSERT INTO audit(ts, user_id, event, ip, detail) VALUES(?,?,?,?,?)",
        (int(time.time()), user_id, event, ip, (detail or "")[:500]),
    )
