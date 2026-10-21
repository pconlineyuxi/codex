from datetime import date, datetime, timezone
import json
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from bi_check_agent import lifecycle_patrol as p, codex_patrol, service

DAY = date(2026,10,20)
NOW = datetime(2026,10,21,14,tzinfo=timezone.utc)


def source_row(line, sku, **changes):
    row = dict(order_line_id=line,market_place='Amazon',store='US',sku=sku,main_ir=sku,
               gross_sales=100.,raw_promotion=0.,promo_cost=0.,sales=100.,units=1.,
               product_cost_unit=50.,hardware_cost=0.,removed_hardware=0.)
    row.update(changes)
    return row


def fixture(_):
    sales = pd.DataFrame([{'order_line_id':1,'created_at':'2026-10-20 15:00:00'}])
    cost = pd.DataFrame([
        {'order_id':10,'product_id':20,'line_matches':1,'order_line_id':2,'created_at':'2026-10-17 15:00:00'},
        {'order_id':11,'product_id':21,'line_matches':2,'order_line_id':3,'created_at':'2026-10-20 15:00:00'},
        {'order_id':12,'product_id':22,'line_matches':0,'order_line_id':None,'created_at':'2026-10-20 15:00:00'}])
    source = pd.DataFrame([
        source_row(1,'NEW-UNSHIPPED',sales=0.,product_cost_unit=0.),
        source_row(2,'OLD-SHIPPED',product_cost_unit=0.),
        source_row(3,'AMBIGUOUS',product_cost_unit=0.)])
    return sales,cost,source


def test_utc_boundaries_follow_new_york_dst():
    start,end=p.utc_bounds(date(2026,3,8))
    assert (end-start).total_seconds()==23*3600
    assert start.hour==5 and end.hour==4
    start,end=p.utc_bounds(date(2026,11,1))
    assert (end-start).total_seconds()==25*3600


def test_cost_only_for_uniquely_matched_shipped_product_includes_old_orders():
    r=p.review(DAY,list(service.RULE_LABELS),NOW,fixture)
    assert r['sales']['rule_counts']['zero_sales_with_units']==1
    assert r['cost']['rule_counts']['missing_product_cost']==1
    assert r['cost']['overlapping_cost_rule_lines']==1
    assert {g['sku'] for g in r['cost']['groups']}=={'OLD-SHIPPED'}
    assert r['cost']['created_before_outbound_day']==1
    assert r['cost']['ambiguous_order_products']==r['cost']['unmatched_order_products']==1
    assert r['status']=='partial_assessment' and not r['job_completion_verified']
    assert 'order_line_id' not in json.dumps(r) and 'order_id' not in json.dumps(r)


def test_cost_waits_until_next_morning_and_duplicate_source_not_counted():
    early=datetime(2026,10,21,8,59,tzinfo=ZoneInfo('America/New_York'))
    r=p.review(DAY,list(service.RULE_LABELS),early,fixture)
    assert r['cost']['not_yet_due_lines']==1
    assert r['cost']['rule_counts']['missing_product_cost']==0
    def duplicated(day):
        sales,cost,source=fixture(day)
        return sales,cost,pd.concat([source,source[source.order_line_id==2]])
    r=p.review(DAY,list(service.RULE_LABELS),NOW,duplicated)
    assert r['cost']['duplicate_source_lines']==1
    assert r['cost']['rule_counts']['missing_product_cost']==0
    with pytest.raises(ValueError):p.review(DAY,[],datetime(2026,10,21),fixture)


def test_fixed_main_ir_exclusions_still_apply_and_missing_source_is_unknown():
    def excluded(day):
        sales,cost,source=fixture(day)
        source.loc[source.order_line_id==1,'main_ir']='SHIPPING FEE'
        return sales,cost,source[source.order_line_id!=2]
    r=p.review(DAY,list(service.RULE_LABELS),NOW,excluded)
    assert r['sales']['excluded_fixed_filter_lines']==1
    assert r['sales']['rule_counts']['zero_sales_with_units']==0
    assert r['cost']['missing_source_lines']==1
    assert r['cost']['rule_counts']['missing_product_cost']==0


def test_lifecycle_failure_is_visible_without_exposing_driver_message():
    def query(req,mode):return service.query(req,'demo')
    def fail(day,rules):raise RuntimeError('postgres://secret-value')
    r=codex_patrol.build_report(DAY,list(service.RULE_LABELS),query_fn=query,lifecycle_fn=fail)
    assert r['lifecycle_review']['status']=='failed'
    assert r['status']=='partial_assessment'
    assert 'secret-value' not in json.dumps(r)
    assert '创建/出库时间复核未完成' in codex_patrol.markdown_report(r)


def test_truncation_is_not_accepted(monkeypatch):
    monkeypatch.setenv('MAX_RESULT_ROWS','1')
    with pytest.raises(RuntimeError,match='上限'):
        p.fetch_cohorts(DAY,lambda sql,params:pd.DataFrame([{'order_line_id':1},{'order_line_id':2}]))


def test_empty_cohorts_do_not_generate_hits():
    r=p.review(DAY,list(service.RULE_LABELS),NOW,lambda day:(pd.DataFrame(),pd.DataFrame(),pd.DataFrame(columns=['order_line_id'])))
    assert r['sales']['evaluated_lines']==r['cost']['evaluated_lines']==0
    assert not r['sales']['groups'] and not r['cost']['groups']
    assert not r['job_completion_verified']
    assert r['status']=='empty_unverified'


def test_cumulative_cap_is_checked_during_source_fetch(monkeypatch):
    monkeypatch.setenv('MAX_QUERY_TOTAL_ROWS','1')
    def query(sql,params):
        if 'SELECT sl.id' in sql:return pd.DataFrame({'order_line_id':[1,2]})
        if 'WITH events' in sql:return pd.DataFrame(columns=['line_matches','order_line_id'])
        return pd.DataFrame([source_row(1,'ONE'),source_row(2,'TWO')])
    with pytest.raises(RuntimeError,match='累计'):
        p.fetch_cohorts(DAY,query)
