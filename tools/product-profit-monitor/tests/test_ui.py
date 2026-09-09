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


def test_manual_live_mode_only_exposes_query(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'test.sqlite3'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    app.selectbox[0].set_value('live_test').run()
    assert not app.exception
    assert app.radio[0].options==['业务问题定位']
    next(b for b in app.button if b.label=='执行查询与检查').click().run()
    assert any('店铺' in e.value for e in app.error)


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
