from datetime import date
from types import SimpleNamespace
import json

import pytest
from bi_check_agent import profit_analysis as analysis, service
from bi_check_agent.models import AnomalyQueryRequest


def requests():
    common={'sku':['DEMO-HEALTHY'],'aggregation_dimensions':['store','sku']}
    return (AnomalyQueryRequest(start_date=date(2026,9,8),end_date=date(2026,9,9),**common),
            AnomalyQueryRequest(start_date=date(2026,9,7),end_date=date(2026,9,8),**common))


def snapshot():
    return analysis.compare(*requests(),'demo')


def test_equal_period_and_leap_day():
    assert analysis.previous_equal_period(date(2024,3,1),date(2024,3,8))==(date(2024,2,23),date(2024,3,1))
    with pytest.raises(ValueError): analysis.previous_equal_period(date(2024,3,2),date(2024,3,1))


def test_snapshot_reconciles_and_contains_no_order_rows():
    events=[]
    snap=analysis.compare(*requests(),'demo',progress=lambda *args:events.append(args))
    assert snap['report']['difference']==pytest.approx(sum(x['contribution'] for x in snap['report']['components']))
    assert snap['scope_key']==analysis.scope_key(*requests(),'demo')
    assert snap['previous_days']==snap['current_days']==1
    assert len(snap['_source_frames']['current'])>0
    assert 'order_name' not in json.dumps(analysis.context_for(snap,'test'))
    assert events[-1][0]==1
    assert '对比期' in events[0][1] and '本期' in events[-1][1]


def test_rejects_mismatched_scope_and_missing_cost():
    current,previous=requests()
    with pytest.raises(ValueError,match='相同'):
        analysis.compare(current,previous.model_copy(update={'sku':['different']}),'demo')
    with pytest.raises(ValueError,match='缺失'):
        analysis.compare(current.model_copy(update={'sku':['DEMO-COST']}),previous.model_copy(update={'sku':['DEMO-COST']}),'demo')


def test_empty_period_not_zero_profit(monkeypatch):
    import pandas as pd
    monkeypatch.setattr(service,'query',lambda *a,**kw:{'rows':pd.DataFrame()})
    with pytest.raises(ValueError,match='没有数据'):analysis.compare(*requests(),'live_test')


def test_context_uses_source_not_presentation_aggregates():
    snap=snapshot()
    snap['current_aggregate']=[{'sku':'fabricated','secret':'never-send'}]
    ctx=analysis.context_for(snap,'分析源数据')
    assert 'aggregate_rows' not in ctx
    assert ctx['periods']['current']['source_record_count']==len(snap['_source_frames']['current'])
    assert 'never-send' not in json.dumps(ctx)


def test_answer_calls_source_tool_and_uses_history(monkeypatch):
    import openai
    monkeypatch.setenv('OPENAI_API_KEY','fake-test-key')
    captured=[]
    def create(**kwargs):
        captured.append(dict(kwargs,messages=list(kwargs['messages'])))
        if len(captured)==1:
            call=SimpleNamespace(id='call1',function=SimpleNamespace(name='analyze_filtered_source_data',arguments=json.dumps({'period':'both','operation':'statistics','measures':[{'field':'shipping_fee','statistic':'sum'}]})))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None,tool_calls=[call]))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='源数据运费统计见 [source-1]。',tool_calls=[]))])
    monkeypatch.setattr(openai,'OpenAI',lambda **kw:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    result=analysis.answer(snapshot(),'为什么？',[{'role':'user','content':'运费变了吗？'},{'role':'assistant','content':'旧回答'}])
    assert result['coverage']['basis']=='filtered_source_records'
    assert result['coverage']['analysis_operations']==1
    assert captured[0]['tool_choice']=='required'
    assert captured[0]['store'] is False
    assert captured[0]['messages'][-1]['content']=='为什么？'
    tool=json.loads(captured[1]['messages'][-1]['content'])
    assert tool['periods']['current']['matched_record_count']>0
    assert 'fake-test-key' not in json.dumps(captured)


def test_api_error_is_sanitized(monkeypatch):
    import openai
    monkeypatch.setenv('OPENAI_API_KEY','fake-key')
    def fail(**kwargs):raise RuntimeError('secret-credential')
    monkeypatch.setattr(openai,'OpenAI',fail)
    with pytest.raises(RuntimeError,match='已有对话均保留') as exc:analysis.answer(snapshot(),'为何下降？')
    assert 'secret-credential' not in str(exc.value)


@pytest.mark.parametrize('old,new,expected',[(100,120,20),(100,80,-20),(100,100,0),(0,100,None),(0,0,None),(-100,-50,-50)])
def test_amount_change_percent(old,new,expected):
    assert service.amount_change_percent(old,new)==expected


def test_percentage_is_amount_change_not_profit_contribution():
    snap=snapshot()
    ctx=analysis.context_for(snap,'费用变化百分比？')
    for row in ctx['components']:
        assert row['change_percent']==service.amount_change_percent(row['previous'],row['current'])
    # A cost decrease is a negative amount change but a positive profit contribution.
    import pandas as pd
    before=service.demo_rows(requests()[0])
    after=before.copy()
    after['shipping_fee']=after['shipping_fee']*0.5
    report=service.decompose(before,after)
    shipping=next(c for c in report['components'] if c['item']=='运费')
    assert shipping['change_percent']==-50
    assert shipping['contribution']>0
