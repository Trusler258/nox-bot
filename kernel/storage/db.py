"""SQLite 存储与任务账本。

M0 只建三张表：job（任务账本）、plugin_state、config_overrides。
写操作全部走单写锁，避免 "database is locked"。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS job (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    state         TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    run_after     REAL NOT NULL DEFAULT 0,
    lease_until   REAL NOT NULL DEFAULT 0,
    owner         TEXT NOT NULL DEFAULT '',
    idem_key      TEXT,
    checkpoint    TEXT,
    result        TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_job_idem ON job(idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_job_state ON job(state, run_after);
CREATE INDEX IF NOT EXISTS ix_job_lease ON job(lease_until);

CREATE TABLE IF NOT EXISTS plugin_state (
    plugin_id  TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    version    TEXT NOT NULL DEFAULT '',
    last_error TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS config_overrides (
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, key)
);
"""


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        # WAL + busy_timeout：多线程/多进程读写不再轻易 "database is locked"（§8.5）
        c = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript(_SCHEMA)

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(sql, args)
            return cur.rowcount

    def executemany(self, sql: str, seq) -> int:
        """批量写。`execute` 每次调用都要新开一条连接，几万行会慢到不可用
        （导入 V2 的 41MB `wdsj_history.json` 就踩到了）。"""
        with self._lock, self._conn() as c:
            cur = c.executemany(sql, seq)
            return cur.rowcount

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None


class JobLedger:
    """任务账本。语义：至少一次 + 幂等键；租约到期可被回收。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    def enqueue(self, type_: str, payload: dict | None = None, *,
                run_after: float = 0.0, idem_key: str | None = None,
                max_attempts: int = 3) -> str:
        now = time.time()
        job_id = uuid.uuid4().hex[:20]
        if idem_key:
            exist = self.store.one("SELECT id FROM job WHERE idem_key=?", (idem_key,))
            if exist:
                return exist["id"]
        self.store.execute(
            "INSERT INTO job(id, type, payload, state, run_after, idem_key,"
            " max_attempts, created_at, updated_at)"
            " VALUES(?,?,?,'pending',?,?,?,?,?)",
            (job_id, type_, json.dumps(payload or {}, ensure_ascii=False),
             run_after or now, idem_key, max_attempts, now, now))
        return job_id

    def claim(self, owner: str, lease_seconds: float, types: list[str] | None = None) -> dict | None:
        now = time.time()
        rows = self.store.query(
            "SELECT * FROM job WHERE state='pending' AND run_after<=?"
            " ORDER BY run_after LIMIT 1", (now,))
        if not rows:
            return None
        job = rows[0]
        if types and job["type"] not in types:
            return None
        changed = self.store.execute(
            "UPDATE job SET state='running', owner=?, lease_until=?,"
            " attempts=attempts+1, updated_at=? WHERE id=? AND state='pending'",
            (owner, now + lease_seconds, now, job["id"]))
        if changed != 1:
            return None
        job.update(state="running", owner=owner, lease_until=now + lease_seconds,
                   attempts=job["attempts"] + 1)
        return job

    def complete(self, job_id: str, result: str = "") -> None:
        self.store.execute(
            "UPDATE job SET state='done', result=?, lease_until=0, updated_at=? WHERE id=?",
            (result, time.time(), job_id))

    def fail(self, job_id: str, error: str, *, retry: bool = True) -> None:
        job = self.store.one("SELECT attempts, max_attempts FROM job WHERE id=?", (job_id,))
        if job is None:
            return
        if retry and job["attempts"] < job["max_attempts"]:
            backoff = min(60, 2 ** job["attempts"])
            self.store.execute(
                "UPDATE job SET state='pending', result=?, run_after=?, lease_until=0,"
                " updated_at=? WHERE id=?",
                (error[:500], time.time() + backoff, time.time(), job_id))
        else:
            self.store.execute(
                "UPDATE job SET state='dead', result=?, lease_until=0, updated_at=? WHERE id=?",
                (error[:500], time.time(), job_id))

    def recover_expired(self) -> int:
        """租约过期的 running 任务回收成 pending；超过重试上限的置 dead。"""
        now = time.time()
        expired = self.store.query(
            "SELECT id, attempts, max_attempts FROM job"
            " WHERE state='running' AND lease_until>0 AND lease_until<?", (now,))
        count = 0
        for job in expired:
            if job["attempts"] >= job["max_attempts"]:
                self.store.execute(
                    "UPDATE job SET state='dead', result='租约过期且重试耗尽',"
                    " lease_until=0, updated_at=? WHERE id=?", (now, job["id"]))
            else:
                self.store.execute(
                    "UPDATE job SET state='pending', result='租约过期回收',"
                    " lease_until=0, updated_at=? WHERE id=?", (now, job["id"]))
            count += 1
        return count

    def stats(self) -> dict[str, int]:
        rows = self.store.query("SELECT state, COUNT(*) c FROM job GROUP BY state")
        return {r["state"]: r["c"] for r in rows}
