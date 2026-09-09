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
    app.radio[0].set_value('业务问题定位').run()
    assert not app.exception
    next(b for b in app.button if b.label=='执行查询与检查').click().run(timeout=20)
    assert not app.exception


def test_manual_live_mode_keeps_workspaces_and_optional_store(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'test.sqlite3'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(s for s in app.selectbox if s.label=='数据模式').set_value('live_test').run()
    assert not app.exception
    assert app.radio[0].options==['巡查概览','业务问题定位','异常历史','巡查计划']
    assert next(m for m in app.multiselect if m.label=='店铺（留空为全部）').value==[]
    for page in ['巡查概览','异常历史','巡查计划']:
        app.radio[0].set_value(page).run()
        assert not app.exception


def test_empty_dimensions_disable_query_and_comparison(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    app.radio[0].set_value('业务问题定位').run()
    next(m for m in app.multiselect if m.label=='汇总维度').set_value([]).run()
    assert not app.exception
    assert next(b for b in app.button if b.label=='执行查询与检查').disabled
    assert next(b for b in app.button if b.label=='比较利润变化').disabled
    assert any('汇总维度不能为空' in w.value for w in app.warning)


def test_store_options_and_all_store_query(tmp_path,monkeypatch):
    from pathlib import Path
    from bi_check_agent import ui, service
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    monkeypatch.setattr(ui,'available_stores',lambda *a:['Store A','Store B'])
    seen=[]
    def fake_query(request,mode):
        seen.append(request.store)
        return service.query(request,'demo')
    monkeypatch.setattr(ui,'query',fake_query)
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    next(s for s in app.selectbox if s.label=='数据模式').set_value('live_test').run()
    next(b for b in app.button if b.label=='加载当前日期范围内的店铺选项').click().run()
    selector=next(m for m in app.multiselect if m.label=='店铺（留空为全部）')
    assert selector.options==['Store A','Store B']
    next(b for b in app.button if b.label=='执行查询与检查').click().run()
    assert seen==[[]]
    assert not app.error and not app.exception
    next(m for m in app.multiselect if m.label=='店铺（留空为全部）').set_value(['Store A','Store B']).run()
    next(b for b in app.button if b.label=='执行查询与检查').click().run()
    assert seen[-1]==['Store A','Store B']


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
    app.radio[0].set_value('巡查概览').run()
    next(b for b in app.button if b.label=='立即巡查').click().run()
    assert not app.exception and not app.error
    assert seen[0][1]=='live_test'
    assert seen[0][0].anomaly_rules and seen[0][0].aggregation_dimensions
    assert len(list((tmp_path/'manual-scans').glob('*.json')))==1
    from bi_check_agent.monitor import MonitorStore
    assert MonitorStore(tmp_path/'state.sqlite3').plans()==[]
    assert MonitorStore(tmp_path/'state.sqlite3').outbox()==[]
