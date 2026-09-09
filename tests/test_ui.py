from streamlit.testing.v1 import AppTest


def test_overview_and_demo_scan(tmp_path,monkeypatch):
    monkeypatch.setenv('PROFIT_STATE_DB',str(tmp_path/'state.sqlite3'))
    app=AppTest.from_file(str(__import__('pathlib').Path('app.py').resolve())).run(timeout=20)
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
