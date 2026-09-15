from datetime import date
from bi_check_agent import core, service
from bi_check_agent.models import AnomalyQueryRequest


def test_fixed_exclusions_cannot_be_overridden():
    for filters in [{},{'main_ir':['SHIPMENT DISCOUNT']}]:
        req=AnomalyQueryRequest(start_date=date(2026,9,7),end_date=date(2026,9,8),**filters)
        sql,params,applied,_=core.build_order_line_sql(req)
        assert core.FIXED_MAIN_IR_SQL in sql
        assert sql.index(core.FIXED_MAIN_IR_SQL)<sql.index("type = 'order'",sql.index("WHERE"))
        assert any(core.FIXED_MAIN_IR_SQL in item for item in applied)


def test_option_loading_uses_same_fixed_scope(monkeypatch):
    import pandas as pd
    calls=[]
    def query(sql,params):
        calls.append(sql)
        return pd.DataFrame(columns=['market_place','store'])
    monkeypatch.setattr(service.db,'run_query',query)
    assert service.available_business_options(date(2026,9,7),date(2026,9,8))==[]
    assert core.FIXED_MAIN_IR_SQL in calls[0]
