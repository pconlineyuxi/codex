from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pandas as pd
import pytest
from bi_check_agent import patrol_tracking as t, lifecycle_patrol as lifecycle, codex_patrol as patrol, service

DAY = date(2026,10,29)
NOW = datetime(2026,10,30,14,tzinfo=timezone.utc)


def state(): return {'schema_version':1,'cases':{},'imported_reports':[],'health':{}}


def observation(line=1, rule='missing_product_cost', status='open', day=DAY, **changes):
    return {'event_day':str(day),'cohort':'cost','identity_kind':'exact','order_line_id':line,
            'order_id':100+line,'product_id':200+line,'market_place':'Amazon','store':'US','sku':'SKU',
            'rule':rule,'state':status,'reason':status,'count':1, **changes}


def snapshot(*rows, day=DAY): return {'event_day':str(day),'checked_at':NOW.isoformat(),'observations':list(rows)}


def report(day=DAY):
    return {'mode':'review','query_succeeded':True,'window':{'start':str(day)},'status':'observations',
            'lifecycle_review':{'status':'reviewed'},'diagnostics':{'source_status':'ok'}}


def run(tmp_path, s, rows, now=NOW, day=DAY, callback=None):
    r=report(day)
    t.review_history(r,s,tmp_path,[snapshot(*rows,day=day)],now,review_fn=callback)
    return r


def test_normal_is_quiet_until_three_days_and_then_every_three(tmp_path):
    s=state()
    assert run(tmp_path,s,[observation()])['notification']['should_notify']
    for delta in (0,1,2):
        assert not run(tmp_path,s,[observation()],NOW+timedelta(days=delta))['notification']['should_notify']
    r=run(tmp_path,s,[observation()],NOW+timedelta(days=3))
    assert r['notification']['items'][0]['notification_reason']=='reminder'
    assert not run(tmp_path,s,[observation()],NOW+timedelta(days=3,hours=1))['notification']['should_notify']
    assert not run(tmp_path,s,[observation()],NOW+timedelta(days=5))['notification']['should_notify']
    assert run(tmp_path,s,[observation()],NOW+timedelta(days=6))['notification']['should_notify']


@pytest.mark.parametrize('rule', sorted(t.HIGH_RULES))
def test_high_repeats_daily_not_per_run(tmp_path,rule):
    s=state(); row=observation(rule=rule)
    run(tmp_path,s,[row])
    assert not run(tmp_path,s,[row],NOW+timedelta(hours=1))['notification']['should_notify']
    assert run(tmp_path,s,[row],NOW+timedelta(days=1))['notification']['should_notify']


def test_new_york_calendar_day_used_across_dst(tmp_path):
    s=state(); row=observation(rule='unit_cost_non_positive')
    now=datetime(2026,11,1,4,30,tzinfo=timezone.utc)
    run(tmp_path,s,[row],now)
    assert not run(tmp_path,s,[row],now+timedelta(hours=24))['notification']['should_notify']
    assert run(tmp_path,s,[row],now+timedelta(hours=25))['notification']['should_notify']


def test_clear_reopen_and_missing_are_distinct(tmp_path):
    s=state();run(tmp_path,s,[observation()])
    missing=run(tmp_path,s,[],NOW+timedelta(hours=1))
    assert missing['notification']['items'][0]['state']=='unknown'
    assert not run(tmp_path,s,[],NOW+timedelta(hours=2))['notification']['should_notify']
    clear=run(tmp_path,s,[observation(status='clear')],NOW+timedelta(hours=3))
    assert clear['notification']['items'][0]['state']=='clear'
    assert not run(tmp_path,s,[observation(status='clear')],NOW+timedelta(days=7))['notification']['should_notify']
    reopened=run(tmp_path,s,[observation()],NOW+timedelta(days=8))
    assert reopened['notification']['items'][0]['notification_reason']=='reopened'


def test_same_sku_different_line_never_closes_old_case(tmp_path):
    s=state();run(tmp_path,s,[observation(line=1)])
    r=run(tmp_path,s,[observation(line=2)])
    assert len(s['cases'])==2
    assert {x['state'] for x in r['history_review']['items']}=={'unknown','open'}


def test_unresolved_older_than_window_still_queried(tmp_path):
    s=state();old=DAY-timedelta(days=25)
    row=observation(day=old)
    s['cases'][t.identity(row)]=t._new_case(row,(NOW-timedelta(days=25)).isoformat())
    calls=[]
    def query(day,rules,now,sink,targets):
        calls.append((day,targets))
        sink(snapshot(observation(day=old,status='clear'),day=old))
    r=run(tmp_path,s,[],callback=query)
    assert calls[0][0]==old and calls[0][1][0]['order_line_id']==1
    assert r['notification']['items'][0]['state']=='clear'


def test_old_closed_case_not_reopened_by_targeted_sibling_recheck(tmp_path):
    s=state();old=DAY-timedelta(days=25)
    for line,status in [(1,'open'),(2,'clear')]:
        row=observation(line=line,day=old,status=status)
        s['cases'][t.identity(row)]=t._new_case(row,(NOW-timedelta(days=25)).isoformat(),NOW.isoformat())
    def query(day,rules,now,sink,targets):
        assert len(targets)==1
        sink(snapshot(observation(day=old),day=old))
    run(tmp_path,s,[],callback=query)
    assert next(c for c in s['cases'].values() if c['order_line_id']==2)['state']=='clear'


def test_unknown_input_is_not_high_until_serious_rule_actually_hits(tmp_path):
    s=state();row=observation(rule='unit_cost_non_positive',status='unknown',reason='unknown_input')
    r=run(tmp_path,s,[row]);assert r['history_review']['items'][0]['priority']=='normal'
    assert not run(tmp_path,s,[row],NOW+timedelta(days=1))['notification']['should_notify']
    hit=observation(rule='unit_cost_non_positive')
    r=run(tmp_path,s,[hit],NOW+timedelta(days=1));assert r['history_review']['items'][0]['priority']=='high'
    r=run(tmp_path,s,[row],NOW+timedelta(days=2));assert r['history_review']['items'][0]['priority']=='high'


def test_query_failure_keeps_case_and_sanitizes(tmp_path):
    s=state();old=DAY-timedelta(days=1); row=observation(day=old)
    s['cases'][t.identity(row)]=t._new_case(row,NOW.isoformat(),NOW.isoformat())
    def fail(*args): raise RuntimeError('postgres://secret-password')
    r=run(tmp_path,s,[],callback=fail)
    assert r['history_review']['failed_event_days']==[str(old)]
    assert s['cases'][t.identity(row)]['state']=='unknown'
    assert 'secret-password' not in json.dumps(r)
    assert not run(tmp_path,s,[],callback=fail)['notification']['should_notify']


def save_legacy(tmp_path, day=DAY, **changes):
    section={'groups':[],'missing_source_lines':0,'duplicate_source_lines':0,'unknown_inputs':[],
             'ambiguous_order_products':0,'unmatched_order_products':0}
    cost={**section,'groups':[{'market_place':'Amazon','store':'US','sku':'SKU','anomaly_rules':'missing_product_cost','规则命中样本数':1}],**changes}
    data={'checked_at':(NOW-timedelta(days=1)).isoformat(),'lifecycle_review':{'status':'reviewed','sales':section,'cost':cost}}
    path=tmp_path/str(day)/'first'/'report.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))
    return path


def test_legacy_seed_is_idempotent_and_does_not_double_count_exact(tmp_path):
    save_legacy(tmp_path)
    s=state();r=run(tmp_path,s,[observation()])
    assert len(s['cases'])==1
    assert next(iter(s['cases'].values()))['identity_kind']=='legacy_group'
    assert not r['notification']['should_notify']
    run(tmp_path,s,[observation()])
    assert len(s['cases'])==1


def test_legacy_clear_requires_complete_same_group_not_missing_or_other_sku(tmp_path):
    save_legacy(tmp_path);s=state();run(tmp_path,s,[observation()])
    r=run(tmp_path,s,[observation(status='clear',sku='OTHER')])
    assert r['history_review']['items'][0]['state']=='unknown'
    r=run(tmp_path,s,[observation(status='clear'),observation(line=2,status='unknown',sku=None,reason='missing_source')])
    assert next(c for c in s['cases'].values() if c['identity_kind']=='legacy_group')['state']=='unknown'
    r=run(tmp_path,s,[observation(status='clear'),observation(line=2,status='clear')])
    assert any(n['state']=='clear' and n['identity_kind']=='legacy_group' for n in r['notification']['items'])


def test_legacy_cost_clear_blocked_by_ambiguous_shipment_of_unknown_identity(tmp_path):
    save_legacy(tmp_path);s=state();run(tmp_path,s,[observation()])
    run(tmp_path,s,[observation(status='clear'),observation(line=2,rule='outbound_match')])
    legacy=next(c for c in s['cases'].values() if c['identity_kind']=='legacy_group')
    assert legacy['state']=='unknown'


def test_damaged_state_preserved_and_lock_excludes_concurrent_run(tmp_path):
    path=tmp_path/'tracking-state.json';path.write_text('{broken')
    with pytest.raises(ValueError):t.load_state(tmp_path)
    assert path.read_text()=='{broken'
    with t.locked(tmp_path):
        with pytest.raises(RuntimeError,match='正在运行'):
            with t.locked(tmp_path):pass


def test_public_report_never_exposes_identity_or_relabels_clear_as_recovery(tmp_path):
    s=state();r=run(tmp_path,s,[observation()])
    payload=json.dumps(r)
    assert not any(k in payload for k in ('order_line_id','order_id','product_id'))
    md='\n'.join(t.markdown(r['history_review'],r['notification']))
    assert '不是正式异常恢复' in md and '不是去重订单数' in md


def test_collection_missing_duplicate_excluded_and_unknown_cannot_clear():
    sales=pd.DataFrame([{'order_line_id':i} for i in range(1,6)])
    common=dict(market_place='Amazon',store='US',sku='SKU',main_ir='SKU',units=1.,sales=0.,product_cost_unit=0.,hardware_cost=0.,removed_hardware=0.)
    source=pd.DataFrame([dict(common,order_line_id=1),dict(common,order_line_id=2),dict(common,order_line_id=2),
                         dict(common,order_line_id=3,main_ir='SHIPPING FEE'),dict(common,order_line_id=5,sales=float('nan'))])
    snap=t.collect_snapshot(DAY,NOW,sales,pd.DataFrame(),source,['zero_sales_with_units'],[])
    rows={r['order_line_id']:r for r in snap['observations']}
    assert rows[1]['state']=='open'
    assert [rows[i]['reason'] for i in (2,3,4,5)]==['duplicate_source','excluded_fixed_filter','missing_source','unknown_input']


def test_partial_shipment_and_overlap_kept_separate():
    cost=pd.DataFrame([{'order_id':10,'product_id':20,'line_matches':1,'order_line_id':1},
                       {'order_id':10,'product_id':21,'line_matches':2,'order_line_id':2}])
    source=pd.DataFrame([dict(order_line_id=i,market_place='Amazon',store='US',sku='SKU',main_ir='SKU',units=1.,sales=10.,product_cost_unit=0.,hardware_cost=0.,removed_hardware=0.) for i in (1,2)])
    snap=t.collect_snapshot(DAY,NOW,pd.DataFrame(),cost,source,[],['missing_product_cost','unit_cost_non_positive'])
    open_rows=[r for r in snap['observations'] if r['state']=='open']
    assert len(open_rows)==3
    assert len({r['order_line_id'] for r in open_rows if r['rule']!='outbound_match'})==1
    assert open_rows[-1]['rule']=='outbound_match'


def test_targeted_queries_are_bound_and_keep_event_window():
    queries=[]
    def query(sql,params):
        queries.append((sql,params))
        return pd.DataFrame(columns=['order_line_id','line_matches'])
    lifecycle.fetch_cohorts(DAY,query,targets=[observation()])
    assert len(queries)==2
    assert 'AND FALSE' in queries[0][0]
    assert '(order_id=:order_0 AND product_id=:product_0)' in queries[1][0]
    assert queries[1][1]['order_0']==101
    assert 'scan_time >= :start AND scan_time < :end AND scan_qty > 0' in queries[1][0]


def test_main_query_failure_still_rechecks_history(tmp_path):
    s=state();old=DAY-timedelta(days=1);row=observation(day=old)
    s['cases'][t.identity(row)]=t._new_case(row,NOW.isoformat())
    r=patrol.failed_report(DAY,'review',RuntimeError('network'))
    def query(day,rules,now,sink,targets):sink(snapshot(observation(day=old),day=old))
    t.review_history(r,s,tmp_path,[],NOW,review_fn=query)
    assert str(old) in r['history_review']['checked_event_days']
    assert not r['query_succeeded']
    assert '历史疑点复查' in patrol.markdown_report(r)


def test_state_persists_reminder_clock_across_restart(tmp_path):
    s=state();run(tmp_path,s,[observation()])
    (tmp_path/'tracking-state.json').write_text(json.dumps(s))
    restored=t.load_state(tmp_path)
    assert not run(tmp_path,restored,[observation()],NOW+timedelta(days=2))['notification']['should_notify']
    assert run(tmp_path,restored,[observation()],NOW+timedelta(days=3))['notification']['should_notify']


def test_cli_archives_public_report_and_private_state_and_second_run_quiet(tmp_path,monkeypatch,capsys):
    original=patrol.build_report
    def build(day,rules,mode,tracking_sink=None):
        def lifecycle_stub(day,rules):
            tracking_sink(snapshot(observation()))
            return {'status':'failed','note':'fixture'}
        return original(day,rules,mode,query_fn=lambda req,mode:service.query(req,'demo'),lifecycle_fn=lifecycle_stub)
    monkeypatch.setattr(patrol,'build_report',build)
    args=['--mode','review','--date',str(DAY),'--output-dir',str(tmp_path)]
    assert patrol.main(args)==0
    first=json.loads(capsys.readouterr().out)
    assert first['notification']['should_notify']
    private=tmp_path/'review'/'tracking-state.json'
    assert private.stat().st_mode & 0o777 == 0o600
    saved=json.loads(private.read_text());assert next(iter(saved['cases'].values()))['order_line_id']==1
    public=json.loads((tmp_path/'review'/'latest.json').read_text())
    assert 'order_line_id' not in json.dumps(public['history_review'])
    assert patrol.main(args)==0
    second=json.loads(capsys.readouterr().out)
    assert not second['notification']['should_notify']
