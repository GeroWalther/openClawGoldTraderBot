#!/usr/bin/env python3
"""Idempotent SQLite migration for v4 columns on the trades table."""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "trades.db"

NEW_COLUMNS = [
    ("conviction", "TEXT"),
    ("expected_price", "REAL"),
    ("actual_price", "REAL"),
    ("spread_at_entry", "REAL"),
]


def migrate(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        print(f"Database not found at {db_path} — nothing to migrate (tables will be created on startup)")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Get existing columns
    cursor.execute("PRAGMA table_info(trades)")
    existing = {row[1] for row in cursor.fetchall()}

    added = []
    for col_name, col_type in NEW_COLUMNS:
        if col_name not in existing:
            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
            added.append(col_name)

    conn.commit()
    conn.close()

    if added:
        print(f"Migration complete — added columns: {', '.join(added)}")
    else:
        print("Migration: all columns already exist, nothing to do")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    migrate(path)
