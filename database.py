"""SQLite helpers for the Telegram support bot."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional


ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"


class Database:
    """Lightweight wrapper around sqlite3 for bot storage."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._initialise()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        finally:
            cur.close()

    def _initialise(self) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    twitter_handle TEXT,
                    last_post_date TEXT,
                    posts_today INTEGER DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    tweet_url TEXT NOT NULL,
                    message_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS supports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    supporter_id INTEGER NOT NULL,
                    post_id INTEGER NOT NULL,
                    proof TEXT,
                    created_at TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    verification_method TEXT,
                    UNIQUE (supporter_id, post_id),
                    FOREIGN KEY (supporter_id) REFERENCES users (telegram_id),
                    FOREIGN KEY (post_id) REFERENCES posts (id)
                )
            """
        )
        self._ensure_supports_columns()

    def _ensure_supports_columns(self) -> None:
        with self.cursor() as cur:
            cur.execute("PRAGMA table_info(supports)")
            columns = {row[1] for row in cur.fetchall()}
            if "verified" not in columns:
                cur.execute(
                    "ALTER TABLE supports ADD COLUMN verified INTEGER NOT NULL DEFAULT 0"
                )
            if "verification_method" not in columns:
                cur.execute(
                    "ALTER TABLE supports ADD COLUMN verification_method TEXT"
                )

    # -- User helpers -----------------------------------------------------
    def upsert_user(
        self, telegram_id: int, username: Optional[str], twitter_handle: Optional[str]
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_id, username, twitter_handle)
                VALUES (:telegram_id, :username, :twitter)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username=excluded.username,
                    twitter_handle=COALESCE(excluded.twitter_handle, users.twitter_handle)
                """,
                {
                    "telegram_id": telegram_id,
                    "username": username,
                    "twitter": twitter_handle,
                },
            )

    def update_twitter_handle(self, telegram_id: int, twitter_handle: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE users SET twitter_handle=:twitter WHERE telegram_id=:telegram",
                {"twitter": twitter_handle, "telegram": telegram_id},
            )

    def get_user(self, telegram_id: int) -> Optional[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE telegram_id=:telegram",
                {"telegram": telegram_id},
            )
            return cur.fetchone()

    def reset_daily_counter_if_needed(self, telegram_id: int, current_date: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "SELECT last_post_date FROM users WHERE telegram_id=:telegram",
                {"telegram": telegram_id},
            )
            row = cur.fetchone()
            if row is None:
                return
            if row["last_post_date"] != current_date:
                cur.execute(
                    """
                    UPDATE users
                    SET last_post_date=:date, posts_today=0
                    WHERE telegram_id=:telegram
                    """,
                    {"date": current_date, "telegram": telegram_id},
                )

    def increment_post_counter(self, telegram_id: int, current_date: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET posts_today = posts_today + 1, last_post_date=:date
                WHERE telegram_id=:telegram
                """,
                {"date": current_date, "telegram": telegram_id},
            )

    # -- Post helpers -----------------------------------------------------
    def record_post(
        self,
        telegram_id: int,
        tweet_url: str,
        message_id: Optional[int],
        created_at: datetime,
    ) -> int:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO posts (telegram_id, tweet_url, message_id, created_at)
                VALUES (:telegram, :url, :message_id, :created)
                """,
                {
                    "telegram": telegram_id,
                    "url": tweet_url,
                    "message_id": message_id,
                    "created": created_at.strftime(ISO_FORMAT),
                },
            )
            return int(cur.lastrowid)

    def get_recent_posts(
        self,
        limit: int,
        exclude_telegram_id: Optional[int] = None,
    ) -> List[sqlite3.Row]:
        query = [
            "SELECT id, telegram_id, tweet_url, created_at FROM posts",
        ]
        params = {}
        if exclude_telegram_id is not None:
            query.append("WHERE telegram_id != :exclude")
            params["exclude"] = exclude_telegram_id
        query.append("ORDER BY id DESC LIMIT :limit")
        params["limit"] = limit
        with self.cursor() as cur:
            cur.execute(" ".join(query), params)
            return list(cur.fetchall())

    def posts_supported_by(self, supporter_id: int) -> List[int]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT post_id FROM supports WHERE supporter_id=:supporter",
                {"supporter": supporter_id},
            )
            return [row[0] for row in cur.fetchall()]

    def record_support(
        self,
        supporter_id: int,
        post_id: int,
        proof: Optional[str],
        timestamp: datetime,
        *,
        verified: bool,
        verification_method: Optional[str],
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO supports (
                    supporter_id,
                    post_id,
                    proof,
                    created_at,
                    verified,
                    verification_method
                )
                VALUES (:supporter, :post, :proof, :created, :verified, :method)
                ON CONFLICT(supporter_id, post_id) DO UPDATE SET
                    proof=COALESCE(excluded.proof, supports.proof),
                    created_at=excluded.created,
                    verified=MAX(supports.verified, excluded.verified),
                    verification_method=COALESCE(excluded.method, supports.verification_method)
                """,
                {
                    "supporter": supporter_id,
                    "post": post_id,
                    "proof": proof,
                    "created": timestamp.strftime(ISO_FORMAT),
                    "verified": 1 if verified else 0,
                    "method": verification_method,
                },
            )

    def count_supports_for_post(self, post_id: int) -> int:
        with self.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM supports WHERE post_id=:post",
                {"post": post_id},
            )
            (count,) = cur.fetchone()
            return int(count)

    def get_post_by_url(self, tweet_url: str) -> Optional[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM posts WHERE tweet_url=:url ORDER BY id DESC LIMIT 1",
                {"url": tweet_url},
            )
            return cur.fetchone()

    def supports_needed(self, supporter_id: int, required: int) -> List[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT id, telegram_id, tweet_url, created_at
                FROM posts
                WHERE telegram_id != :supporter
                ORDER BY id DESC
                LIMIT :limit
                """,
                {"supporter": supporter_id, "limit": required * 4},
            )
            recent_posts = cur.fetchall()

        if not recent_posts:
            return []

        post_ids = [row["id"] for row in recent_posts]
        placeholders = ",".join("?" for _ in post_ids)

        if not placeholders:
            return []

        query = (
            "SELECT post_id FROM supports WHERE supporter_id=? AND post_id IN ("
            + placeholders
            + ")"
        )
        with self.cursor() as cur:
            cur.execute(query, [supporter_id, *post_ids])
            supported_ids = {row["post_id"] for row in cur.fetchall()}

        missing = [row for row in recent_posts if row["id"] not in supported_ids]
        return missing[:required]

    def posts_today(self, telegram_id: int, current_date: str) -> int:
        with self.cursor() as cur:
            cur.execute(
                "SELECT posts_today FROM users WHERE telegram_id=:telegram",
                {"telegram": telegram_id},
            )
            row = cur.fetchone()
            return int(row["posts_today"]) if row else 0
