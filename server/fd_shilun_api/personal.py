"""访问审计存储（auth.py 的依赖面）。

业务表由各 feature 模块自建（幂等 CREATE IF NOT EXISTS，同库共存）；
本模块只负责 auth 审计留痕（连接/迁移/audit）。
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS = [
    """
    CREATE TABLE audit_log (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        action TEXT NOT NULL,
        resource TEXT,
        outcome TEXT NOT NULL,
        detail TEXT,
        at TEXT NOT NULL
    );
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("FD_SHILUN_DB", "")
    if not path:
        path = str(Path.home() / ".shilun" / "app.db")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, ddl in enumerate(MIGRATIONS, start=1):
        if i > current:
            conn.executescript(ddl)
            conn.execute(f"PRAGMA user_version = {i}")
            conn.commit()
    return len(MIGRATIONS)


def audit(
    conn: sqlite3.Connection, user_id: str | None, action: str,
    resource: str | None, outcome: str, detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (id, user_id, action, resource, outcome, detail, at)"
        " VALUES (?,?,?,?,?,?,?)",
        (uuid.uuid4().hex[:12], user_id, action, resource, outcome, detail, now()),
    )
    conn.commit()
