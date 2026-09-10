from pathlib import Path
from datetime import date,timedelta
import pytest
from streamlit.testing.v1 import AppTest
from bi_check_agent import profit_analysis,service
from bi_check_agent.source_analysis import analyze
from bi_check_agent.models import AnomalyQueryRequest


def test_scan_snapshot_supports_incomplete_costs_and_single_period():
    request=AnomalyQueryRequest(start_date=date(2026,9,8),end_date=date(2026,9,9),sku=['DEMO-COST'])
    result=service.query(request,'demo')
    record={'id':'scan-test','evidence':result['evidence'],'summary':[]}
    snapshot=profit_analysis.scan_snapshot(record,request,result)
    context=profit_analysis.context_for(snapshot,'成本缺失？')
    assert context['kind']=='single_period_scan'
    assert set(context['periods'])=={'current'}
    stats=analyze(snapshot,{'measures':[{'field':'product_cost_unit','statistic':'missing_count'}]})
    assert stats['periods']['current']['rows'][0]['product_cost_unit__missing_count']>0
    with pytest.raises(ValueError,match='只有一个时期'):
        analyze(snapshot,{'operation':'compare'})


def test_scan_chat_and_stale_scope(tmp_path,monkeypatch):
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    seen=[]
    def fake_answer(snapshot,question,history):
        seen.append((snapshot['kind'],len(history)))
        return {'content':'源数据检查结果。','evidence':{'id':snapshot['id']}}
    monkeypatch.setattr(profit_analysis,'answer',fake_answer)
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(b for b in app.button if b.label=='立即巡查').click().run()
    assert not app.exception and not app.error
    assert not app.chat_input[0].disabled
    app.chat_input[0].set_value('缺失成本集中在哪里？').run()
    app.chat_input[0].set_value('还有呢？').run()
    assert seen==[('scan',0),('scan',2)]
    assert len(app.session_state['manual_scan_chat'])==4
    next(d for d in app.date_input if d.label=='巡查开始日期').set_value(date.today()-timedelta(days=3)).run()
    assert app.chat_input[0].disabled
    assert any('巡查条件已变化' in w.value for w in app.warning)
    next(b for b in app.button if b.label=='立即巡查').click().run()
    assert app.session_state['manual_scan_chat']==[]
    assert not app.chat_input[0].disabled
