"""任务持久化（SQLite）：任务、候选链接、产物。

单文件数据库，线程安全（Web UI 与 CLI 共用）。目的：
1. 断点续跑 —— 同一个 prompt 再跑一次会跳过已判定为失效/已下载过的链接；
2. 可追溯 —— 每个候选为什么被选中、下载到了哪里，都有记录。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

from . import util

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    prompt      TEXT NOT NULL,
    intent      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'new',
    stage       TEXT NOT NULL DEFAULT '',
    chosen_url  TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    paths       TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    url         TEXT NOT NULL,
    cloud       TEXT NOT NULL DEFAULT 'baidu',
    password    TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    published   TEXT NOT NULL DEFAULT '',
    alive       TEXT NOT NULL DEFAULT 'unknown',
    verdict     TEXT NOT NULL DEFAULT 'pending',
    score       REAL NOT NULL DEFAULT 0,
    reasons     TEXT NOT NULL DEFAULT '[]',
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    file_count  INTEGER NOT NULL DEFAULT 0,
    probe       TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'candidate',
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cand_url ON candidates(job_id, url);
CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_links (
    url_hash    TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    alive       TEXT NOT NULL DEFAULT 'unknown',
    checked_at  TEXT NOT NULL
);
"""


class State:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        util.ensure_dir(self.db_path.parent)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ 内部
    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            self._conn.commit()
            return cur

    def _query(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ 任务
    def create_job(self, prompt: str, intent: dict, paths: dict | None = None) -> dict:
        job_id = util.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        ts = util.stamp()
        self._exec(
            "INSERT INTO jobs (id,prompt,intent,status,stage,paths,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (job_id, prompt, json.dumps(intent, ensure_ascii=False), "new", "",
             json.dumps(paths or {}, ensure_ascii=False), ts, ts),
        )
        return self.get_job(job_id) or {}

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = util.stamp()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE id=?", [*fields.values(), job_id])

    def get_job(self, job_id: str) -> dict | None:
        rows = self._query("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._hydrate_job(rows[0]) if rows else None

    def recent_jobs(self, limit: int = 20) -> list[dict]:
        rows = self._query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._hydrate_job(r) for r in rows]

    @staticmethod
    def _hydrate_job(row: dict) -> dict:
        for key in ("intent", "paths"):
            try:
                row[key] = json.loads(row.get(key) or "{}")
            except json.JSONDecodeError:
                row[key] = {}
        return row

    # ------------------------------------------------------------ 候选
    def add_candidates(self, job_id: str, cands: list[dict]) -> int:
        added = 0
        ts = util.stamp()
        for cand in cands:
            try:
                self._exec(
                    "INSERT OR IGNORE INTO candidates"
                    " (id,job_id,url,cloud,password,title,note,source,published,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:16], job_id, cand["url"], cand.get("cloud", "baidu"),
                        cand.get("password", ""), cand.get("title", ""), cand.get("note", ""),
                        cand.get("source", ""), cand.get("published", ""), ts,
                    ),
                )
                added += 1
            except sqlite3.Error as exc:  # pragma: no cover
                util.log(f"写入候选失败 {cand.get('url')}: {exc}", "WARN")
        return added

    def list_candidates(self, job_id: str) -> list[dict]:
        rows = self._query(
            "SELECT * FROM candidates WHERE job_id=? ORDER BY score DESC, created_at ASC",
            (job_id,),
        )
        for row in rows:
            try:
                row["reasons"] = json.loads(row.get("reasons") or "[]")
            except json.JSONDecodeError:
                row["reasons"] = []
            try:
                row["probe"] = json.loads(row.get("probe") or "{}")
            except json.JSONDecodeError:
                row["probe"] = {}
        return rows

    def update_candidate(self, job_id: str, url: str, **fields: Any) -> None:
        if not fields:
            return
        for key in ("reasons", "probe"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE candidates SET {cols} WHERE job_id=? AND url=?",
                   [*fields.values(), job_id, url])

    def chosen(self, job_id: str) -> dict | None:
        rows = self._query(
            "SELECT * FROM candidates WHERE job_id=? AND state='chosen' LIMIT 1", (job_id,)
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------ 产物
    def add_asset(self, job_id: str, kind: str, path: str, size: int = 0, note: str = "") -> None:
        self._exec(
            "INSERT INTO assets (job_id,kind,path,size_bytes,note,created_at) VALUES (?,?,?,?,?,?)",
            (job_id, kind, str(path), int(size), note, util.stamp()),
        )

    def list_assets(self, job_id: str) -> list[dict]:
        return self._query("SELECT * FROM assets WHERE job_id=? ORDER BY id", (job_id,))

    # ------------------------------------------------------------ 全局去重
    def remember_link(self, url: str, alive: str) -> None:
        self._exec(
            "INSERT INTO seen_links (url_hash,url,alive,checked_at) VALUES (?,?,?,?)"
            " ON CONFLICT(url_hash) DO UPDATE SET alive=excluded.alive, checked_at=excluded.checked_at",
            (util.sha1(url), url, alive, util.stamp()),
        )

    def link_memory(self, url: str) -> str | None:
        rows = self._query("SELECT alive FROM seen_links WHERE url_hash=?", (util.sha1(url),))
        return rows[0]["alive"] if rows else None

    def stats(self) -> dict:
        jobs = self._query("SELECT COUNT(*) AS n FROM jobs")[0]["n"]
        cands = self._query("SELECT COUNT(*) AS n FROM candidates")[0]["n"]
        return {"jobs": jobs, "candidates": cands}
