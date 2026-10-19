"""Copy an offline SQLite database to an empty PostgreSQL database, atomically.

Use a SQLite backup made after stopping the application. Target URL is read from
AETHERSWAP_DATABASE_URL (never printed). Run before starting the target app.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, select, func, text, Boolean
from sqlmodel import SQLModel
from app import database, trading_models


def migrate(source_path, target_url):
    if not target_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("目标必须为 PostgreSQL")
    target_url = target_url.replace("postgresql://", "postgresql+psycopg://", 1)
    source = sqlite3.connect(f"file:{Path(source_path).resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("BEGIN")
    engine = create_engine(target_url)
    SQLModel.metadata.create_all(engine)
    tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    summary = {}
    try:
        with engine.begin() as target:
            for table in SQLModel.metadata.sorted_tables:
                if target.execute(select(func.count()).select_from(table)).scalar_one():
                    raise ValueError(f"目标表 {table.name} 非空，拒绝覆盖")
            for table in SQLModel.metadata.sorted_tables:
                if table.name not in tables:
                    continue
                source_columns = {r[1] for r in source.execute(f'PRAGMA table_info("{table.name}")')}
                fields = [c.name for c in table.columns if c.name in source_columns]
                columns = ", ".join('"'+f+'"' for f in fields)
                rows = [dict(r) for r in source.execute(f'SELECT {columns} FROM "{table.name}"')]
                for row in rows:
                    for col in table.columns:
                        if isinstance(col.type, Boolean) and row.get(col.name) is not None:
                            row[col.name] = bool(row[col.name])
                for start in range(0, len(rows), 500):
                    target.execute(table.insert(), rows[start:start+500])
                copied = target.execute(select(func.count()).select_from(table)).scalar_one()
                if copied != len(rows):
                    raise RuntimeError(f"{table.name} 数量校验失败")
                if rows:
                    # Verify all copied values, not only counts. Server defaults are excluded.
                    back = [dict(r._mapping) for r in target.execute(select(*(table.c[f] for f in fields)))]
                    def normalized(row):
                        return tuple((k, str(row[k]) if row[k] is not None else None) for k in fields)
                    if sorted(map(normalized, rows), key=str) != sorted(map(normalized, back), key=str):
                        raise RuntimeError(f"{table.name} 内容校验失败")
                if "id" in table.c and str(table.c.id.type) == "INTEGER":
                    target.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), COALESCE((SELECT MAX(id) FROM {table.name}), 1), EXISTS(SELECT 1 FROM {table.name}))"))
                summary[table.name] = copied
    finally:
        source.close()
        engine.dispose()
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="停机后生成的 SQLite 备份文件")
    args = parser.parse_args()
    import json
    print(json.dumps(migrate(args.source, os.environ["AETHERSWAP_DATABASE_URL"]), ensure_ascii=False))
