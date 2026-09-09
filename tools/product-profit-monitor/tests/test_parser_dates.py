from datetime import date
from types import SimpleNamespace
import json
import pytest
from bi_check_agent import ai_parser, parser_rules

@pytest.mark.parametrize('text,start,end', [
    ('今年是哪个日期范围？', '2026-01-01', '2027-01-01'),
    ('查询今年 SKU ABC123 的数据', '2026-01-01', '2027-01-01'),
    ('查询今年的数据', '2026-01-01', '2027-01-01'),
    ('this year', '2026-01-01', '2027-01-01'),
    ('去年', '2025-01-01', '2026-01-01'),
    ('今年至今', '2026-01-01', '2026-09-10'),
    ('今年八月', '2026-08-01', '2026-09-01'),
    ('昨天', '2026-09-08', '2026-09-09'),
])
def test_relative_dates(text, start, end):
    result = parser_rules.parse_time_range(text, date(2026, 9, 9))
    assert tuple(d.isoformat() for d in result[:2]) == (start, end)


def test_ai_gets_current_date_and_wrong_year_is_overridden(monkeypatch):
    import openai
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    monkeypatch.setattr(parser_rules, 'today_est', lambda: date(2026, 9, 9))
    monkeypatch.setattr(ai_parser, 'today_est', lambda: date(2026, 9, 9))
    def create(**kwargs):
        assert kwargs['model'] == 'gpt-5.4'
        assert '2026-09-09' in kwargs['messages'][0]['content']
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'start_date':'2023-01-01','end_date':'2024-01-01'})))])
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    result = ai_parser.ai_parse('查询今年的数据')
    assert result['start_date'] == '2026-01-01'
    assert result['end_date'] == '2027-01-01'
    assert result['time_range']['start_date'] == result['start_date']
    assert result['source'] == 'openai_with_rule_validation_v20'


def test_year_rollover():
    assert parser_rules.parse_time_range('今年', date(2027,1,1))[:2] == (date(2027,1,1), date(2028,1,1))


@pytest.mark.parametrize('phrase',['售价为0','售价为 0','售价是0','售价等于零','售价=0'])
def test_zero_price_business_alias(phrase):
    result=parser_rules.fallback_parse('查昨天'+phrase+'的数据，按SKU汇总')
    assert 'zero_sales_with_units' in result['anomaly_rules']
    assert any('sales = 0' in note and 'units != 0' in note for note in result['notes'])


def test_recognized_rule_clears_only_its_false_warning():
    result=ai_parser._merge_rule_overrides('查昨天售价为0的数据', {
        'warnings':['售价为0不在允许的 anomaly_rules 列表中，无法映射。','未支持仓库筛选'],
        'unresolved_terms':['售价为0无法映射','仓库'],
        'missing_required_fields':['anomaly_rules'],
    })
    assert result['anomaly_rules']==['zero_sales_with_units']
    assert result['warnings']==['未支持仓库筛选']
    assert result['unresolved_terms']==['仓库']
    assert 'anomaly_rules' not in result['missing_required_fields']


def test_nonzero_price_is_not_zero_price_rule():
    assert 'zero_sales_with_units' not in parser_rules.fallback_parse('售价为0.5')['anomaly_rules']
