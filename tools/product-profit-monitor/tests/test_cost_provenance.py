from copy import deepcopy
from datetime import date, datetime, timezone
import json
import pandas as pd
import pytest
from bi_check_agent import cost_provenance as p, patrol_policy, lifecycle_patrol, patrol_tracking, diagnostics


def context():
    return dict(order_line_id=10,actual_ir='GIFT',purchase_price=0.,fallback_purchase_price=0.,fallback_matches=1,
                tracking_method='upc',tracking='none',is_distribution=False,raw_cost=0.,main_upc=None)


def moves():
    return [dict(move_id=1,state='done',move_date='2026-04-08',product_qty=3.,price_unit=0.,picking_id=7,
                 receipt='IN/GIFT',receipt_state='done',source_usage='supplier',dest_usage='internal'),
            dict(move_id=2,state='done',move_date='2026-10-28',product_qty=1.,price_unit=0.,
                 source_usage='internal',dest_usage='customer',sale_line_id=10)]


def messages():
    return [dict(picking_id=7,body='<p>雷神赠品0成本入库</p>')]


def row():
    return dict(order_line_id=10,order_number='SO-GIFT',market_place='Target',store='TA',sku='GIFT-SKU',main_ir='GIFT',
                product_cost_unit=0.,hardware_cost=0.,removed_hardware=0.,units=1.,sales=29.99,actual_ir='GIFT',
                created_at='2026-10-28 10:00',qty_delivered=1.,order_state='sale',walmart_status=None,
                positive_scan_records=1,amz_fulfullment_order_status=None,cwzy_ss_order_status=None)


def test_no_po_alone_never_exempts_and_confirmed_gift_does():
    no_po=p.judge(context(),[],[],[])
    assert no_po['code']=='no_po_zero_purchase_price' and not no_po['gift_verified']
    r=row();r['cost_review']=no_po
    assert patrol_policy.decision('unit_cost_non_positive',r) is None
    gift=p.judge(context(),[],moves(),messages())
    assert gift['gift_verified'] and gift['gift_receipts']==['IN/GIFT']
    r['cost_review']=gift
    for rule in ['missing_product_cost','unit_cost_non_positive']:
        assert patrol_policy.decision(rule,r)==dict(state='exempt',reason='verified_gift_receipt')
    assert patrol_policy.decision('removed_hardware_abnormal',r) is None


@pytest.mark.parametrize('field,value', [('product_cost_unit',None),('product_cost_unit',-1.),('hardware_cost',None),
    ('hardware_cost',-1.),('removed_hardware',1.)])
def test_gift_does_not_hide_missing_negative_or_excess_removal(field,value):
    r=row();r['cost_review']=p.judge(context(),[],moves(),messages());r[field]=value
    assert not patrol_policy.allowed_zero_cost('unit_cost_non_positive',r)


@pytest.mark.parametrize('change', ['paid','missing_comment','negated_comment','mixed_receipt','return','inventory','overdraw',
    'missing_date','wrong_line','po_link','procurement_link','duplicate_move','pending_receipt','null_quantity'])
def test_gift_proof_fails_closed(change):
    m=moves(); notes=messages()
    if change=='paid':m[0]['price_unit']=10
    if change=='missing_comment':notes=[]
    if change=='negated_comment':notes[0]['body']='不是雷神赠品0成本入库'
    if change=='mixed_receipt':m.append({**m[0],'move_id':3,'picking_id':8,'move_date':'2026-11-01'})
    if change=='return':m[1]['origin_returned_move_id']=30
    if change=='inventory':m[0]['inventory_id']=30
    if change=='overdraw':m[1]['product_qty']=4
    if change=='missing_date':m[0]['move_date']=None
    if change=='wrong_line':m[1]['sale_line_id']=11
    if change=='po_link':m[0]['purchase_line_id']=30
    if change=='procurement_link':m[0]['procurement_id']=30
    if change=='duplicate_move':m.append(deepcopy(m[0]))
    if change=='pending_receipt':m[0]['receipt_state']='draft'
    if change=='null_quantity':m[0]['product_qty']=None
    assert not p.judge(context(),[],m,notes)['gift_verified']


def test_future_paid_po_reopens_and_sku_is_not_allowlisted():
    c=context()
    po=[dict(price_unit=10,po_number='PO/NEW',currency='USD')]
    result=p.judge(c,po,moves(),messages())
    assert not result['gift_verified'] and result['code']=='purchase_cost_available'
    assert result['po_numbers']==['PO/NEW']
    c['purchase_price']=10
    assert p.judge(c,[],moves(),messages())['code']=='purchase_cost_available'
    c=context();c['tracking_method']='sn'
    assert p.judge(c,[],moves(),messages())['code']=='unsupported_cost_branch'
    c=context();c['fallback_matches']=0
    assert p.judge(c,[],moves(),messages())['status']=='incomplete'


def test_failed_or_truncated_queries_are_visible_and_do_not_exempt(monkeypatch):
    def fail(*args):raise RuntimeError('postgres://secret')
    result=p.enrich(pd.DataFrame([row()]),fail).iloc[0].cost_review
    assert result['status']=='failed' and not result['gift_verified']
    assert 'secret' not in json.dumps(result)
    monkeypatch.setenv('MAX_RESULT_ROWS','1')
    result=p.enrich(pd.DataFrame([row()]),lambda *a:pd.DataFrame([context(),context()])).iloc[0].cost_review
    assert result['status']=='failed'


def test_lifecycle_tracking_web_agree_and_failed_history_is_visible(tmp_path):
    r=row();r['cost_review']=p.judge(context(),[],moves(),messages())
    source=pd.DataFrame([r]);cost=pd.DataFrame([{**r,'order_id':99,'product_id':77,'line_matches':1}])
    now=datetime(2026,11,4,15,tzinfo=timezone.utc);day=date(2026,10,28);snap=[]
    report=lifecycle_patrol.review(day,['missing_product_cost','unit_cost_non_positive'],now,
        lambda _:(pd.DataFrame(),cost,source),snap.append)
    assert report['cost']['overlapping_cost_rule_lines']==0
    assert report['cost']['rule_counts']['unit_cost_non_positive']==0
    assert report['cost_provenance'][0]['gift_verified']
    assert len([x for x in snap[0]['observations'] if x['state']=='exempt'])==2
    assert all(x not in json.dumps(report) for x in ['order_line_id','product_id','order_id'])
    src={**r,'result_matches':1,'result_cost':0.,'result_sales':29.99,'source_cost':0.,'source_sales':29.99,
         'raw_cost':0.,'last_scan_at':'2026-10-28 13:00','line_matches':1}
    assert diagnostics.assess('unit_cost_non_positive',r,src,now)['reason_code']=='verified_gift_receipt'
    r['cost_review']={'status':'failed','code':'evidence_read_failed','gift_verified':False,'summary':'未完成'}
    snap=patrol_tracking.collect_snapshot(day,now,pd.DataFrame(),cost.drop(columns='cost_review'),pd.DataFrame([r]),[],['unit_cost_non_positive'])
    state=dict(schema_version=1,cases={},imported_reports=[],health={})
    daily=dict(window={'start':str(day)},query_succeeded=True,status='reviewed',lifecycle_review={'status':'partial_assessment'},diagnostics={'source_status':'ok'})
    patrol_tracking.review_history(daily,state,tmp_path,[snap],now)
    assert daily['history_review']['cost_evidence_incomplete_days']==[str(day)]
    assert daily['notification']['health_changes']['cost_provenance']['current']=='incomplete'


def test_legacy_exemption_requires_complete_count_and_no_association_gap():
    case=dict(cohort='cost',rule='unit_cost_non_positive',state='open',count=2,market_place='M',store='S',sku='P')
    exempt={**case,'state':'exempt','reason':'verified_gift_receipt','count':1}
    assert patrol_tracking._legacy_observation(case,{'observations':[exempt]})['state']=='unknown'
    gap=dict(cohort='cost',rule='outbound_match',state='unknown',count=1)
    assert patrol_tracking._legacy_observation(case,{'observations':[exempt,exempt,gap]})['state']=='unknown'


def test_history_evidence_change_notifies_once_and_missing_read_cannot_show_old_gift():
    now=datetime(2026,11,4,15,tzinfo=timezone.utc)
    item=dict(state='open',reason='open',rule='unit_cost_non_positive',cost_review={'code':'no_po_zero_purchase_price'})
    case=patrol_tracking._new_case(item,now.isoformat())
    changed={**item,'cost_review':{'code':'purchase_cost_available'}}
    assert patrol_tracking._update(case,changed,now)=='check_changed'
    assert patrol_tracking._update(case,changed,now) is None
    patrol_tracking._update(case,{'state':'unknown','reason':'missing_source'},now)
    assert 'cost_review' not in case


def test_legacy_missing_rows_do_not_reuse_old_gift_proof():
    case=dict(cohort='cost',rule='unit_cost_non_positive',state='exempt',count=1,market_place='M',store='S',sku='P',
        cost_review={'gift_verified':True,'code':'verified_gift_receipt'})
    result=patrol_tracking._legacy_observation(case,{'observations':[]})
    assert result['state']=='unknown' and 'cost_review' not in result


def test_gift_case_changes_once_stops_reminders_and_paid_source_reopens():
    from datetime import timedelta
    now=datetime(2026,11,4,15,tzinfo=timezone.utc)
    initial=dict(state='open',reason='open',rule='unit_cost_non_positive')
    case=patrol_tracking._new_case(initial,(now-timedelta(days=6)).isoformat())
    proof=p.judge(context(),[],moves(),messages())
    exempt=dict(state='exempt',reason='verified_gift_receipt',cost_review=proof)
    assert patrol_tracking._update(case,exempt,now)=='changed'
    assert patrol_tracking._update(case,exempt,now) is None
    assert not patrol_tracking._due(case,now+timedelta(days=10))
    paid=p.judge(context(),[dict(price_unit=10,po_number='PO/NEW',currency='USD')],moves(),messages())
    assert patrol_tracking._update(case,{**initial,'cost_review':paid},now+timedelta(days=1))=='reopened'
    assert case['priority']=='high'


def test_public_evidence_serializes_missing_dimensions_without_nan():
    r=row();r['store']=float('nan');r['cost_review']=p.judge(context(),[],[],[])
    result=p.public_reviews(pd.DataFrame([r]))
    assert result[0]['store'] is None
    json.dumps(result,allow_nan=False)


def test_gift_exception_cannot_hide_changed_hardware_snapshot():
    r=row();src={**r,'cost_review':p.judge(context(),[],moves(),messages()),'result_matches':1,
        'result_cost':0.,'result_sales':29.99,'source_cost':0.,'source_sales':29.99,'raw_cost':0.,
        'last_scan_at':'2026-10-28 13:00','line_matches':1,'source_hardware_cost':10.,'source_removed_hardware':0.}
    now=datetime(2026,11,4,15,tzinfo=timezone.utc)
    result=diagnostics.assess('unit_cost_non_positive',r,src,now)
    assert result['reason_code']=='snapshot_changed' and result['assessment']=='unknown'


def test_new_unshipped_zero_cost_does_not_trigger_purchase_lookup():
    def query(*args):pytest.fail('unshipped sales cohort must not run cost provenance')
    result=p.enrich(pd.DataFrame([row()]),query,eligible_ids=set())
    assert 'cost_review' not in result


def test_legacy_cleared_association_does_not_publish_all_healthy_orders():
    case=dict(cohort='cost',rule='outbound_match',identity_kind='legacy_group',state='open',count=1,
        market_place=None,store=None,sku=None,order_numbers=['OLD-UNVERIFIED'])
    observations=[dict(cohort='cost',rule='outbound_match',state='clear',reason='unique_match',count=1,order_numbers=[f'HEALTHY-{i}']) for i in range(100)]
    result=patrol_tracking._legacy_observation(case,{'observations':observations})
    assert result['state']=='clear' and result['order_numbers']==[]
    now=datetime(2026,11,4,15,tzinfo=timezone.utc)
    tracked=patrol_tracking._new_case(case,now.isoformat())
    patrol_tracking._update(tracked,result,now)
    assert tracked['order_numbers']==[]
