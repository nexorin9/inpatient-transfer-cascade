"""SQLite schema：表结构齐全、处置项字段完整、迁移函数连跑两次结果一致。"""
from __future__ import annotations

import re
from pathlib import Path

from itc.domain.models import DispositionItem
from itc.store import db
from itc.store.db import SCHEMA_VERSION, init_schema, insert_disposition, migrate

REQUIRED_TABLES = {
    "graph_runs", "dispositions", "external_blocks", "cache_entries", "cache_dirty",
    "writeback_registry", "pending_writebacks", "audit_events", "execution_events",
    "model_audits", "graph_idempotency", "import_drops", "schema_meta",
}

REQUIRED_DISPOSITION_COLUMNS = {
    "transfer_no", "inpatient_no", "order_no", "specimen_no", "kind", "action",
    "state", "judgement", "evidence_fields", "evidence_snippet", "reasoning_summary",
    "stop_draft_content", "execute_dept_code", "execute_dept_name", "effective_from",
    "idempotency_key", "confirmed_by", "confirmed_at", "audit_hash", "created_at", "updated_at",
}


def _objects(conn):
    return [(r["type"], r["name"], r["sql"]) for r in conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()]


def _tables(conn) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}


def _version_rows(conn) -> list[int]:
    return [r["version"] for r in conn.execute("SELECT version FROM schema_meta").fetchall()]


def test_required_tables_exist(db_conn):
    assert REQUIRED_TABLES <= _tables(db_conn)


def test_disposition_columns_complete(db_conn):
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(dispositions)").fetchall()}
    assert REQUIRED_DISPOSITION_COLUMNS <= cols


def test_writeback_registry_has_unique_key(db_conn):
    cols = {r["name"]: r for r in db_conn.execute(
        "PRAGMA table_info(writeback_registry)"
    ).fetchall()}
    assert {"idempotency_key", "status_code", "response_digest", "disposition_id", "created_at"} <= set(cols)
    assert cols["idempotency_key"]["pk"] == 1  # 主键即跨进程唯一约束


def test_migrate_twice_is_identical(db_conn):
    before = _objects(db_conn)
    v1 = migrate(db_conn)
    after_first = _objects(db_conn)
    v2 = migrate(db_conn)
    after_second = _objects(db_conn)
    assert v1 == v2 == SCHEMA_VERSION
    assert after_first == after_second == before
    assert _version_rows(db_conn) == [SCHEMA_VERSION]  # 不产生第二行版本记录


def test_init_schema_repeat_preserves_data(db_conn):
    item_id = insert_disposition(db_conn, DispositionItem(
        transfer_no="T-SCHEMA", inpatient_no="123", kind="order", ref="O-1",
    ))
    before = _objects(db_conn)
    assert init_schema(db_conn) == SCHEMA_VERSION
    assert init_schema(db_conn) == SCHEMA_VERSION
    assert _objects(db_conn) == before
    row = db_conn.execute("SELECT transfer_no FROM dispositions WHERE id=?", (item_id,)).fetchone()
    assert row["transfer_no"] == "T-SCHEMA"


def test_audit_tables_have_no_modification_entry():
    """写入层禁止改写 / 删除审计表：底座源码不含针对审计表的修改语句。"""
    source = Path(db.__file__).read_text(encoding="utf-8")
    for table in ("audit_events", "execution_events"):
        assert not re.search(rf"(?i)\bupdate\s+{table}\b", source)
        assert not re.search(rf"(?i)\bdelete\s+from\s+{table}\b", source)
    for name in ("update_audit", "delete_audit", "update_audit_event", "delete_audit_event"):
        assert not hasattr(db, name)


def test_schema_version_is_stable(db_conn):
    assert migrate(db_conn) == SCHEMA_VERSION
    assert _version_rows(db_conn) == [SCHEMA_VERSION]
