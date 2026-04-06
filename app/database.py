"""Async SQLite database layer using aiosqlite."""

import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Optional

from app.config import get_settings

_db: Optional[aiosqlite.Connection] = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_sub TEXT NOT NULL,
    user_name TEXT NOT NULL,
    session_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT 'New conversation',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_sub);
"""


async def get_db() -> aiosqlite.Connection:
    """Get or create the database connection."""
    global _db
    if _db is None:
        settings = get_settings()
        Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(settings.database_path)
        _db.row_factory = aiosqlite.Row
        await _db.executescript(SCHEMA_SQL)
        await _db.commit()
    return _db


async def close_db():
    """Close the database connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def create_conversation(
    user_sub: str, user_name: str, session_key: str, title: str = "New conversation"
) -> dict:
    """Insert a new conversation and return it."""
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO conversations (user_sub, user_name, session_key, title)
           VALUES (?, ?, ?, ?)""",
        (user_sub, user_name, session_key, title),
    )
    await db.commit()
    row = await (
        await db.execute(
            "SELECT * FROM conversations WHERE id = ?", (cursor.lastrowid,)
        )
    ).fetchone()
    return dict(row)


async def get_conversations_for_user(user_sub: str) -> list[dict]:
    """Get all conversations for a user, newest first."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT * FROM conversations
           WHERE user_sub = ?
           ORDER BY updated_at DESC""",
        (user_sub,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_conversation(conversation_id: int, user_sub: str) -> Optional[dict]:
    """Get a single conversation, scoped to the user."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_sub = ?",
        (conversation_id, user_sub),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_conversation_by_session_key(
    session_key: str, user_sub: str
) -> Optional[dict]:
    """Get a conversation by its session key, scoped to the user."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM conversations WHERE session_key = ? AND user_sub = ?",
        (session_key, user_sub),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_conversation_title(
    conversation_id: int, user_sub: str, title: str
) -> Optional[dict]:
    """Update a conversation's title, scoped to the user."""
    db = await get_db()
    await db.execute(
        """UPDATE conversations
           SET title = ?, updated_at = ?
           WHERE id = ? AND user_sub = ?""",
        (title, datetime.utcnow().isoformat(), conversation_id, user_sub),
    )
    await db.commit()
    return await get_conversation(conversation_id, user_sub)


async def update_conversation_timestamp(session_key: str, user_sub: str):
    """Touch the updated_at timestamp for a conversation."""
    db = await get_db()
    await db.execute(
        """UPDATE conversations
           SET updated_at = ?
           WHERE session_key = ? AND user_sub = ?""",
        (datetime.utcnow().isoformat(), session_key, user_sub),
    )
    await db.commit()


async def delete_conversation(conversation_id: int, user_sub: str) -> bool:
    """Delete a conversation, scoped to the user. Returns True if deleted."""
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM conversations WHERE id = ? AND user_sub = ?",
        (conversation_id, user_sub),
    )
    await db.commit()
    return cursor.rowcount > 0
