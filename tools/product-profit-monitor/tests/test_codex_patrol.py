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
    changed=report();changed['finding_groups'][0]['规则命中样本数']+=1
    assert p.persist(changed,tmp_path)['changed_since_previous_run']
