from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from bi_check_agent import core, patrol_policy as policy, lifecycle_patrol as lifecycle, patrol_tracking as tracking, diagnostics, codex_patrol
from bi_check_agent.rulebook import render

NOW = datetime(2026,11,2,15,tzinfo=timezone.utc)
DAY = date(2026,10,19)


def row(**changes):
    return dict(order_line_id=10,order_number='SO-TEST-10',market_place='Amazon',store='US',sku='SKU',main_ir='SKU',
                type='order',date_order=str(DAY),created_at='2026-10-19 15:00:00',units=1.,sales=0.,
                product_cost_unit=0.,hardware_cost=0.,removed_hardware=0.,positive_scan_records=0,
                qty_delivered=0.,amz_fulfullment_order_status=None,cwzy_ss_order_status=None,
                order_state='sale',is_amazon_canceled=False,walmart_status=None,**changes)


def changed(**changes):
    result=row(); result.update(changes); return result


@pytest.mark.parametrize('change',[
    {'qty_delivered':1}, {'positive_scan_records':1}, {'amz_fulfullment_order_status':'Shipped'},
    {'cwzy_ss_order_status':'unknown-status'}, {'qty_delivered':None}, {'units':-1}, {'sales':10},
])
def test_stale_never_suppresses_delivery_or_missing_evidence(change):
    assert policy.decision('zero_sales_with_units',changed(**change),NOW) is None


def test_fourteen_days_boundary_and_no_cancellation_inference():
    assert policy.decision('zero_sales_with_units',row(),NOW-timedelta(days=1)) is None
    decision=policy.decision('zero_sales_with_units',row(),NOW)
    assert decision=={'state':'deferred','reason':'stale_unfulfilled'}
    missing=row();del missing['amz_fulfullment_order_status']
    assert policy.decision('zero_sales_with_units',missing,NOW) is None


def test_cancel_is_explicit_and_conflicting_fulfillment_stays_visible():
    assert policy.decision('zero_sales_with_units',changed(is_amazon_canceled=True),NOW)['state']=='exempt'
    assert policy.decision('zero_sales_with_units',changed(order_state='cancel',qty_delivered=1),NOW)['state']=='unknown'
    assert policy.decision('zero_sales_with_units',changed(is_buyer_requested_cancellation=True),NOW-timedelta(days=1)) is None


def bonsaii(**changes):
    return changed(market_place='Ebay',store='PCOnline US_EB',sku='BONSAII_Renew-C237-B-Black_ER1',**changes)


def test_zero_cost_exception_shared_with_core_but_null_negative_other_sku_not_hidden():
    rules=['missing_product_cost','unit_cost_non_positive','removed_hardware_abnormal']
    assert core.detect_anomalies(pd.DataFrame([bonsaii()]),rules).empty
    for change in ({'product_cost_unit':None},{'product_cost_unit':-1},{'removed_hardware':1}):
        assert not core.detect_anomalies(pd.DataFrame([bonsaii(**change)]),rules).empty
    assert not core.detect_anomalies(pd.DataFrame([changed(no_po=True)]),rules).empty


def test_new_public_order_numbers_and_deferred_lifecycle_match_snapshot():
    r=row();sales=pd.DataFrame([r]);cost=pd.DataFrame();source=pd.DataFrame([r])
    captured=[]
    result=lifecycle.review(DAY,['zero_sales_with_units'],NOW,lambda _:(sales,cost,source),captured.append)
    assert result['sales']['rule_counts']['zero_sales_with_units']==0
    assert result['sales']['applicability'][0]['state']=='deferred'
    assert captured[0]['observations'][0]['state']=='deferred'
    fulfilled=pd.DataFrame([changed(qty_delivered=1)])
    result=lifecycle.review(DAY,['zero_sales_with_units'],NOW,lambda _:(fulfilled,cost,fulfilled))
    assert result['sales']['groups'][0]['order_numbers']==['SO-TEST-10']
    serialized=json.dumps(result)
    assert all(k not in serialized for k in ('order_line_id','product_id','order_id'))


def test_confirmed_cancel_without_amount_source_is_exempt_not_missing_or_repaired():
    cancelled=pd.DataFrame([changed(order_state='cancel')]);captured=[]
    result=lifecycle.review(DAY,['zero_sales_with_units'],NOW,
        lambda _:(cancelled,pd.DataFrame(),pd.DataFrame(columns=['order_line_id'])),captured.append)
    assert result['sales']['missing_source_lines']==0
    assert captured[0]['observations'][0]['state']=='exempt'


def test_deferred_case_quiet_then_delivery_reopens_with_order_number(tmp_path):
    state={'schema_version':1,'cases':{},'imported_reports':[],'health':{}}
    def run(r,now):
        snap=tracking.collect_snapshot(DAY,now,pd.DataFrame([r]),pd.DataFrame(),pd.DataFrame([r]),['zero_sales_with_units'],[])
        report={'window':{'start':str(DAY)},'query_succeeded':True,'status':'observations',
                'lifecycle_review':{'status':'reviewed'},'diagnostics':{'source_status':'ok'}}
        tracking.review_history(report,state,tmp_path,[snap],now)
        return report
    initial=run(row(),NOW)
    assert initial['notification']['items'][0]['state']=='deferred'
    assert not run(row(),NOW+timedelta(days=10))['notification']['should_notify']
    reopened=run(changed(qty_delivered=1),NOW+timedelta(days=11))
    item=reopened['notification']['items'][0]
    assert item['notification_reason']=='reopened' and item['order_numbers']==['SO-TEST-10']
    assert 'SO-TEST-10' in '\n'.join(tracking.markdown(reopened['history_review'],reopened['notification']))


def test_legacy_zero_exception_is_auditable_and_stops_daily_reminder(tmp_path):
    state={'schema_version':1,'cases':{},'imported_reports':[],'health':{}}
    case=dict(event_day=str(DAY),cohort='cost',identity_kind='legacy_group',rule='unit_cost_non_positive',
              market_place='Ebay',store='PCOnline US_EB',sku='BONSAII_Renew-C237-B-Black_ER1',
              state='open',reason='legacy_group_hit',count=2)
    state['cases'][tracking.identity(case)]=tracking._new_case(case,(NOW-timedelta(days=10)).isoformat())
    cost=pd.DataFrame([{**bonsaii(), 'order_id':99,'product_id':88,'line_matches':1}])
    snap=tracking.collect_snapshot(DAY,NOW,pd.DataFrame(),cost,pd.DataFrame([bonsaii()]),[],['unit_cost_non_positive'])
    def run(now):
        report={'window':{'start':str(DAY)},'query_succeeded':True,'status':'observations',
                'lifecycle_review':{'status':'reviewed'},'diagnostics':{'source_status':'ok'}}
        tracking.review_history(report,state,tmp_path,[snap],now)
        return report
    report=run(NOW)
    item=report['notification']['items'][0]
    assert item['state']=='exempt' and item['order_reference_basis']=='current_group'
    assert item['order_numbers']==['SO-TEST-10']
    assert not run(NOW+timedelta(days=1))['notification']['should_notify']
    assert len(state['cases'])==1


def test_shared_diagnostics_defers_same_stale_order():
    r=row();source={**r,'source_cost':0.,'source_sales':0.,'raw_cost':0.,'result_cost':0.,'result_sales':0.,
                    'result_matches':1,'line_matches':1,'last_scan_at':None}
    decision=diagnostics.assess('zero_sales_with_units',r,source,NOW)
    assert decision['applicability_state']=='deferred'


def test_rulebook_is_complete_current_and_cli_needs_no_database(monkeypatch,capsys):
    monkeypatch.setattr(lifecycle,'fetch_cohorts',lambda *a:pytest.fail('must not query'))
    assert codex_patrol.main(['--list-rules'])==0
    output=capsys.readouterr().out
    for rule in policy.CONFIG['queryable_rules']: assert f'`{rule}`' in output
    assert (policy.ROOT/'docs/patrol-rules.md').read_text()==render()
