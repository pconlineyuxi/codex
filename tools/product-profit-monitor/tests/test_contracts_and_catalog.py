"""Behavioral checks for the shared rule and result boundaries."""
import json
from pathlib import Path

import pytest
import yaml

from bi_check_agent import core, service, parser_schema, profit_analysis
from bi_check_agent.contracts import AnalysisSnapshot, QueryResult, analysis_status
from bi_check_agent.models import AnomalyQueryRequest, ALLOWED_ANOMALY_RULES
from bi_check_agent.rule_catalog import RULES, RULE_IDS


def request(**kwargs):
    return AnomalyQueryRequest(start_date='2026-09-01', end_date='2026-09-03', **kwargs)


CASES = {
    'zero_units_with_sales': {'units': 0},
    'zero_sales_with_units': {'sales': 0},
    'negative_units': {'units': -1},
    'negative_sales': {'sales': -1},
    'negative_shipping_fee': {'shipping_fee': -1},
    'negative_promotion': {'raw_promotion': -1},
    'negative_commission': {'commission': -1},
    'negative_ad_spend': {'ad_spend': -1},
    'missing_product_cost': {'product_cost_unit': None},
    'unit_cost_non_positive': {'product_cost_unit': 0, 'hardware_cost': 0},
    'removed_hardware_abnormal': {'removed_hardware': 1000},
    'shipping_cost_ratio_high': {'shipping_fee': 1000},
    'unit_shipping_fee_high': {'shipping_fee': 200},
    'promotion_ratio_high': {'promo_cost': 1000},
    'ad_spend_ratio_high': {'ad_spend': 1000},
    'ad_spend_without_sales': {'ad_spend': 10, 'sales': 0},
    'profit_rate_with_ads_ship_high': {'commission': -2000},
}


def test_every_catalog_rule_has_a_real_behavior_case_and_all_consumers_agree():
    assert set(CASES) == set(RULE_IDS) == ALLOWED_ANOMALY_RULES == set(parser_schema.ANOMALY_RULES)
    assert set(core.ANOMALY_RULE_LABELS) == set(service.RULE_LABELS) == set(RULE_IDS)
    assert list(RULE_IDS) == list(service.RULE_LABELS)  # UI order is stable.
    for key, rule in RULES.items():
        assert parser_schema.RULE_ALIASES[key] == list(rule.aliases)
        assert parser_schema.sanitize_parser_result({'anomaly_rules': [key]})['anomaly_rules'] == [key]
        assert request(anomaly_rules=[key]).anomaly_rules == [key]


@pytest.mark.parametrize('rule,changes', CASES.items())
def test_catalog_rules_are_executable_and_keep_positive_negative_cases(rule, changes):
    rows = service.demo_rows(request(sku=['DEMO-HEALTHY'])).iloc[:1].copy()
    assert core.detect_anomalies(rows, [rule]).empty
    for field, value in changes.items():
        rows[field] = value
    hit = core.detect_anomalies(rows, [rule])
    assert len(hit) == 1 and hit.iloc[0]['anomaly_rules'] == rule


def test_missing_cost_is_a_finding_not_an_unknown_operand():
    rows = service.demo_rows(request(sku=['DEMO-COST']))
    assert not service.rule_unknowns(rows, ['missing_product_cost'])
    assert service.rule_unknowns(rows, ['unit_cost_non_positive'])[0]['field'] == 'product_cost_unit'


def test_catalog_edit_cannot_mix_new_evidence_with_loaded_old_rules(tmp_path, monkeypatch):
    from bi_check_agent import rule_catalog
    config = core.load_yaml('diagnostic_rules.yaml')
    config['queryable_rules']['negative_units']['required_fields'].append('sales')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/diagnostic_rules.yaml').write_text(yaml.safe_dump(config))
    monkeypatch.setattr(rule_catalog, '__file__', str(tmp_path / 'bi_check_agent/rule_catalog.py'))
    with pytest.raises(RuntimeError, match='重启'):
        service.query(request())


def test_query_result_validates_scope_count_and_does_not_copy_source_frame():
    result = service.query(request())
    model = QueryResult.model_validate(result)
    assert model.as_dict()['rows'] is result['rows']
    json.dumps(model.as_dict()['evidence'], allow_nan=False)
    for change in ({'row_count': 999}, {'start': '2026-08-01'}, {'query_complete': False}, {'complete': 1}):
        with pytest.raises(ValueError) as error:
            QueryResult.model_validate({**result, 'evidence': {**result['evidence'], **change}})
        assert 'input_value' not in str(error.value) and 'DEMO-' not in str(error.value)


def test_scans_and_comparisons_share_shape_but_keep_full_separate_source_periods():
    req = request(sku=['DEMO-HEALTHY'])
    result = service.query(req)
    scan = profit_analysis.scan_snapshot({'id': 'scan', 'evidence': result['evidence'], 'summary': []}, req, result)
    previous = AnomalyQueryRequest(**{**req.model_dump(mode='json'), 'start_date': '2026-08-30', 'end_date': '2026-09-01'})
    comparison = profit_analysis.compare(req, previous, 'demo')
    assert set(scan) == set(comparison)
    assert scan['_source_frames']['current'] is result['rows']
    assert set(scan['_source_frames']) == {'current'}
    assert set(comparison['_source_frames']) == {'current', 'previous'}
    assert scan['previous_request'] is None and scan['report'] is None
    for snapshot in (scan, comparison):
        model = AnalysisSnapshot.model_validate(snapshot)
        exported = model.public_dict()
        assert '_source_frames' not in exported
        json.dumps(exported, allow_nan=False)
        assert analysis_status(snapshot, 'demo') == 'ready'
        assert analysis_status(snapshot, 'live') == 'missing'
        assert analysis_status(snapshot, 'demo', record_id='other') == 'missing'
        assert analysis_status(snapshot, 'demo', failed=True) == 'stale'
        assert analysis_status(snapshot, 'demo', scope_matches=False) == 'stale'
        assert analysis_status(exported, 'demo') == 'missing'


def test_snapshot_rejects_wrong_evidence_and_missing_source_period():
    req = request()
    result = service.query(req)
    record = {'id': 'scan', 'evidence': {**result['evidence'], 'checked_at': 'different-run'}, 'summary': []}
    with pytest.raises(ValueError, match='同一份证据'):
        profit_analysis.scan_snapshot(record, req, result)
    record['evidence'] = result['evidence']
    snapshot = profit_analysis.scan_snapshot(record, req, result)
    with pytest.raises(ValueError):
        AnalysisSnapshot.model_validate({**snapshot, '_source_frames': {'previous': result['rows']}})
    with pytest.raises(ValueError):
        AnalysisSnapshot.model_validate({**snapshot, 'current_days': 3})


def test_pre_refactor_demo_sql_frames_findings_and_profit_are_unchanged(monkeypatch):
    """Golden data was captured before editing, not regenerated from new code."""
    baseline = json.loads((Path(__file__).parent / 'fixtures/pre_refactor_demo.json').read_text())
    monkeypatch.setenv('MAX_RESULT_ROWS', '50000')
    monkeypatch.setenv('MAX_QUERY_DAYS', '366')
    req = AnomalyQueryRequest(**baseline['request'])
    result = service.query(req)
    assert {key: service.records(result[key]) for key in ('rows', 'aggregate', 'anomalies')} == baseline['frames']
    assert list(core.build_order_line_sql(req)) == baseline['sql']
    assert service.RULE_LABELS == baseline['labels']
    assert result['evidence']['unknown_rule_inputs'] == baseline['unknowns']
    current = request(sku=['DEMO-HEALTHY'], anomaly_rules=req.anomaly_rules,
                      aggregation_dimensions=req.aggregation_dimensions)
    previous = AnomalyQueryRequest(**{**current.model_dump(mode='json'), 'start_date': '2026-08-30', 'end_date': '2026-09-01'})
    assert profit_analysis.compare(current, previous, 'demo')['report'] == baseline['comparison']
