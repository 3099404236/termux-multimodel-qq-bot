from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class MemoryMessage:
    id: int
    role: str
    sender_id: str
    sender_name: str
    content: str
    created_at: str


@dataclass(frozen=True)
class SummaryBatch:
    start_message_id: int
    end_message_id: int
    messages: tuple[MemoryMessage, ...]


class GroupMemoryStore:
    def __init__(self, database_path: Path | None = None) -> None:
        if database_path is None:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            database_path = (
                Path(get_astrbot_data_path()) / "group_memory" / "group_memory.db"
            )
        self.database_path = database_path.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS group_memory_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    umo TEXT NOT NULL,
                    role TEXT NOT NULL,
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_group_memory_messages_umo_id
                    ON group_memory_messages(umo, id);
                CREATE INDEX IF NOT EXISTS idx_group_memory_messages_created
                    ON group_memory_messages(umo, created_at);

                CREATE TABLE IF NOT EXISTS group_memory_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    umo TEXT NOT NULL,
                    start_message_id INTEGER NOT NULL,
                    end_message_id INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(umo, start_message_id, end_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_group_memory_summaries_umo_id
                    ON group_memory_summaries(umo, id);

                CREATE TABLE IF NOT EXISTS group_memory_cursors (
                    umo TEXT PRIMARY KEY,
                    last_summarized_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def record_message(
        self,
        *,
        umo: str,
        role: str,
        sender_id: str,
        sender_name: str,
        content: str,
        created_at: datetime | None = None,
    ) -> int:
        timestamp = (created_at or datetime.now()).isoformat(timespec="seconds")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO group_memory_messages(
                    umo, role, sender_id, sender_name, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (umo, role, sender_id, sender_name, content, timestamp),
            )
            return int(cursor.lastrowid)

    def _last_summarized_message_id(
        self, connection: sqlite3.Connection, umo: str
    ) -> int:
        row = connection.execute(
            """
            SELECT last_summarized_message_id
            FROM group_memory_cursors
            WHERE umo = ?
            """,
            (umo,),
        ).fetchone()
        return int(row[0]) if row else 0

    def next_summary_batch(self, umo: str, batch_size: int) -> SummaryBatch | None:
        with self._lock, self._connect() as connection:
            cursor_id = self._last_summarized_message_id(connection, umo)
            rows = connection.execute(
                """
                SELECT id, role, sender_id, sender_name, content, created_at
                FROM group_memory_messages
                WHERE umo = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (umo, cursor_id, max(batch_size, 1)),
            ).fetchall()
        if len(rows) < batch_size:
            return None
        messages = tuple(self._message_from_row(row) for row in rows)
        return SummaryBatch(
            start_message_id=messages[0].id,
            end_message_id=messages[-1].id,
            messages=messages,
        )

    def save_summary(self, umo: str, batch: SummaryBatch, content: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO group_memory_summaries(
                    umo, start_message_id, end_message_id,
                    message_count, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    umo,
                    batch.start_message_id,
                    batch.end_message_id,
                    len(batch.messages),
                    content,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO group_memory_cursors(
                    umo, last_summarized_message_id, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(umo) DO UPDATE SET
                    last_summarized_message_id = excluded.last_summarized_message_id,
                    updated_at = excluded.updated_at
                """,
                (umo, batch.end_message_id, now),
            )

    def render_context(
        self,
        umo: str,
        *,
        recent_limit: int,
        summary_limit: int,
        exclude_message_id: int | None = None,
    ) -> str:
        with self._lock, self._connect() as connection:
            cursor_id = self._last_summarized_message_id(connection, umo)
            summaries = connection.execute(
                """
                SELECT start_message_id, end_message_id, content, created_at
                FROM group_memory_summaries
                WHERE umo = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (umo, max(summary_limit, 0)),
            ).fetchall()
            parameters: list[object] = [umo, cursor_id]
            exclude_clause = ""
            if exclude_message_id is not None:
                exclude_clause = " AND id != ?"
                parameters.append(exclude_message_id)
            parameters.append(max(recent_limit, 1))
            messages = connection.execute(
                f"""
                SELECT id, role, sender_id, sender_name, content, created_at
                FROM group_memory_messages
                WHERE umo = ? AND id > ?{exclude_clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

        sections: list[str] = []
        if summaries:
            summary_text = "\n\n".join(
                (
                    f"[事件摘要 #{row['start_message_id']}-#{row['end_message_id']}]\n"
                    f"{row['content']}"
                )
                for row in reversed(summaries)
            )
            sections.append("较早的结构化群聊记忆：\n" + summary_text)
        if messages:
            recent_text = "\n---\n".join(row["content"] for row in reversed(messages))
            sections.append("尚未压缩的最近原文：\n" + recent_text)
        return "\n\n".join(sections)

    def search(
        self,
        umo: str,
        *,
        query: str,
        limit: int,
        hours: int,
    ) -> str:
        limit = min(max(limit, 1), 12)
        normalized_query = query.strip()
        cutoff = None
        if hours > 0:
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(
                timespec="seconds"
            )

        message_rows = self._search_messages(
            umo,
            query=normalized_query,
            limit=limit,
            cutoff=cutoff,
        )
        summary_rows = self._search_summaries(
            umo,
            query=normalized_query,
            limit=max(min(limit // 2, 4), 1),
            cutoff=cutoff,
        )

        sections: list[str] = []
        if summary_rows:
            sections.append(
                "匹配的事件摘要：\n"
                + "\n\n".join(
                    f"[摘要 #{row['start_message_id']}-#{row['end_message_id']}]\n"
                    f"{row['content']}"
                    for row in summary_rows
                )
            )
        if message_rows:
            sections.append(
                "匹配的原始消息（可作为回答证据）：\n"
                + "\n".join(
                    f"[消息 #{row['id']} | {row['created_at']}] {row['content']}"
                    for row in message_rows
                )
            )
        if not sections:
            return "当前群聊记忆中没有找到匹配记录。"
        return "\n\n".join(sections)

    def _search_messages(
        self,
        umo: str,
        *,
        query: str,
        limit: int,
        cutoff: str | None,
    ) -> list[sqlite3.Row]:
        clauses = ["umo = ?"]
        parameters: list[object] = [umo]
        if cutoff:
            clauses.append("created_at >= ?")
            parameters.append(cutoff)
        for term in self._query_terms(query):
            clauses.append("(content LIKE ? OR sender_name LIKE ?)")
            pattern = f"%{term}%"
            parameters.extend([pattern, pattern])
        parameters.append(limit)
        with self._lock, self._connect() as connection:
            return connection.execute(
                f"""
                SELECT id, role, sender_id, sender_name, content, created_at
                FROM group_memory_messages
                WHERE {" AND ".join(clauses)}
                ORDER BY id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

    def _search_summaries(
        self,
        umo: str,
        *,
        query: str,
        limit: int,
        cutoff: str | None,
    ) -> list[sqlite3.Row]:
        clauses = ["umo = ?"]
        parameters: list[object] = [umo]
        if cutoff:
            clauses.append("created_at >= ?")
            parameters.append(cutoff)
        for term in self._query_terms(query):
            clauses.append("content LIKE ?")
            parameters.append(f"%{term}%")
        parameters.append(limit)
        with self._lock, self._connect() as connection:
            return connection.execute(
                f"""
                SELECT start_message_id, end_message_id, content, created_at
                FROM group_memory_summaries
                WHERE {" AND ".join(clauses)}
                ORDER BY id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        if not query:
            return []
        terms = re.findall(r"[\w\u3400-\u9fff]+", query, flags=re.UNICODE)
        return terms[:5] or [query]

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> MemoryMessage:
        return MemoryMessage(
            id=int(row["id"]),
            role=str(row["role"]),
            sender_id=str(row["sender_id"]),
            sender_name=str(row["sender_name"]),
            content=str(row["content"]),
            created_at=str(row["created_at"]),
        )


def build_summary_prompt(batch: SummaryBatch) -> str:
    transcript = "\n".join(
        f"[消息 #{message.id} | {message.created_at}] {message.content}"
        for message in batch.messages
    )
    schema = {
        "topics": ["正在讨论的主题"],
        "facts_and_decisions": ["带人物和消息编号的事实、决定、数字、链接"],
        "participants_and_stances": ["参与者及明确表达的立场或偏好"],
        "unresolved_questions": ["未解决的问题或争议"],
        "resources": ["原文中的链接、文件、产品名、代码名"],
        "retrieval_keywords": ["便于以后搜索的关键词和别名"],
    }
    return (
        "请把下面这批 QQ 群聊压缩成可检索的结构化事件记忆。\n"
        "只允许写原文明确支持的信息，不推测动机，不补全缺失事实。\n"
        "保留否定、时间顺序、人物归属、数字、链接、项目名和关键原话。\n"
        "闲聊可以省略；发生冲突时并列记录不同说法，不替任何一方裁决。\n"
        "每条事实尽量附上对应消息编号。字段没有内容时返回空数组。\n"
        "只输出一个 JSON 对象，不要 Markdown 代码块。结构如下：\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"群聊原文：\n{transcript}"
    )
