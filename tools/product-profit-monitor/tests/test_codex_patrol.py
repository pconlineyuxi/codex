import json
from datetime import date,datetime,timezone
import pytest
from bi_check_agent import codex_patrol as p, service


def demo_query(req,mode):
    assert mode=='live_test'
    result=service.query(req,'demo')
    result['evidence']['mode']='live_test'
    result['evidence']['complete']=False
    return result


def report():
    return p.build_report(date(2026,10,19),list(service.RULE_LABELS),query_fn=demo_query)


def test_date_is_previous_new_york_day_across_dst():
    assert p.previous_day(datetime(2026,10,21,2,tzinfo=timezone.utc))==date(2026,10,19)
    assert p.previous_day(datetime(2026,11,2,4,tzinfo=timezone.utc))==date(2026,10,31)
    with pytest.raises(ValueError):p.previous_day(datetime(2026,10,21))


def test_headless_report_all_rules_and_no_order_rows():
    r=report()
    assert len(r['rules'])==17
    assert r['status']=='partial_assessment'
    assert r['limitations'] and not r['evidence']['complete']
    assert {x['sku'] for x in r['finding_groups']}=={'DEMO-COST','DEMO-SHIP','DEMO-ADS'}
    assert 'order_id' not in {k for x in r['finding_groups'] for k in x}
    assert '_source_frames' not in json.dumps(r)
    assert any(x['unit']=='SKU/店铺/平台/日分组' for x in r['rules'])


def test_empty_is_not_successful_quality_check():
    def empty(req,mode):
        result=service.query(req.model_copy(update={'sku':['absent']}),'demo')
        return result
    r=p.build_report(date(2026,10,19),list(service.RULE_LABELS),query_fn=empty)
    assert r['status']=='empty_unverified'


def test_failure_keeps_last_query_success_and_sanitizes(tmp_path):
    good=p.persist(report(),tmp_path)
    before=(tmp_path/'review/last-successful.json').read_bytes()
    bad=p.failed_report(date(2026,10,20),'review',RuntimeError('postgres://secret-password'))
    p.persist(bad,tmp_path)
    assert (tmp_path/'review/last-successful.json').read_bytes()==before
    assert 'secret-password' not in json.dumps(bad)
    assert not json.loads((tmp_path/'review/latest.json').read_text())['query_succeeded']


def test_same_distribution_is_quiet_and_changed_counts_notify(tmp_path):
    r=p.persist(report(),tmp_path)
    assert r['changed_since_previous_run']
    same=report();same['window']['start']='2026-10-20'
    for row in same['finding_groups']:row['day']='2026-10-20'
    same=p.persist(same,tmp_path)
    assert not same['changed_since_previous_run']
    changed=report()
    next(x for x in changed['finding_groups'] if x['anomaly_rules']=='unit_shipping_fee_high')['规则命中样本数']+=1
    assert p.persist(changed,tmp_path)['changed_since_previous_run']


def test_maturity_dependent_hits_are_not_actionable_changes(tmp_path):
    r=p.persist(report(),tmp_path)
    pending={x['rule']:x for x in r['rules'] if x['assessment']=='maturity_unverified'}
    assert {'missing_product_cost','unit_cost_non_positive','zero_sales_with_units','ad_spend_without_sales'} <= pending.keys()
    assert all(x['pending_reason'] for x in pending.values())
    changed=report()
    for rule in changed['rules']:
        if rule['rule'] in pending:rule['hit_count']+=100
    for group in changed['finding_groups']:
        if group['anomaly_rules'] in pending:group['规则命中样本数']+=100
    assert not p.persist(changed,tmp_path)['changed_since_previous_run']
    assert p.maturity_reason('missing_product_cost','verified')


def test_ad_group_is_not_platform_count_and_dates_survive_mixed_grains():
    r=report()
    ads=r['advertising_observation']
    assert ads['group_count']==ads['platform_count']==ads['store_count']==1
    assert ads['no_matching_order_groups']==1
    assert ads['orders_present_zero_net_sales_groups']==0
    assert all(x['day']=='2026-10-19' for x in r['finding_groups'])
    text=p.markdown_report(r)
    assert '待核对到齐条件' in text
    assert '不是平台数' in text


def test_ads_with_order_but_zero_sales_are_separate_from_no_order():
    import pandas as pd
    rows=pd.DataFrame([
        {'type':'order','market_place':'Amazon','store':'US','sku':'HAS-ORDER','day':'2026-10-19'},
        {'type':'ad_daily','market_place':'Amazon','store':'US','sku':'NO-ORDER','day':'2026-10-19'}])
    groups=[{'market_place':'Amazon','store':'US','sku':sku,'day':'2026-10-19','anomaly_rules':'ad_spend_without_sales'} for sku in ['HAS-ORDER','NO-ORDER']]
    ads=p.advertising_observation(rows,groups)
    assert ads['group_count']==2 and ads['platform_count']==1
    assert ads['no_matching_order_groups']==ads['orders_present_zero_net_sales_groups']==1
