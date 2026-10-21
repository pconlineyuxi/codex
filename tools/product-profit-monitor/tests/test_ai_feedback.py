from pathlib import Path
from types import SimpleNamespace
import json

import httpx2 as httpx
import openai
import pytest
from streamlit.testing.v1 import AppTest

from bi_check_agent import profit_analysis, parser_rules, ui
from bi_check_agent.ai_errors import AIServiceError
from bi_check_agent.ai_feedback import BILLING_URL, STATE_KEY

APP = str(Path(__file__).resolve().parents[1] / 'app.py')
SECRET = 'test-secret-provider-body'


def failing_api(monkeypatch, code='credit_balance_exhausted', status=429):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key')
    error = openai.APIStatusError(
        SECRET,
        response=httpx.Response(status, request=httpx.Request('POST', 'https://example.test')),
        body={'code': code, 'message': SECRET},
    )
    calls = []
    def create(**kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    return calls


def select_page(app, page):
    next(r for r in app.radio if r.label == '工作区').set_value(page).run()


@pytest.mark.parametrize('page,run_button,namespace', [
    ('巡查概览', '立即巡查', 'manual_scan'),
    ('业务问题定位', '执行查询与检查', 'query_scan'),
    ('利润变化分析', '开始利润分析', 'profit'),
])
def test_all_chats_show_quota_and_recover_without_losing_data(tmp_path, monkeypatch, page, run_button, namespace):
    calls = failing_api(monkeypatch)
    monkeypatch.setenv('PROFIT_STATE_DB', str(tmp_path / 'state.sqlite3'))
    app = AppTest.from_file(APP).run()
    select_page(app, page)
    if page == '利润变化分析':
        next(t for t in app.text_input if t.label == 'SKU').set_value('DEMO-HEALTHY').run()
    next(b for b in app.button if b.label == run_button).click().run()
    snapshot = app.session_state[namespace + '_snapshot']
    snapshot_id = snapshot['id']
    history = [{'role': 'user', 'content': '之前的问题'}, {'role': 'assistant', 'content': '之前的回答'}]
    app.session_state[namespace + '_chat'] = list(history)
    assert calls == []  # No extra paid health checks.
    app.chat_input[0].set_value('这份数据的异常在哪里？').run()
    assert not app.exception
    assert any('API 额度不足或已耗尽' in e.value for e in app.error)
    assert any(BILLING_URL in m.value for m in app.markdown)
    assert SECRET not in '\n'.join(e.value for e in app.error)
    assert app.session_state[namespace + '_chat'] == history
    assert app.session_state[namespace + '_snapshot']['id'] == snapshot_id
    assert not app.chat_input[0].disabled
    app.run()
    assert any('API 额度不足或已耗尽' in e.value for e in app.error)
    select_page(app, '异常历史')
    assert any(BILLING_URL in m.value for m in app.markdown)
    assert calls == [True]
    select_page(app, page)
    monkeypatch.setattr(profit_analysis, 'answer', lambda *args: {'content': '重试成功', 'evidence': {}})
    app.chat_input[0].set_value('请重试').run()
    assert not app.exception and not app.error
    assert not any(BILLING_URL in m.value for m in app.markdown)
    assert app.session_state[namespace + '_snapshot']['id'] == snapshot_id
    assert app.session_state[namespace + '_chat'][:2] == history
    assert len(app.session_state[namespace + '_chat']) == 4


@pytest.mark.parametrize('code,status,category', [
    ('insufficient_quota', 429, 'quota_exhausted'),
    ('rate_limit_exceeded', 429, 'rate_limited'),
    ('invalid_api_key', 401, 'authentication_failed'),
    ('model_not_found', 404, 'model_unavailable'),
    (None, 500, 'service_error'),
])
def test_chat_preserves_failure_category(monkeypatch, code, status, category):
    failing_api(monkeypatch, code, status)
    monkeypatch.setattr(profit_analysis, 'context_for', lambda *args: {})
    with pytest.raises(AIServiceError) as exc:
        profit_analysis.answer({}, '检查异常')
    assert exc.value.code == category
    assert SECRET not in str(exc.value)
    assert '已有对话均保留' in str(exc.value)


@pytest.mark.parametrize('code,expected,billing', [
    ('credit_balance_exhausted', 'quota_exhausted', True),
    ('rate_limit_exceeded', 'rate_limited', False),
])
def test_parser_notice_persists_and_success_clears(tmp_path, monkeypatch, code, expected, billing):
    calls = failing_api(monkeypatch, code)
    monkeypatch.setenv('PROFIT_STATE_DB', str(tmp_path / 'state.sqlite3'))
    app = AppTest.from_file(APP).run()
    select_page(app, '业务问题定位')
    next(t for t in app.text_area if t.label == '业务问题').set_value('查找昨天只有广告花费但没有销售的产品')
    next(b for b in app.button if b.label == '解析为查询草稿').click().run()
    assert not app.exception
    assert app.session_state[STATE_KEY] == expected
    assert any(BILLING_URL in m.value for m in app.markdown) == billing
    if not billing:
        assert any('请求频率受限' in w.value for w in app.warning)
        assert not any('额度' in e.value for e in app.error)
    assert SECRET not in json.dumps(app.session_state['parsed_query'])
    select_page(app, '异常历史')
    assert app.session_state[STATE_KEY] == expected
    assert calls == [True]
    select_page(app, '业务问题定位')
    def successful_parse(text):
        result = parser_rules.fallback_parse(text)
        result['ai_status'] = 'succeeded'
        return result
    monkeypatch.setattr(ui, 'parse_business_description', successful_parse)
    next(b for b in app.button if b.label == '解析为查询草稿').click().run()
    assert not app.exception and not app.error
    assert STATE_KEY not in app.session_state
    assert not any(BILLING_URL in m.value for m in app.markdown)
