from __future__ import annotations

import os
from typing import Any

import pandas as pd
from dotenv import load_dotenv

load_dotenv()


def get_engine():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None
    from sqlalchemy import create_engine
    return create_engine(database_url, pool_pre_ping=True)


def run_query(sql: str, params: dict[str, Any]) -> pd.DataFrame:
    engine = get_engine()
    if engine is None:
        raise RuntimeError("未配置 DATABASE_URL，真实查询未执行。请配置只读连接或选择演示模式。")
    from sqlalchemy import text
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            with conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text("SET LOCAL statement_timeout = '60s'"))
                conn.execute(text("SET LOCAL TIME ZONE 'America/New_York'"))
                return pd.read_sql(text(sql), conn, params=params)
    except Exception:
        raise RuntimeError("数据库查询未完成，请检查连接、只读权限、字段和查询时限。") from None
    finally:
        engine.dispose()
