"""Alpha Hive Bot · 订阅者 SQLite 存储

状态机：
- whitelisted: 管理员加白名单但用户还没 /start
- active: 用户已 /start 且白名单（会收推送）
- unsubscribed: 用户主动 /unsubscribe（不推送，保留记录）
- revoked: 管理员 /revoke（不推送）

invite-only 流程：
  admin /invite <id> → DB.add_whitelist(id) → status=whitelisted
  user /start          → DB.activate_if_whitelisted(id, chat_id) → status=active
  user /unsubscribe    → DB.unsubscribe(id) → status=unsubscribed
  admin /revoke <id>   → DB.revoke(id) → status=revoked
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    user_id    INTEGER PRIMARY KEY,
    chat_id    INTEGER,
    username   TEXT,
    status     TEXT NOT NULL CHECK(status IN ('whitelisted','active','unsubscribed','revoked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers(status);
"""


class SubscriberDB:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # ── 管理员操作 ────────────────────────────────────
    def add_whitelist(self, user_id: int) -> bool:
        """加白名单。已存在则返回 False，新增返回 True。"""
        with self._conn() as c:
            row = c.execute("SELECT status FROM subscribers WHERE user_id=?", (user_id,)).fetchone()
            now = self._now()
            if row is None:
                c.execute(
                    "INSERT INTO subscribers (user_id, status, created_at, updated_at) "
                    "VALUES (?, 'whitelisted', ?, ?)",
                    (user_id, now, now),
                )
                return True
            # 已 revoked / unsubscribed 重新加白名单 → whitelisted
            if row["status"] in ("revoked", "unsubscribed"):
                c.execute(
                    "UPDATE subscribers SET status='whitelisted', updated_at=? WHERE user_id=?",
                    (now, user_id),
                )
                return True
            return False  # 已是 whitelisted 或 active

    def revoke(self, user_id: int) -> bool:
        with self._conn() as c:
            r = c.execute(
                "UPDATE subscribers SET status='revoked', updated_at=? WHERE user_id=?",
                (self._now(), user_id),
            )
            return r.rowcount > 0

    # ── 用户操作 ──────────────────────────────────────
    def activate_if_whitelisted(
        self, user_id: int, chat_id: int, username: Optional[str]
    ) -> str:
        """用户 /start 时调。
        返回 status: 'active'（激活成功）/ 'whitelisted'（已是 active）/ 'not_invited'（不在白名单）/ 'revoked'/'unsubscribed'
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT status FROM subscribers WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None:
                return "not_invited"
            now = self._now()
            if row["status"] == "whitelisted":
                c.execute(
                    "UPDATE subscribers SET status='active', chat_id=?, username=?, updated_at=? "
                    "WHERE user_id=?",
                    (chat_id, username, now, user_id),
                )
                return "active"
            if row["status"] == "active":
                # 已 active，更新 chat_id（用户可能换设备）
                c.execute(
                    "UPDATE subscribers SET chat_id=?, username=?, updated_at=? WHERE user_id=?",
                    (chat_id, username, now, user_id),
                )
                return "already_active"
            return row["status"]  # revoked / unsubscribed

    def unsubscribe(self, user_id: int) -> bool:
        with self._conn() as c:
            r = c.execute(
                "UPDATE subscribers SET status='unsubscribed', updated_at=? WHERE user_id=? "
                "AND status='active'",
                (self._now(), user_id),
            )
            return r.rowcount > 0

    # ── 查询 ──────────────────────────────────────────
    def get_status(self, user_id: int) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT status FROM subscribers WHERE user_id=?", (user_id,)).fetchone()
            return row["status"] if row else None

    def list_active_chat_ids(self) -> list[int]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT chat_id FROM subscribers WHERE status='active' AND chat_id IS NOT NULL"
            ).fetchall()
            return [r["chat_id"] for r in rows]

    def list_all(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT user_id, chat_id, username, status, created_at, updated_at "
                "FROM subscribers ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
