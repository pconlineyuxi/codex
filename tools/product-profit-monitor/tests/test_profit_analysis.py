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
    assert 'rows' not in snap and 'order_name' not in json.dumps(snap)
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


def test_context_is_bounded_and_finds_requested_object():
    snap=snapshot()
    snap['current_aggregate']=[{'sku':f'SKU-{i:04d}','Profit with Ads & Ship':i,'order_name':'never-send','secret':'never-send'} for i in range(200)]
    ctx=analysis.context_for(snap,'SKU-0001 的利润是多少？',limit=5)
    assert len(ctx['aggregate_rows'])==5
    assert any(r.get('sku')=='SKU-0001' for r in ctx['aggregate_rows'])
    assert not ctx['coverage']['complete']
    assert ctx['totals']['difference']==snap['report']['difference']
    assert 'never-send' not in json.dumps(ctx)


def test_answer_uses_snapshot_and_history_not_tools(monkeypatch):
    import openai
    monkeypatch.setenv('OPENAI_API_KEY','fake-test-key')
    captured={}
    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='费用贡献见 [component-1]。'))])
    monkeypatch.setattr(openai,'OpenAI',lambda **kw:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    result=analysis.answer(snapshot(),'为什么？',[{'role':'user','content':'销售额变了吗？'},{'role':'assistant','content':'旧回答'}])
    assert result['content'] and result['evidence']['analysis_id']
    assert captured['store'] is False and 'tools' not in captured
    assert captured['messages'][-1]['content']=='为什么？'
    assert captured['messages'][-3]['content']=='销售额变了吗？'
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
