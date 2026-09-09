from datetime import date
import pandas as pd
import pytest
from bi_check_agent.models import AnomalyQueryRequest
from bi_check_agent import core, db


def req(**kw):
    return AnomalyQueryRequest(start_date=date(2026,9,8), end_date=date(2026,9,9), **kw)


def test_live_never_falls_back_to_mock(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    with pytest.raises(RuntimeError):
        db.run_query('select 1', {})


def test_unknown_filter_is_not_silently_ignored():
    with pytest.raises(ValueError):
        core.build_where_clause(req(unsupported_filters={'country':['US']}))


def test_null_cost_is_preserved():
    d=core._ensure_numeric(pd.DataFrame([{'product_cost_unit':None,'hardware_cost':0,'removed_hardware':0,'units':2}]))
    assert pd.isna(d.iloc[0]['product_cost_unit'])


def test_raw_query_never_sums_unit_costs():
    sql,params,_,_=core.build_order_line_sql(req())
    assert 'SUM(' not in sql
    assert params['max_rows'] == 50001


def test_demo_query_obeys_dates_and_sku():
    from bi_check_agent.service import query
    result=query(req(sku=['DEMO-COST']), 'demo')
    assert set(result['rows']['sku']) == {'DEMO-COST'}
    assert result['evidence']['complete'] is True


def test_zero_denominator_not_zero_rate():
    from bi_check_agent.service import query
    rows=query(req(sku=['DEMO-ADS']), 'demo')['rows']
    agg=core.aggregate_product(rows,['sku'])
    assert pd.isna(agg.iloc[0]['Profit rate with Ads & Ship'])


def test_decomposition_reconciles():
    from bi_check_agent.service import query, decompose
    rows=query(req(sku=['DEMO-HEALTHY']), 'demo')['rows']
    report=decompose(rows, rows)
    assert report['difference'] == 0
    assert report['reconciliation_error'] == 0
    assert sum(x['contribution'] for x in report['components']) == 0


def test_ads_do_not_merge_stores():
    from bi_check_agent.service import query
    rows=query(req(sku=['DEMO-ADS']), 'demo')['rows']
    a=core.detect_anomalies(rows,['ad_spend_without_sales'])
    assert len(a)==1
    assert 'store' in a.columns


def test_missing_cost_blocks_profit_explanation():
    from bi_check_agent.service import query,decompose
    rows=query(req(sku=['DEMO-COST']),'demo')['rows']
    with pytest.raises(ValueError, match='缺失'):
        decompose(rows,rows)


def test_promotion_guard_skips_non_amazon():
    from bi_check_agent.service import query
    rows=query(req(sku=['DEMO-HEALTHY']),'demo')['rows']
    rows['market_place']='Walmart'
    rows['promo_cost']=999
    assert core.detect_anomalies(rows,['promotion_ratio_high']).empty


def test_ads_stores_cannot_cancel_each_other():
    from bi_check_agent.service import query
    rows=query(req(sku=['DEMO-ADS']),'demo')['rows']
    other=rows.copy(); other['store']='OTHER';other['sales']=10000;other['ad_spend']=0
    findings=core.detect_anomalies(pd.concat([rows,other]),['ad_spend_without_sales'])
    assert len(findings)==1
    assert findings.iloc[0]['store']=='Demo US'


def test_live_limit_cannot_be_success(monkeypatch):
    from bi_check_agent import service
    monkeypatch.setattr(service,'_contract',lambda *a: {'currency':'USD'})
    monkeypatch.setenv('MAX_RESULT_ROWS','1')
    monkeypatch.setattr(db,'run_query',lambda *a: pd.DataFrame([{'x':1},{'x':2}]))
    with pytest.raises(RuntimeError,match='不完整'):service.query(req(),'live')


def test_refresh_changed_rejects_results(monkeypatch):
    from bi_check_agent import service
    versions=iter([{'snapshot_id':'a','currency':'USD'},{'snapshot_id':'b','currency':'USD'}])
    monkeypatch.setattr(service,'_contract',lambda *a: next(versions))
    monkeypatch.setattr(db,'run_query',lambda *a: pd.DataFrame([{'x':1}]))
    with pytest.raises(RuntimeError,match='变化'):service.query(req(),'live')


def test_real_ready_contract_missing_and_expired(tmp_path,monkeypatch):
    from bi_check_agent.service import _contract
    monkeypatch.setenv('PROFIT_LIVE_CONTRACT_CONFIRMED','yes')
    monkeypatch.setenv('PROFIT_READY_FILE',str(tmp_path/'no.json'))
    with pytest.raises(RuntimeError,match='就绪'):_contract(date(2026,9,8),date(2026,9,9))


def test_money_comparison_exact_contributions():
    from bi_check_agent.service import query,decompose
    a=query(req(sku=['DEMO-HEALTHY']),'demo')['rows']
    b=a.copy(); b['gross_sales']+=100; b['order_cost_total']+=20; b['shipping_fee']+=5
    result=decompose(a,b)
    assert result['difference']==75
    assert result['reconciliation_error']==0
    assert [x['contribution'] for x in result['components']]==[100,0,-20,0,0,-5]


def test_nan_sales_is_not_nonzero_sales():
    from bi_check_agent.service import query
    rows=query(req(sku=['DEMO-HEALTHY']),'demo')['rows']
    rows['sales']=float('nan');rows['units']=0
    assert core.detect_anomalies(rows,['zero_units_with_sales']).empty


def test_impact_threshold_and_worsening_evidence():
    from bi_check_agent.service import evaluate_plan
    result=evaluate_plan({'mode':'demo','rules':['shipping_cost_ratio_high'],'worsening_deltas':{'shipping_cost_ratio_high':0.1}},'2026-09-08','2026-09-09')
    f=result['findings'][0]
    assert f['value']>0.5
    assert f['worsening_delta']==0.1
    assert f['evidence']['threshold']==0.5
    assert f['evidence']['affected_sales']>0
    assert f['evidence']['query_evidence']['complete'] is True


def test_missing_units_not_aggregated_to_zero():
    from bi_check_agent.service import query
    rows=query(req(sku=['DEMO-HEALTHY']),'demo')['rows']
    rows['units']=float('nan')
    assert pd.isna(core.aggregate_product(rows,['sku']).iloc[0]['Units'])


def test_yesterday_parser_and_boolean_filter(monkeypatch):
    from bi_check_agent import parser_rules
    from bi_check_agent.ai_parser import parse_business_description
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    monkeypatch.setattr(parser_rules,'today_est',lambda:date(2026,9,9))
    result=parse_business_description('查昨天 SKU DEMO-COST 的产品成本缺失数据 has_product_cost=false')
    assert result['start_date']=='2026-09-08'
    assert result['end_date']=='2026-09-09'
    assert result['has_product_cost']==[False]
    assert result['sku']==['DEMO-COST']


def test_invalid_date_is_not_silently_clamped():
    from bi_check_agent.parser_rules import parse_time_range
    with pytest.raises(ValueError):parse_time_range('2026-02-31 到 2026-03-01')


def test_mixed_line_and_aggregate_rules_have_valid_metric(monkeypatch):
    from bi_check_agent import service
    original=service.demo_rows
    def mixed(r):
        rows=original(r);rows.loc[rows.sku=='DEMO-HEALTHY','ad_spend']=200.
        return rows
    monkeypatch.setattr(service,'demo_rows',mixed)
    result=service.evaluate_plan({'mode':'demo','rules':['missing_product_cost','ad_spend_ratio_high']},'2026-09-08','2026-09-09')
    ad=next(f for f in result['findings'] if f['rule']=='ad_spend_ratio_high')
    assert ad['value']>0.1
    assert ad['evidence']['affected_sales']>0


def test_null_marker_never_drops_sku_filter():
    with pytest.raises(ValueError,match='NULL'):
        core.build_where_clause(req(sku=['NULL']))


def test_missing_rule_operand_never_recovers(tmp_path,monkeypatch):
    from bi_check_agent import service
    from bi_check_agent.monitor import MonitorStore
    store=MonitorStore(tmp_path/'state.sqlite3')
    plan=store.save_plan({'id':'null','mode':'demo','rules':['shipping_cost_ratio_high']})
    first=store.run(plan,'2026-09-08','2026-09-09',service.evaluate_plan)
    assert first['status']=='succeeded'
    original=service.demo_rows
    def missing(r):
        d=original(r);d.loc[d.sku=='DEMO-SHIP','shipping_fee']=float('nan');return d
    monkeypatch.setattr(service,'demo_rows',missing)
    second=store.run(plan,'2026-09-08','2026-09-09',service.evaluate_plan)
    assert second['status']=='failed'
    assert store.incidents()[0]['status']=='open'


def test_live_testing_is_bounded_and_not_monitor_evidence(monkeypatch):
    from bi_check_agent import service
    with pytest.raises(ValueError,match='店铺'):service.query(req(),'live_test')
    monkeypatch.setattr(db,'run_query',lambda *a:service.demo_rows(req(sku=['DEMO-HEALTHY'])))
    monkeypatch.setattr(service,'_contract',lambda *a: (_ for _ in ()).throw(AssertionError('must not fabricate readiness')))
    result=service.query(req(store=['Test Store']),'live_test')
    assert result['evidence']['query_complete'] is True
    assert result['evidence']['complete'] is False
    assert result['evidence']['snapshot'] is None
    with pytest.raises(ValueError,match='不能用于'):
        service.evaluate_plan({'mode':'live_test','rules':['missing_product_cost']},'2026-09-08','2026-09-09')


def test_full_leap_year_query_limit(monkeypatch):
    from datetime import timedelta
    from bi_check_agent import service
    monkeypatch.delenv('MAX_QUERY_DAYS', raising=False)
    request = AnomalyQueryRequest(start_date=date(2024,1,1), end_date=date(2025,1,1), store=['Test Store'])
    core.build_where_clause(request)
    # Reach the reader beyond the former seven-day live-test gate, without
    # actually reading a year of production data.
    class ReaderReached(Exception): pass
    def reader(*args): raise ReaderReached()
    monkeypatch.setattr(db, 'run_query', reader)
    with pytest.raises(ReaderReached): service.query(request, 'live_test')
    too_long = request.model_copy(update={'end_date':request.end_date+timedelta(days=1)})
    with pytest.raises(ValueError, match='366'): service.query(too_long, 'live_test')
