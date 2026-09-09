from streamlit.testing.v1 import AppTest


def test_overview_and_demo_scan(tmp_path,monkeypatch):
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    app=AppTest.from_file(str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'app.py')).run(timeout=20)
    assert not app.exception
    app.button[0].click().run(timeout=20)
    assert not app.exception
    assert app.metric[0].value=='3'
    app.button[0].click().run(timeout=20)
    assert app.metric[0].value=='3'
    next(r for r in app.radio if r.label=='工作区').set_value('业务问题定位').run()
    assert not app.exception
    next(b for b in app.button if b.label=='执行查询与检查').click().run(timeout=20)
    assert not app.exception


def test_manual_live_mode_keeps_workspaces_and_optional_store(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'test.sqlite3'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(s for s in app.selectbox if s.label=='数据模式').set_value('live_test').run()
    assert not app.exception
    assert app.radio[0].options==['巡查概览','业务问题定位','利润变化分析','异常历史','巡查计划']
    assert next(m for m in app.multiselect if m.label=='店铺（留空为全部）').value==[]
    for page in ['巡查概览','异常历史','巡查计划']:
        next(r for r in app.radio if r.label=='工作区').set_value(page).run()
        assert not app.exception


def test_empty_dimensions_disable_query_and_comparison(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(r for r in app.radio if r.label=='工作区').set_value('业务问题定位').run()
    next(m for m in app.multiselect if m.label=='汇总维度').set_value([]).run()
    assert not app.exception
    assert next(b for b in app.button if b.label=='执行查询与检查').disabled
    assert not any(b.label=='比较利润变化' for b in app.button)
    assert any('汇总维度不能为空' in w.value for w in app.warning)


def test_store_options_and_all_store_query(tmp_path,monkeypatch):
    from pathlib import Path
    from bi_check_agent import ui, service
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    monkeypatch.setattr(ui,'available_business_options',lambda *a:[{'market_place':'Platform A','store':'Store A'},{'market_place':'Platform B','store':'Store B'}])
    seen=[]
    def fake_query(request,mode):
        seen.append(request.store)
        return service.query(request,'demo')
    monkeypatch.setattr(ui,'query',fake_query)
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(s for s in app.selectbox if s.label=='数据模式').set_value('live_test').run()
    next(b for b in app.button if b.label=='加载当前日期范围内的平台和店铺选项').click().run()
    selector=next(m for m in app.multiselect if m.label=='店铺（留空为全部）')
    assert selector.options==['Store A','Store B']
    next(b for b in app.button if b.label=='执行查询与检查').click().run()
    assert seen==[[]]
    assert not app.error and not app.exception
    next(m for m in app.multiselect if m.label=='店铺（留空为全部）').set_value(['Store A','Store B']).run()
    next(b for b in app.button if b.label=='执行查询与检查').click().run()
    assert seen[-1]==['Store A','Store B']
    next(m for m in app.multiselect if m.label=='平台（留空为全部）').set_value(['Platform A']).run()
    assert next(m for m in app.multiselect if m.label=='店铺（留空为全部）').options==['Store A']
    assert not app.exception


def test_manual_scan_runs_without_enabling_schedule(tmp_path,monkeypatch):
    from pathlib import Path
    from bi_check_agent import ui, service
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    seen=[]
    def fake_query(request,mode):
        seen.append((request,mode))
        return service.query(request,'demo')
    monkeypatch.setattr(ui,'query',fake_query)
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(s for s in app.selectbox if s.label=='数据模式').set_value('live_test').run()
    next(r for r in app.radio if r.label=='工作区').set_value('巡查概览').run()
    next(b for b in app.button if b.label=='立即巡查').click().run()
    assert not app.exception and not app.error
    assert seen[0][1]=='live_test'
    assert seen[0][0].anomaly_rules and seen[0][0].aggregation_dimensions
    assert len(list((tmp_path/'manual-scans').glob('*.json')))==1
    from bi_check_agent.monitor import MonitorStore
    assert MonitorStore(tmp_path/'state.sqlite3').plans()==[]
    assert MonitorStore(tmp_path/'state.sqlite3').outbox()==[]


def test_profit_analysis_chat_persists_and_blocks_stale_scope(tmp_path,monkeypatch):
    from pathlib import Path
    from datetime import timedelta
    from bi_check_agent import profit_analysis
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    captured=[]
    def fake_answer(snapshot,question,history):
        captured.append((snapshot['id'],question,len(history)))
        return {'content':'根据 [total]，这是本次数据的解释。','coverage':{'complete':True},'evidence':{'analysis_id':snapshot['id']}}
    monkeypatch.setattr(profit_analysis,'answer',fake_answer)
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(r for r in app.radio if r.label=='工作区').set_value('利润变化分析').run()
    next(t for t in app.text_input if t.label=='SKU').set_value('DEMO-HEALTHY').run()
    next(b for b in app.button if b.label=='开始利润分析').click().run(timeout=20)
    assert not app.exception and not app.error
    assert len(app.metric)==3 and not app.chat_input[0].disabled
    initial=app.session_state['profit_snapshot']['id']
    app.chat_input[0].set_value('费用变化说明什么？').run(timeout=20)
    assert not app.exception
    assert len(app.session_state['profit_chat'])==2
    app.chat_input[0].set_value('还有哪些不能确定？').run(timeout=20)
    assert captured[-1][2]==2
    assert app.session_state['profit_snapshot']['id']==initial
    # Merely redrawing/downloading must not rerun the data query or discard chat.
    app.run()
    assert len(app.session_state['profit_chat'])==4
    end=next(d for d in app.date_input if d.label=='本期结束日期（包含当天）')
    end.set_value(end.value-timedelta(days=1)).run()
    assert app.chat_input[0].disabled
    assert any('条件已变化' in w.value for w in app.warning)
    assert app.session_state['profit_snapshot']['id']==initial
    next(b for b in app.button if b.label=='开始利润分析').click().run(timeout=20)
    assert app.session_state['profit_snapshot']['id']!=initial
    assert app.session_state['profit_chat']==[]
    assert not app.chat_input[0].disabled
    next(r for r in app.radio if r.label=='工作区').set_value('业务问题定位').run()
    next(r for r in app.radio if r.label=='工作区').set_value('利润变化分析').run()
    assert len(app.metric)==3 and not app.chat_input[0].disabled


def test_failed_comparison_keeps_previous_result(tmp_path,monkeypatch):
    from pathlib import Path
    from bi_check_agent import profit_analysis
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(r for r in app.radio if r.label=='工作区').set_value('利润变化分析').run()
    next(t for t in app.text_input if t.label=='SKU').set_value('DEMO-HEALTHY').run()
    next(b for b in app.button if b.label=='开始利润分析').click().run(timeout=20)
    initial=app.session_state['profit_snapshot']['id']
    def fail(*a,**kw):raise RuntimeError('测试读取失败')
    monkeypatch.setattr(profit_analysis,'compare',fail)
    next(b for b in app.button if b.label=='开始利润分析').click().run()
    assert any('测试读取失败' in e.value for e in app.error)
    assert app.session_state['profit_snapshot']['id']==initial
    assert len(app.metric)==3 and app.chat_input[0].disabled


def test_old_profit_snapshot_cannot_silently_use_aggregate_chat(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(r for r in app.radio if r.label=='工作区').set_value('利润变化分析').run()
    next(t for t in app.text_input if t.label=='SKU').set_value('DEMO-HEALTHY').run()
    next(b for b in app.button if b.label=='开始利润分析').click().run(timeout=20)
    old=dict(app.session_state['profit_snapshot'])
    old.pop('_source_frames')
    app.session_state['profit_snapshot']=old
    app.run()
    assert len(app.metric)==3
    assert app.chat_input[0].disabled
    assert any('旧结果没有保留源数据' in info.value for info in app.info)
