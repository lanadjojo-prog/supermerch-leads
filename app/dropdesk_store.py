from __future__ import annotations

import json
import re

from sqlalchemy import Column, MetaData, String, Table, Text, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sq_insert

from .database import engine

_SCHEMA = "dropship_engine" if engine.dialect.name == "postgresql" else None
_meta = MetaData(schema=_SCHEMA)
_records = Table(
    "records",
    _meta,
    Column("kind", String(32), primary_key=True),
    Column("id", String(128), primary_key=True),
    Column("payload", Text, nullable=False),
)
_valid_kind = re.compile(r"^[a-z0-9_]{1,32}$")
_valid_id = re.compile(r"^[A-Za-z0-9_.:@+-]{1,128}$")


def init_storage() -> None:
    with engine.begin() as conn:
        if _SCHEMA:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS dropship_engine"))
        _meta.create_all(conn)


def _check(kind: str, key: str | None = None) -> tuple[str, str | None]:
    kind = str(kind or "")
    if not _valid_kind.fullmatch(kind):
        raise ValueError("invalid kind")
    if key is not None:
        key = str(key or "")
        if not _valid_id.fullmatch(key):
            raise ValueError("invalid id")
    return kind, key


def get_record(kind: str, key: str):
    kind, key = _check(kind, key)
    with engine.connect() as conn:
        row = conn.execute(
            select(_records.c.payload).where(_records.c.kind == kind, _records.c.id == key)
        ).first()
    return json.loads(row[0]) if row else None


def all_records(kind: str):
    kind, _ = _check(kind)
    with engine.connect() as conn:
        rows = conn.execute(
            select(_records.c.payload).where(_records.c.kind == kind)
        ).all()
    return [json.loads(row[0]) for row in rows]


def apply_ops(ops: list[dict]) -> int:
    if not isinstance(ops, list) or len(ops) > 5000:
        raise ValueError("invalid operations")
    count = 0
    with engine.begin() as conn:
        for op in ops:
            action = str(op.get("action") or "")
            kind, key = _check(op.get("kind"), op.get("id"))
            if action == "put":
                payload = json.dumps(op.get("data"), ensure_ascii=False, separators=(",", ":"))
                if len(payload.encode("utf-8")) > 1_000_000:
                    raise ValueError("payload too large")
                stmt = (pg_insert if engine.dialect.name == "postgresql" else sq_insert)(_records).values(
                    kind=kind, id=key, payload=payload
                )
                conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["kind", "id"], set_={"payload": stmt.excluded.payload}
                    )
                )
            elif action == "delete":
                conn.execute(
                    _records.delete().where(_records.c.kind == kind, _records.c.id == key)
                )
            else:
                raise ValueError("invalid action")
            count += 1
    return count


def ping() -> bool:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
