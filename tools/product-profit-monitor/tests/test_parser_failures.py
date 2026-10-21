from datetime import date
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from streamlit.testing.v1 import AppTest

from bi_check_agent import ai_parser, parser_rules
from bi_check_agent.models import AnomalyQueryRequest

QUESTION = '查找昨天只有广告花费但没有销售的产品'


def api_failure(monkeypatch, code='credit_balance_exhausted', status=429):
    import openai
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    class ProviderError(Exception):
        pass
    error = ProviderError('secret-key-and-provider-response-must-not-be-shown')
    error.code, error.status_code = code, status
    def create(**kwargs):
        raise error
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))


@pytest.mark.parametrize('code,status,category', [
    ('credit_balance_exhausted', 429, 'quota_exhausted'),
    ('insufficient_quota', 429, 'quota_exhausted'),
    ('rate_limit_exceeded', 429, 'rate_limited'),
    ('invalid_api_key', 401, 'authentication_failed'),
    ('model_not_found', 404, 'model_unavailable'),
    (None, 500, 'service_error'),
])
def test_failure_reason_is_actionable_and_does_not_expose_provider_body(monkeypatch, code, status, category):
    api_failure(monkeypatch, code, status)
    result = ai_parser.parse_business_description(QUESTION)
    assert result['ai_status'] == 'failed'
    assert result['ai_error_code'] == category
    assert result['source'] == 'fallback_after_ai_error_v20'
    assert result['anomaly_rules'] == ['ad_spend_without_sales']
    assert result['query_mode'] == 'data_anomaly_query'
    assert result['aggregation_dimensions'] == ['marketplace', 'store', 'sku', 'day']
    assert 'secret-key' not in json.dumps(result)
    assert any('草稿' in text for text in result['warnings'])
    if category == 'quota_exhausted':
        assert any('额度' in text for text in result['warnings'])


def test_reported_question_parses_exact_window_rule_and_nonempty_grain(monkeypatch):
    monkeypatch.setattr(parser_rules, 'today_est', lambda: date(2026, 10, 21))
    result = parser_rules.fallback_parse(QUESTION)
    assert (result['start_date'], result['end_date']) == ('2026-10-20', '2026-10-21')
    assert result['anomaly_rules'] == ['ad_spend_without_sales']
    assert result['aggregation_dimensions_source'] == 'derived_from_rule_grain'
    AnomalyQueryRequest(**{key: result[key] for key in ('start_date', 'end_date', 'anomaly_rules', 'aggregation_dimensions')})


def test_no_ad_spend_is_not_the_positive_ad_spend_rule():
    result = parser_rules.fallback_parse('查找昨天没有广告花费但没有销售的产品')
    assert 'ad_spend_without_sales' not in result['anomaly_rules']


def test_default_dimensions_match_declared_default_without_overriding_ai_dimensions():
    local = parser_rules.fallback_parse('查询昨天的数据')
    assert local['aggregation_dimensions'] == ['main_ir']
    assert local['aggregation_dimensions_source'] == 'default_main_ir_when_empty'
    merged = ai_parser._merge_rule_overrides('查昨天各店铺的数据', {
        'aggregation_dimensions': ['store'], 'aggregation_dimensions_source': 'user_specified',
    })
    assert merged['aggregation_dimensions'] == ['store']
    specified = ai_parser._merge_rule_overrides('按 SKU 汇总', {'aggregation_dimensions': ['main_ir']})
    assert specified['aggregation_dimensions'] == ['sku']
    assert specified['aggregation_dimensions_source'] == 'user_specified'


@pytest.mark.parametrize('content,finish', [('not-json', 'stop'), ('{}', 'stop'), ('[]', 'stop'), ('{"start_date":"2026-10-20"}', 'length')])
def test_empty_or_truncated_response_is_not_ai_success(monkeypatch, content, finish):
    import openai
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)])
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response))))
    result = ai_parser.ai_parse(QUESTION)
    assert result['ai_status'] == 'failed' and result['ai_error_code'] == 'invalid_response'
    assert result['anomaly_rules'] == ['ad_spend_without_sales']


def test_page_shows_quota_reason_and_keeps_recognized_rule(tmp_path, monkeypatch):
    api_failure(monkeypatch)
    monkeypatch.setenv('PROFIT_STATE_DB', str(tmp_path / 'state.sqlite3'))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py')).run()
    next(r for r in app.radio if r.label == '工作区').set_value('业务问题定位').run()
    next(t for t in app.text_area if t.label == '业务问题').set_value(QUESTION)
    next(b for b in app.button if b.label == '解析为查询草稿').click().run()
    assert not app.exception
    assert any('额度' in w.value for w in app.warning)
    assert next(m for m in app.multiselect if m.label == '检查规则（留空只查询）').value == ['ad_spend_without_sales']
    assert next(m for m in app.multiselect if m.label == '汇总维度').value == ['marketplace', 'store', 'sku', 'day']
