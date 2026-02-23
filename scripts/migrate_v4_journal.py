#!/usr/bin/env python3
"""Idempotent SQLite migration adding the analysis_journal table."""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "trades.db"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS analysis_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL,
    conviction TEXT,
    total_score REAL NOT NULL,
    factors TEXT,
    reasoning TEXT,
    trade_idea TEXT,
    source TEXT DEFAULT 'krabbe',
    linked_trade_id INTEGER REFERENCES trades(id),
    outcome TEXT,
    outcome_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def migrate(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        print(f"Database not found at {db_path} — nothing to migrate (tables will be created on startup)")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Check if table already exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_journal'")
    if cursor.fetchone():
        print("Migration: analysis_journal table already exists, nothing to do")
        conn.close()
        return

    cursor.executescript(CREATE_TABLE)
    conn.commit()
    conn.close()
    print("Migration complete — created analysis_journal table")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    migrate(path)
