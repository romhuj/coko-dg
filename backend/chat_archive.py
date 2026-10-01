"""Private, transactional conversation history. Stored receipts are display data, never commands."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


class ConversationListChanged(Exception):
    """A pin mutation invalidated the ordering of a previously opened page."""


class ChatArchive:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=2, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    context TEXT NOT NULL DEFAULT '{}', pinned INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL, role TEXT NOT NULL,
                    body TEXT NOT NULL, created_at INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                    UNIQUE(conversation_id,turn_id,role)
                );
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id,seq);
                CREATE TABLE IF NOT EXISTS archived_turns (
                    conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    PRIMARY KEY(conversation_id,turn_id),
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );
                INSERT OR IGNORE INTO archived_turns SELECT DISTINCT conversation_id,turn_id FROM messages;
                CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(conversations)")}
            if "pinned" not in columns:
                self.db.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        with self._lock:
            active = self.db.execute("SELECT value FROM archive_meta WHERE key='active'").fetchone()
            self.active_id = active[0] if active and self._exists(active[0]) else self.create()["conversation_id"]

    def _exists(self, conversation_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone() is not None

    def _require(self, conversation_id: str):
        if not isinstance(conversation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", conversation_id) or not self._exists(conversation_id):
            raise KeyError("聊天不存在")

    def _create_record(self) -> str:
        """Caller owns the transaction so replacing a deleted active chat is atomic."""
        conversation_id = uuid.uuid4().hex
        now = time.time_ns() // 1_000_000
        self.db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                        (conversation_id, "新聊天", now, now))
        self.db.execute("INSERT OR REPLACE INTO archive_meta VALUES('active',?)", (conversation_id,))
        return conversation_id

    def create(self) -> dict:
        with self._lock:
            with self.db:
                conversation_id = self._create_record()
            self.active_id = conversation_id
            return self.messages(conversation_id)

    def select(self, conversation_id: str) -> dict:
        with self._lock:
            with self.db:
                self._require(conversation_id)
                self.db.execute("INSERT OR REPLACE INTO archive_meta VALUES('active',?)", (conversation_id,))
            self.active_id = conversation_id
            return self.messages(conversation_id)

    def _list_revision(self) -> int:
        row = self.db.execute("SELECT value FROM archive_meta WHERE key='list_revision'").fetchone()
        return int(row[0]) if row else 0

    def set_pinned(self, conversation_id: str, pinned: bool) -> dict:
        if type(pinned) is not bool:
            raise ValueError("pinned 必须是布尔值")
        with self._lock, self.db:
            self._require(conversation_id)
            previous = bool(self.db.execute("SELECT pinned FROM conversations WHERE id=?", (conversation_id,)).fetchone()[0])
            if previous != pinned:
                self.db.execute("UPDATE conversations SET pinned=? WHERE id=?", (int(pinned), conversation_id))
                self.db.execute("INSERT OR REPLACE INTO archive_meta VALUES('list_revision',?)", (str(self._list_revision() + 1),))
            return {"conversation_id": conversation_id, "pinned": pinned, "active_id": self.active_id}

    def delete(self, conversation_id: str) -> dict:
        with self._lock:
            with self.db:
                self._require(conversation_id)
                active_changed = conversation_id == self.active_id
                self.db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
                self.db.execute("DELETE FROM archived_turns WHERE conversation_id=?", (conversation_id,))
                self.db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
                next_active = self._create_record() if active_changed else self.active_id
            # A failed delete or replacement leaves both the stored and live selection intact.
            self.active_id = next_active
            page = {"conversation_id": next_active, "title": "新聊天", "messages": [], "has_more": False} if active_changed else None
            return {"deleted_id": conversation_id, "active_id": next_active,
                    "active_changed": active_changed, "conversation": page}

    def list(self, *, before: str | None = None, limit: int = 100) -> dict:
        limit = max(1, min(100, int(limit)))
        with self._lock:
            revision = self._list_revision()
            params: list = []
            where = ""
            if before:
                # Freeze the sort boundary in the cursor; its conversation may receive new
                # messages between page requests and move to the top of the list.
                parts = before.split(":")
                if len(parts) == 5 and parts[0] == "v2" and parts[1].isdigit() and parts[2] in ("0", "1") and parts[3].isdigit() and re.fullmatch(r"[a-f0-9]{32}", parts[4]):
                    if len(parts[3]) > 19:
                        raise KeyError("聊天位置无效")
                    if len(parts[1]) > 19 or int(parts[1]) != revision:
                        raise ConversationListChanged("聊天列表已更新，请重新加载")
                    pinned, timestamp, identity = int(parts[2]), int(parts[3]), parts[4]
                elif len(parts) == 2 and parts[0].isdigit() and re.fullmatch(r"[a-f0-9]{32}", parts[1]):
                    if revision:
                        raise ConversationListChanged("聊天列表已更新，请重新加载")
                    if len(parts[0]) > 19:
                        raise KeyError("聊天位置无效")
                    timestamp, identity = int(parts[0]), parts[1]
                    row = self.db.execute("SELECT pinned FROM conversations WHERE id=?", (identity,)).fetchone()
                    pinned = int(row[0]) if row else 0
                else:
                    if revision and re.fullmatch(r"[a-f0-9]{32}", before):
                        raise ConversationListChanged("聊天列表已更新，请重新加载")
                    self._require(before)
                    timestamp, pinned = self.db.execute("SELECT updated_at,pinned FROM conversations WHERE id=?", (before,)).fetchone()
                    identity = before
                if timestamp > 2**63 - 1:
                    raise KeyError("聊天位置无效")
                where = "WHERE (c.pinned < ? OR (c.pinned = ? AND (c.updated_at < ? OR (c.updated_at = ? AND c.id < ?))))"
                params.extend((pinned, pinned, timestamp, timestamp, identity))
            rows = self.db.execute(f"""SELECT c.id,c.title,c.created_at,c.updated_at,c.pinned,
                (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count
                FROM conversations c {where} ORDER BY c.pinned DESC,c.updated_at DESC,c.id DESC LIMIT ?""", (*params, limit + 1)).fetchall()
            values = [{**dict(row), "pinned": bool(row["pinned"])} for row in rows[:limit]]
            cursor = f"v2:{revision}:{int(values[-1]['pinned'])}:{values[-1]['updated_at']}:{values[-1]['id']}" if len(rows) > limit else None
            return {"active_id": self.active_id, "conversations": values, "has_more": len(rows) > limit,
                    "next_cursor": cursor, "next_before": cursor}

    def messages(self, conversation_id: str, *, before: str | None = None, limit: int = 100) -> dict:
        limit = max(1, min(100, int(limit)))
        with self._lock:
            self._require(conversation_id)
            title = self.db.execute("SELECT title FROM conversations WHERE id=?", (conversation_id,)).fetchone()[0]
            params: list = [conversation_id]
            where = ""
            if before:
                marker = self.db.execute("SELECT seq FROM messages WHERE id=? AND conversation_id=?", (before, conversation_id)).fetchone()
                if marker is None:
                    raise KeyError("消息位置不存在")
                where = "AND seq < ?"
                params.append(marker[0])
            rows = self.db.execute(f"SELECT body FROM messages WHERE conversation_id=? {where} ORDER BY seq DESC LIMIT ?", (*params, limit + 1)).fetchall()
            values = [json.loads(row[0]) for row in reversed(rows[:limit])]
            return {"conversation_id": conversation_id, "title": title, "messages": values, "has_more": len(rows) > limit}

    def title(self, conversation_id: str) -> str:
        with self._lock:
            self._require(conversation_id)
            return self.db.execute("SELECT title FROM conversations WHERE id=?", (conversation_id,)).fetchone()[0]

    def append_turn(self, conversation_id: str, turn_id: str, user_text: str | None,
                    result: dict, *, history: list[dict], persona: str) -> list[dict]:
        """One transaction stores both messages, the exact receipts, and the next model context."""
        with self._lock, self.db:
            self._require(conversation_id)
            existing = self.db.execute("SELECT body FROM messages WHERE conversation_id=? AND turn_id=? ORDER BY seq", (conversation_id, turn_id)).fetchall()
            if self.db.execute("SELECT 1 FROM archived_turns WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)).fetchone():
                return [json.loads(row[0]) for row in existing]
            self.db.execute("INSERT INTO archived_turns VALUES(?,?)", (conversation_id, turn_id))
            now = time.time_ns() // 1_000_000
            values = []
            def add(role: str, text: str, **extra):
                value = {"id": uuid.uuid4().hex, "conversation_id": conversation_id,
                         "role": role, "text": text, "created_at": now, **extra}
                encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
                self.db.execute("INSERT INTO messages(id,conversation_id,turn_id,role,body,created_at) VALUES(?,?,?,?,?,?)",
                                (value["id"], conversation_id, turn_id, role, encoded, now))
                values.append(value)
            if user_text:
                add("user", user_text)
            line = str(result.get("line") or "")
            if line or result.get("executed") or result.get("dropped"):
                add("ai", line or "设备操作已处理。", speechText=line,
                    executed=result.get("executed") or [], dropped=result.get("dropped") or [])
            elif result.get("error"):
                add("sys", str(result["error"]))
            title = self.db.execute("SELECT title FROM conversations WHERE id=?", (conversation_id,)).fetchone()[0]
            if title == "新聊天" and user_text:
                title = re.sub(r"\s+", " ", user_text).strip()[:32] or title
            context = json.dumps({"persona": persona, "history": history}, ensure_ascii=False, allow_nan=False)
            self.db.execute("UPDATE conversations SET title=?,updated_at=?,context=? WHERE id=?", (title, now, context, conversation_id))
            return values

    def context(self, conversation_id: str, persona: str, keep: int) -> list[dict]:
        with self._lock:
            self._require(conversation_id)
            value = json.loads(self.db.execute("SELECT context FROM conversations WHERE id=?", (conversation_id,)).fetchone()[0])
            if value.get("persona") != persona:
                return []
            # Never pass archived command fields back into an executor.
            history = value.get("history", [])
            return [{"role": item["role"], "content": item["content"]} for item in history
                    if isinstance(item, dict) and item.get("role") in ("user", "assistant", "system") and isinstance(item.get("content"), str)][-max(1, keep):]

    def persona(self, conversation_id: str) -> str | None:
        with self._lock:
            self._require(conversation_id)
            value = json.loads(self.db.execute("SELECT context FROM conversations WHERE id=?", (conversation_id,)).fetchone()[0])
            return value.get("persona")

    def clear(self, conversation_id: str) -> None:
        with self._lock, self.db:
            self._require(conversation_id)
            self.db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
            self.db.execute("DELETE FROM archived_turns WHERE conversation_id=?", (conversation_id,))
            self.db.execute("UPDATE conversations SET title='新聊天',context='{}',updated_at=? WHERE id=?", (time.time_ns() // 1_000_000, conversation_id))

    def close(self):
        with self._lock:
            self.db.close()
