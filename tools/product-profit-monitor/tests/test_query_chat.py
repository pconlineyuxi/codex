from pathlib import Path
from streamlit.testing.v1 import AppTest
from bi_check_agent import profit_analysis,ui


def test_query_result_gets_inline_chat_without_extra_query(tmp_path,monkeypatch):
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    seen=[]
    def fake_answer(snapshot,question,history):
        seen.append((len(snapshot['_source_frames']['current']),len(history)))
        return {'content':'源数据核查结果。','evidence':{'kind':'query'}}
    monkeypatch.setattr(profit_analysis,'answer',fake_answer)
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(r for r in app.radio if r.label=='工作区').set_value('业务问题定位').run()
    assert app.chat_input[0].disabled
    next(b for b in app.button if b.label=='执行查询与检查').click().run()
    assert not app.exception and not app.chat_input[0].disabled
    def unexpected(*a,**kw):raise AssertionError('chat must not query database again')
    monkeypatch.setattr(ui,'query',unexpected)
    # Old cached results can be upgraded into source chat on rerun.
    app.session_state['query_scan_snapshot']={}
    app.run()
    assert not app.chat_input[0].disabled
    app.chat_input[0].set_value('成本缺失集中在哪？').run()
    app.chat_input[0].set_value('进一步说明').run()
    assert seen[0][0]>0 and seen[1][1]==2
    assert not app.exception and not app.error
    next(t for t in app.text_input if t.label=='SKU').set_value('DEMO-SHIP').run()
    assert app.chat_input[0].disabled
    assert len(app.session_state['query_scan_chat'])==4
