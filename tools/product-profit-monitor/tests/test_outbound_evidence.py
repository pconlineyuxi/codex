from copy import deepcopy
from datetime import date, datetime, timezone
import pandas as pd
import pytest
from bi_check_agent import outbound_evidence as p, lifecycle_patrol, patrol_tracking


def evidence(sn=True):
    lines=[dict(order_line_id=i,product_uom_qty=1.,qty_delivered=1.) for i in [10,11]]
    scans=[dict(event_id=i,sn=f'SN{i}' if sn else None,scan_qty=1.) for i in [10,11]]
    moves=[dict(order_line_id=i,move_line_id=i,state='done',source_usage='internal',dest_usage='customer',
                origin_returned_move_id=None,qty_done=1.,sn=f'SN{i}' if sn else None) for i in [10,11]]
    return lines,scans,moves


@pytest.mark.parametrize('sn,basis',[(True,'sn_stock_move'),(False,'full_quantity_stock_move')])
def test_multi_line_can_resolve_from_physical_evidence(sn,basis):
    assert p.resolve(*evidence(sn))==([10,11],basis)


@pytest.mark.parametrize('change', ['duplicate_scan','duplicate_move','sn_mismatch','sn_multiple_lines','return',
    'wrong_direction','missing_qty','missing_move_line','partial_scan','partial_delivery','missing_line','mixed_sn'])
def test_sn_conflicts_do_not_resolve(change):
    lines,scans,moves=evidence()
    if change=='duplicate_scan':scans.append(deepcopy(scans[0]))
    if change=='duplicate_move':moves.append(deepcopy(moves[0]))
    if change=='sn_mismatch':moves[0]['sn']='OTHER'
    if change=='sn_multiple_lines':moves[1]['sn']=moves[0]['sn']
    if change=='return':moves[0]['origin_returned_move_id']=5
    if change=='wrong_direction':moves[0]['source_usage']='customer'
    if change=='missing_qty':moves[0]['qty_done']=None
    if change=='missing_move_line':moves[0]['move_line_id']=None
    if change=='partial_scan':scans.pop()
    if change=='partial_delivery':lines[0]['qty_delivered']=0
    if change=='missing_line':lines.pop()
    if change=='mixed_sn':scans[0]['sn']=None
    assert not p.resolve(lines,scans,moves)[0]


def test_sn_can_confirm_a_subset_but_non_sn_requires_all_candidate_lines_delivered():
    lines,scans,moves=evidence()
    lines[1]['qty_delivered']=0
    assert p.resolve(lines,scans[:1],moves[:1])[0]==[10]
    for records in (scans,moves):
        for r in records:r['sn']=None
    assert not p.resolve(lines,scans[:1],moves[:1])[0]


def test_empty_movement_and_failed_read_stay_unresolved():
    lines,scans,_=evidence()
    assert not p.resolve(lines,scans,[])[0]
    cost=pd.DataFrame([dict(order_id=1,product_id=2,line_matches=2,order_line_id=10)])
    def fail(*a):raise RuntimeError('secret')
    result=p.enrich(cost,datetime(2026,11,3),datetime(2026,11,4),fail)
    assert result.iloc[0].association_basis=='evidence_read_failed'
    assert p.resolved_rows(result).empty


def test_expanded_cost_keeps_pair_counts_and_single_tracking_gap_identity():
    cost=pd.DataFrame([dict(order_id=1,product_id=2,line_matches=2,order_line_id=i,association_resolved=True,
        association_basis='sn_stock_move',actual_ir='PART',created_at='2026-10-20') for i in [10,11]])
    src=pd.DataFrame([dict(order_line_id=i,market_place='M',store='S',sku=f'SKU{i}',main_ir='PART',
        product_cost_unit=10.,hardware_cost=0.,removed_hardware=0.,units=1.,sales=20.) for i in [10,11]])
    snap=[];now=datetime(2026,11,4,15,tzinfo=timezone.utc);day=date(2026,11,3)
    result=lifecycle_patrol.review(day,['unit_cost_non_positive'],now,lambda _:(pd.DataFrame(),cost,src),snap.append)
    assert result['cost']['candidate_order_products']==1
    assert result['cost']['candidate_lines']==2
    assert result['cost']['evidence_resolved_order_products']==1
    assert result['cost']['ambiguous_order_products']==0
    assert result['cost']['created_before_outbound_day']==1
    assert len([o for o in snap[0]['observations'] if o['rule']=='outbound_match'])==1
    assert len({patrol_tracking.identity(o) for o in snap[0]['observations']})==3


def test_web_probe_uses_same_evidence_and_does_not_confirm_other_lines(monkeypatch):
    frame=pd.DataFrame([dict(raw_order_id=1,product_id=2,order_line_id=i,line_matches=2,last_scan_at='2026-11-03 12:00') for i in [10,11]])
    def resolver(cost,start,end,query_fn):
        return pd.DataFrame([{**cost.iloc[0].to_dict(),'order_line_id':10,'association_resolved':True,'association_basis':'sn_stock_move'}])
    monkeypatch.setattr(p,'enrich',resolver)
    result=p.enrich_source(frame)
    assert p.resolved(result.iloc[0])
    assert not p.resolved(result.iloc[1])
