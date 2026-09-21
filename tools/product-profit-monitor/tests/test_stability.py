"""Regression coverage for the September functional audit findings."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from bi_check_agent import core, service, ui, profit_analysis, versioning
from bi_check_agent.models import AnomalyQueryRequest


def request(**kw):
    return AnomalyQueryRequest(start_date='2026-09-01',end_date='2026-09-03',**kw)


def test_raw_promotion_keeps_sign_while_cost_is_positive():
    req=request(anomaly_rules=['negative_promotion'])
    sql,_,_,_=core.build_order_line_sql(req)
    assert 'promotion ELSE 0 END AS raw_promotion' in sql
    assert 'ABS(promotion) ELSE 0 END AS promo_cost' in sql
    rows=service.demo_rows(req).iloc[:1].copy()
    rows['raw_promotion']=-10
    rows['promo_cost']=10
    assert len(core.detect_anomalies(rows,req.anomaly_rules))==1
    rows['raw_promotion']=10
    assert core.detect_anomalies(rows,req.anomaly_rules).empty
    rows['raw_promotion']=None
    assert service.rule_unknowns(rows,req.anomaly_rules)[0]['field']=='raw_promotion'


def test_changing_dates_and_platform_preserves_store(monkeypatch):
    monkeypatch.setattr(ui,'available_business_options',lambda *args:[{'market_place':'Amazon','store':'Store A'},{'market_place':'Walmart','store':'Store B'}])
    app=AppTest.from_string('''from datetime import date
import streamlit as st
from bi_check_agent import ui
d=st.date_input('date',date(2026,9,1))
f=ui.business_filters('live_test',d,date(2026,9,20),{})
st.json(f)
''').run()
    next(b for b in app.button if b.label=='加载当前日期范围内的平台和店铺选项').click().run()
    next(m for m in app.multiselect if m.label.startswith('店铺')).set_value(['Store A']).run()
    next(m for m in app.multiselect if m.label.startswith('平台')).set_value(['Walmart']).run()
    assert json.loads(app.json[0].value)['store']==['Store A']
    assert any('已保留筛选' in w.value for w in app.warning)
    app.date_input[0].set_value(date(2026,9,2)).run()
    assert json.loads(app.json[0].value)['store']==['Store A']
    assert not app.exception


def test_failed_requery_disables_chat_until_success(tmp_path,monkeypatch):
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(r for r in app.radio if r.label=='工作区').set_value('业务问题定位').run()
    run=lambda:next(b for b in app.button if b.label=='执行查询与检查').click().run()
    run()
    original=ui.query
    def fail(*a,**kw):raise RuntimeError('simulated failure')
    monkeypatch.setattr(ui,'query',fail)
    run()
    assert app.error and app.dataframe and app.chat_input[0].disabled
    assert any('上次成功的查询结果' in m.value for m in app.markdown)
    monkeypatch.setattr(ui,'query',original)
    run()
    assert not app.chat_input[0].disabled


def test_cumulative_limit_fails_without_partial_result(monkeypatch):
    rows=service.demo_rows(request()).iloc[:2].copy()
    monkeypatch.setattr(service.db,'run_query',lambda *a:rows)
    monkeypatch.setenv('MAX_QUERY_TOTAL_ROWS','3')
    with pytest.raises(RuntimeError,match='累计'):
        service.query(request(),mode='live_test')


def test_fingerprint_covers_code_and_filter_mapping(tmp_path,monkeypatch):
    root=tmp_path
    (root/'bi_check_agent').mkdir();(root/'config').mkdir()
    names=['config/diagnostic_rules.yaml','config/filter_mapping.yaml','bi_check_agent/core.py','bi_check_agent/service.py','bi_check_agent/db.py']
    for name in names:(root/name).write_text('initial')
    monkeypatch.setattr(versioning,'__file__',str(root/'bi_check_agent/versioning.py'))
    before=versioning.rule_source_version()
    for name in names:
        (root/name).write_text('changed')
        after=versioning.rule_source_version()
        assert before!=after
        before=after


def test_answer_rejects_truncation_and_invented_references():
    e={'source_analyses':[{'evidence_id':'source-answer1-1','periods':{}}]}
    profit_analysis.validate_answer('依据 [source-answer1-1]',e,'stop')
    for text,reason in [('依据 [source-answer1-1]','length'),('依据 [source-old-1]','stop'),('没有引用','stop')]:
        with pytest.raises(ValueError):profit_analysis.validate_answer(text,e,reason)
