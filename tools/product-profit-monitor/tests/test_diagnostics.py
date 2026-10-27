from datetime import date, datetime, timezone
from pathlib import Path
import json

import pandas as pd
import pytest

from bi_check_agent import diagnostics as d, core, service, codex_patrol, profit_analysis, versioning
from bi_check_agent.models import AnomalyQueryRequest
from bi_check_agent.monitor import MonitorStore

NOW=datetime(2026,10,27,15,tzinfo=timezone.utc)


def request():
    return AnomalyQueryRequest(start_date='2026-10-20',end_date='2026-10-21',
        anomaly_rules=['missing_product_cost','unit_cost_non_positive'],aggregation_dimensions=['marketplace','store','sku','day'])


def rows():
    frame=service.demo_rows(request()).iloc[:1].copy()
    frame['product_cost_unit']=0.
    frame['hardware_cost']=0.
    frame['unit_cost']=0.
    frame['order_cost_total']=0.
    return core._prepare_date_dimensions(frame)


def source(**changes):
    row=rows().iloc[0].to_dict()
    row.update(order_line_id=1,source_cost=0.,source_sales=row['sales'],raw_cost=0.,
        last_scan_at='2026-10-20 16:00:00',line_matches=1,created_at='2026-10-20 12:00:00',
        order_state='sale',walmart_status=None,result_matches=1,result_cost=0.,result_sales=row['sales'])
    row.update(changes)
    return row


@pytest.mark.parametrize('changes,state,reason',[
    ({'last_scan_at':None},'waiting_data','not_shipped'),
    ({'last_scan_at':'2026-10-27 14:00:00'},'waiting_data','before_cost_deadline'),
    ({'raw_cost':None},'suspected_error','raw_cost_missing'),
    ({'source_cost':50.,'raw_cost':50.},'verified_difference','source_result_mismatch'),
    ({},'suspected_error','cost_still_non_positive'),
    ({'line_matches':2},'unknown','ambiguous_outbound'),
    ({'result_matches':2},'unknown','ambiguous_result'),
    ({'result_cost':50.},'unknown','snapshot_changed'),
    ({'order_state':'cancel'},'unknown','business_scope_changed'),
])
def test_cost_assessment_prerequisites(changes,state,reason):
    item=d.assess('missing_product_cost',rows().iloc[0],source(**changes),NOW)
    assert item['assessment']==state and item['reason_code']==reason
    assert item['cause_status']!='verified'
    if state=='verified_difference':
        assert item['measurements']['source_cost']==50 and item['measurements']['result_cost']==0


def test_mixed_missing_duplicate_unknown_and_no_raw_exports():
    frame=rows()
    hits=core.detect_anomalies(frame,request().anomaly_rules)
    def run(sql,params):return pd.DataFrame([source(),source()])
    report=d.build(frame,hits,request(),'live_test',run,NOW)
    assert all(i['assessment']=='unknown' for i in report['assessments'])
    assert report['summary']['unknown']==1  # Two rules, one business object.
    exported=json.dumps(d.public(report),ensure_ascii=False)
    assert 'object_keys' not in exported and 'order_line_id' not in exported
    assert rows().iloc[0]['order_id'] not in exported


def test_source_failure_is_unknown_and_sanitized():
    def fail(*args):raise RuntimeError('postgres://secret')
    frame=rows()
    report=d.build(frame,core.detect_anomalies(frame,request().anomaly_rules),request(),'live_test',fail,NOW)
    assert report['source_status']=='failed' and report['unresolved_keys']
    assert 'secret' not in json.dumps(report)
    assert all(i['assessment']=='unknown' for i in report['assessments'])


def test_source_truncation_is_not_a_verified_reason(monkeypatch):
    monkeypatch.setenv('MAX_RESULT_ROWS','1')
    frame=rows()
    report=d.build(frame,core.detect_anomalies(frame,request().anomaly_rules),request(),'live_test',lambda *a:pd.DataFrame([source(),source()]),NOW)
    assert report['source_status']=='failed'


def test_service_codex_and_worker_use_identical_assessment(monkeypatch):
    frame=rows()
    def run(sql,params):
        return pd.DataFrame([source(raw_cost=None)]) if 'LATERAL' in sql else frame.copy()
    monkeypatch.setattr(service.db,'run_query',run)
    # Fix the shared clock, not each consumer's interpretation.
    original=d.build
    monkeypatch.setattr(d,'build',lambda *a,**kw:original(*a,**kw,now=NOW))
    result=service.query(request(),'live_test')
    report=codex_patrol.build_report(date(2026,10,20),request().anomaly_rules,query_fn=lambda req,mode:service.query(req,'live_test'))
    assert report['diagnostics']==d.public(result['evidence']['diagnostics'])
    monkeypatch.setattr(service,'query',lambda *a,**kw:result)
    evaluated=service.evaluate_plan({'mode':'demo','rules':request().anomaly_rules},'2026-10-20','2026-10-21')
    assert evaluated['findings']
    for finding in evaluated['findings']:
        assert finding['evidence']['assessment']['reason_code']=='raw_cost_missing'


def test_waiting_records_do_not_create_formal_findings(monkeypatch):
    frame=rows()
    monkeypatch.setattr(service.db,'run_query',lambda sql,params:pd.DataFrame([source(last_scan_at=None)]) if 'LATERAL' in sql else frame.copy())
    result=service.query(request(),'live_test')
    monkeypatch.setattr(service,'query',lambda *a,**kw:result)
    evaluated=service.evaluate_plan({'mode':'demo','rules':request().anomaly_rules},'2026-10-20','2026-10-21')
    assert not evaluated['findings']
    assert evaluated['evidence']['unresolved_keys']


def test_absent_or_unknown_object_is_not_recovered(tmp_path):
    store=MonitorStore(tmp_path/'state')
    plan=store.save_plan({'mode':'demo','rules':['missing_product_cost']})
    finding={'key':'rule-object','rule':'missing_product_cost','object':'SKU','value':1,'evidence':{'object_key':'object'}}
    store.run(plan,'2026-10-20','2026-10-21',lambda *a:{'findings':[finding],'evidence':{'complete':True}})
    def clean(covered,unresolved):
        return lambda *a:{'findings':[],'evidence':{'complete':True,'recovery_policy':'object_present','coverage_keys':covered,'unresolved_keys':unresolved}}
    store.run(plan,'2026-10-20','2026-10-21',clean([],[]))
    assert store.incidents()[0]['status']=='open'
    store.run(plan,'2026-10-20','2026-10-21',clean(['object'],['object']))
    assert store.incidents()[0]['status']=='open'
    store.run(plan,'2026-10-20','2026-10-21',clean(['object'],[]))
    assert store.incidents()[0]['status']=='recovered'


def test_ba_review_persists_and_modes_do_not_mix(tmp_path):
    store=MonitorStore(tmp_path/'state')
    with pytest.raises(ValueError):store.save_review('query','diagnosis-x','live_test','normal','')
    store.save_review('query','diagnosis-x','live_test','normal','核对出库记录，尚未出库')
    restarted=MonitorStore(tmp_path/'state')
    assert restarted.reviews('live_test')[0]['label']=='normal'
    assert not restarted.reviews('demo')
    restarted.save_review('query','diagnosis-x','live_test','waiting_data','等待下一批次')
    assert len(restarted.reviews('live_test'))==1


def test_current_business_policy_does_not_adopt_pre_diagnostics_history():
    assert versioning.POLICY_REVISION>=2
    assert versioning.compatible_legacy_versions(versioning.diagnostic_policy_version())==()


def test_no_unsupported_numeric_or_causal_claim_passes():
    evidence={'source_analyses':[{'evidence_id':'source-check-1','periods':{'current':{'rows':[{'sales__sum':100.}]}}}]}
    profit_analysis.validate_answer('销售额100元。[source-check-1]',evidence,'stop')
    for text in ['销售额999999元。[source-check-1]','已确认根因是成本写入任务失败。[source-check-1]']:
        with pytest.raises(ValueError):profit_analysis.validate_answer(text,evidence,'stop')
    profit_analysis.validate_answer('任务失败尚未验证，需要核查原因。[source-check-1]',evidence,'stop')


def test_aggregate_object_keys_keep_days_separate():
    a={'sku':'SKU','day':'2026-10-20'}
    b={**a,'day':'2026-10-21'}
    assert d.key_for(a)!=d.key_for(b)


def test_readonly_lookup_uses_actual_product_id_and_fixed_exclusions():
    calls=[]
    d.fetch_source(rows(),request(),lambda sql,params:calls.append((sql,params)) or pd.DataFrame())
    sql,params=calls[0]
    assert 'h.product_id=sl.product_id' in sql
    assert 'p.order_name IN (:o0)' in sql
    assert 'PROMOTION DISCOUNT' in sql and 'm.main_ir NOT IN' in sql
    assert rows().iloc[0]['order_id'] not in sql


def test_source_lookup_uses_utc_and_restores_query_timezone():
    from bi_check_agent import db
    before=db._query_timezone.get()
    calls=[]
    def run(sql,params):
        calls.append(db._query_timezone.get())
        raise RuntimeError('failure')
    with pytest.raises(RuntimeError):d.fetch_source(rows(),request(),run)
    assert calls==['UTC'] and db._query_timezone.get()==before


def test_group_evidence_keeps_range_not_first_line_value():
    frame=rows()
    second=frame.copy();second['order_id']='OTHER'
    frame=pd.concat([frame,second],ignore_index=True)
    a=source(source_cost=50.,raw_cost=50.)
    b=source(source_cost=70.,raw_cost=70.,order_id='OTHER')
    report=d.build(frame,core.detect_anomalies(frame,request().anomaly_rules),request(),'live_test',lambda *a_:pd.DataFrame([a,b]),NOW)
    assert report['summary']['verified_difference']==2
    for item in report['assessments']:
        assert item['measurement_ranges']['source_cost']=={'min':50.,'max':70.,'missing_count':0}
        assert 'measurements' not in item


def test_citations_include_only_returned_diagnostic_records():
    e={'source_analyses':[
        {'evidence_id':'source-a-1','periods':{'current':{'matched_record_count':1}}},
        {'evidence_id':'source-a-2','assessments':{'current':{'assessments':[{'evidence_id':'diagnosis-a','facts':['合成数据']}]}}}]}
    profit_analysis.validate_answer('1. 缺少任务成功记录，不能确认原因。[source-a-1][diagnosis-a]',e,'stop')
    with pytest.raises(ValueError):profit_analysis.validate_answer('依据 [source-a-1][diagnosis-unreturned]',e,'stop')


def test_answer_uses_diagnostics_and_repairs_unsupported_number(monkeypatch):
    from types import SimpleNamespace as N
    import openai
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-key')
    result=service.query(request(),'demo')
    snapshot=profit_analysis.scan_snapshot({'id':'synthetic','evidence':result['evidence'],'summary':[]},request(),result)
    seen=[]
    def create(**kwargs):
        seen.append(kwargs)
        turn=len(seen)
        if turn<=2:
            name='analyze_filtered_source_data' if turn==1 else 'review_diagnostic_evidence'
            args={'period':'current','measures':[{'field':'sales','statistic':'sum'}]} if turn==1 else {}
            msg=N(content=None,tool_calls=[N(id='call'+str(turn),function=N(name=name,arguments=json.dumps(args)))])
        else:
            tools=[json.loads(m['content']) for m in kwargs['messages'] if m['role']=='tool']
            ref=tools[0]['evidence_id']
            text='销售额999999元。' if turn==3 else '缺少任务成功记录，原因仍待核实。'
            msg=N(content=text+'['+ref+']',tool_calls=[])
        return N(choices=[N(message=msg,finish_reason='tool_calls' if turn<=2 else 'stop')])
    monkeypatch.setattr(openai,'OpenAI',lambda **kw:N(chat=N(completions=N(create=create))))
    reply=profit_analysis.answer(snapshot,'原因是什么？')
    assert len(seen)==4 and '999999' not in reply['content']
    assert reply['coverage']['analysis_operations']==2


def test_waiting_guard_preserves_database_timestamp_precision(monkeypatch):
    frame=rows()
    frame['date_order']=pd.to_datetime(frame.date_order)+pd.Timedelta(microseconds=123)
    src=source(last_scan_at=None,date_order=frame.iloc[0]['date_order'])
    monkeypatch.setattr(service.db,'run_query',lambda sql,params:pd.DataFrame([src]) if 'LATERAL' in sql else frame.copy())
    result=service.query(request(),'live_test')
    assert result['evidence']['diagnostics']['summary']['waiting_data']==1
    monkeypatch.setattr(service,'query',lambda *a,**kw:result)
    evaluated=service.evaluate_plan({'mode':'demo','rules':request().anomaly_rules},'2026-10-20','2026-10-21')
    assert not evaluated['findings']
    key=d.key_for(frame.iloc[0])
    assert key in evaluated['evidence']['coverage_keys']


def test_manual_diagnostic_review_saves_and_keeps_chat(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    state=tmp_path/'state.sqlite3'
    monkeypatch.setenv('PROFIT_STATE_DB',str(state))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(b for b in app.button if b.label=='立即巡查').click().run()
    next(s for s in app.selectbox if s.label=='BA 核实结论').set_value('normal').run()
    next(t for t in app.text_area if t.label=='核实依据').set_value('已核对出库记录，尚未出库').run()
    next(b for b in app.button if b.label=='保存核实记录').click().run()
    assert not app.exception and not app.error
    assert MonitorStore(state).reviews('demo')[0]['label']=='normal'
    assert not app.chat_input[0].disabled
