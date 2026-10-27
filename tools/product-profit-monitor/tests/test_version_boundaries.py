"""Policy isolation, build continuity and non-destructive legacy adoption."""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from bi_check_agent import monitor, versioning
from bi_check_agent.monitor import MonitorStore

START, END = '2026-09-01', '2026-09-02'


def evaluation(*_):
    return {'findings': [{'key': 'order1', 'rule': 'missing_product_cost', 'object': 'item',
                          'value': 1, 'evidence': {}}], 'evidence': {'complete': True}}


def clean(*_):
    return {'findings': [], 'evidence': {'complete': True}}


def plan(store):
    return store.save_plan({'id': 'p', 'mode': 'live', 'shadow': False, 'enabled': True,
                           'hour': 0, 'rules': ['missing_product_cost']})


def test_only_semantic_config_changes_change_policy_identity(tmp_path, monkeypatch):
    root = Path(versioning.__file__).resolve().parents[1]
    (tmp_path / 'config').mkdir()
    (tmp_path / 'bi_check_agent').mkdir()
    for name in ('diagnostic_rules.yaml', 'filter_mapping.yaml'):
        (tmp_path / 'config' / name).write_text((root / 'config' / name).read_text())
    code = tmp_path / 'bi_check_agent/core.py'
    code.write_text('value = 1\n')
    monkeypatch.setattr(versioning, '__file__', str(tmp_path / 'bi_check_agent/versioning.py'))
    policy, build = versioning.diagnostic_policy_version(), versioning.rule_source_version()
    code.write_text('value = 1\n# operational note\n')
    assert versioning.rule_source_version() != build
    assert versioning.diagnostic_policy_version() == policy
    cfg_path = tmp_path / 'config/diagnostic_rules.yaml'
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg['queryable_rules']['missing_product_cost']['label'] = 'new display label'
    cfg['queryable_rules']['missing_product_cost']['aliases'].append('new alias')
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=True))
    assert versioning.diagnostic_policy_version() == policy
    cfg['thresholds']['shipping_cost_ratio_high'] = 0.6
    cfg_path.write_text(yaml.safe_dump(cfg))
    changed = versioning.diagnostic_policy_version()
    assert changed != policy
    mapping_path = tmp_path / 'config/filter_mapping.yaml'
    mapping = yaml.safe_load(mapping_path.read_text())
    mapping['filters']['store']['sql_field'] = 'another_store'
    mapping_path.write_text(yaml.safe_dump(mapping))
    assert versioning.diagnostic_policy_version() != changed
    changed = versioning.diagnostic_policy_version()
    monkeypatch.setattr(versioning, 'POLICY_REVISION', versioning.POLICY_REVISION + 1)
    assert versioning.diagnostic_policy_version() != changed


def test_build_change_keeps_id_and_history_but_forces_recheck(tmp_path, monkeypatch):
    store = MonitorStore(tmp_path / 'db')
    p = plan(store)
    monkeypatch.setattr(monitor, 'rule_source_version', lambda: 'build-a')
    first = store.run(p, START, END, evaluation)
    incident = store.incidents()[0]
    # Same-day success deduplicates until a new build requires revalidation.
    now = datetime.fromtimestamp(first['updated'], ZoneInfo('America/New_York'))
    assert not any(start == START for _, start, _ in store.due_windows(now))
    monkeypatch.setattr(monitor, 'rule_source_version', lambda: 'build-b')
    assert (p, START, END) in store.due_windows(now)
    second = store.run(p, START, END, evaluation)
    assert second['id'] == first['id'] and second['attempts'] == 2
    assert not second['events']
    assert store.incidents()[0]['id'] == incident['id']
    assert store.incidents()[0]['first_seen'] == incident['first_seen']
    assert len(store.outbox()) == 1
    recovered = store.run(p, START, END, clean)
    assert recovered['events'][0]['incident_id'] == incident['id']
    assert store.incident(incident['id'])['status'] == 'recovered'
    assert len(store.incident(incident['id'])['history']) == 2


def legacy_store(tmp_path, monkeypatch, *, build=None):
    """Seed the old key layout, then remove fields absent in the old schema."""
    store = MonitorStore(tmp_path / 'db')
    p = plan(store)
    build = build or versioning.LEGACY_SOURCE_VERSIONS[0]
    old_scope = monitor._key({'plan': monitor._scope_config(p), 'rule_source_version': build})
    with monkeypatch.context() as patch:
        patch.setattr(monitor, 'scope_key', lambda *_: old_scope)
        patch.setattr(monitor, 'compatible_legacy_versions', lambda *_: ())
        patch.setattr(monitor, 'rule_source_version', lambda: build)
        run = store.run(p, START, END, evaluation)
    with store._db() as db:
        # An existing installation does not have an alias table until upgraded.
        db.execute('DROP TABLE scope_aliases')
        for table in ('runs', 'incidents'):
            for row in db.execute(f'SELECT id,body FROM {table}').fetchall():
                body = json.loads(row['body'])
                body.pop('policy_version', None)
                db.execute(f'UPDATE {table} SET body=? WHERE id=?', (monitor._json(body), row['id']))
    return store, p, run, old_scope


@pytest.mark.parametrize('build', versioning.LEGACY_SOURCE_VERSIONS)
def test_legacy_adoption_preserves_links_history_attempts_and_delivery_ids(tmp_path, monkeypatch, build):
    # This test exercises equivalent builds under the original business policy.
    monkeypatch.setattr(versioning, 'POLICY_REVISION', 1)
    assert versioning.diagnostic_policy_version() == versioning.LEGACY_POLICY_VERSION
    old, p, first, scope = legacy_store(tmp_path, monkeypatch, build=build)
    incident, queued = old.incidents()[0], old.outbox()
    store = MonitorStore(old.path)
    assert store.open_windows(p) == [(START, END)]
    assert store.scope_for(p) == scope
    assert store.outbox() == queued
    result = store.run(p, START, END, evaluation)
    assert result['id'] == first['id'] and result['attempts'] == 2
    assert result['events'] == []
    assert store.incidents()[0]['id'] == incident['id']
    assert len(store.incident(incident['id'])['history']) == 1
    assert store.outbox() == queued
    restarted = MonitorStore(old.path)
    assert restarted.scope_for(p) == scope
    assert restarted.run(p, START, END, clean)['events'][0]['type'] == 'recovered'
    assert restarted.incident(incident['id'])['status'] == 'recovered'
    with restarted._db() as db:
        assert db.execute('SELECT count(*) FROM scope_aliases').fetchone()[0] == 1


def test_legacy_live_lease_and_failed_window_survive_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(versioning, 'POLICY_REVISION', 1)
    old, p, run, _ = legacy_store(tmp_path, monkeypatch)
    with old._db() as db:
        db.execute("UPDATE runs SET status='running',lease_until=? WHERE id=?", (run['updated'] + 300, run['id']))
    new = MonitorStore(old.path)
    called = []
    result = new.run(p, START, END, lambda *_: called.append(True))
    assert result['status'] == 'running' and not called
    with new._db() as db:
        db.execute("UPDATE runs SET status='failed',lease_until=0,updated=0")
        db.execute("UPDATE incidents SET status='recovered'")
        db.execute('INSERT OR REPLACE INTO failures VALUES(?,?,?)', (run['id'], 1, 2))
    now = datetime(2026, 10, 21, 10, tzinfo=ZoneInfo('America/New_York'))
    assert (p, START, END) in new.due_windows(now)
    recovered = new.run(p, START, END, evaluation)
    assert recovered['id'] == run['id']
    assert any(e['type'] == 'run_recovered' and e['sequence'] == 2 for e in recovered['events'])


@pytest.mark.parametrize('change', ['policy', 'plan', 'unknown_build'])
def test_incompatible_history_is_never_silently_adopted_or_recovered(tmp_path, monkeypatch, change):
    old, p, _, scope = legacy_store(tmp_path, monkeypatch, build='unknown' if change == 'unknown_build' else None)
    incident_id = old.incidents()[0]['id']
    if change == 'policy':
        monkeypatch.setattr(monitor, 'diagnostic_policy_version', lambda: 'new-business-policy')
    elif change == 'plan':
        p = {**p, 'filters': {'store': ['Different Store']}}
    store = MonitorStore(old.path)
    assert store.scope_for(p) != scope
    assert store.open_windows(p) == []
    assert store.run(p, START, END, clean)['events'] == []
    assert store.incident(incident_id)['status'] == 'open'


def test_policy_change_during_run_fails_without_recovering(tmp_path, monkeypatch):
    store = MonitorStore(tmp_path / 'db')
    p = plan(store)
    store.run(p, START, END, evaluation)
    def changed(*_):
        monkeypatch.setattr(monitor, 'diagnostic_policy_version', lambda: 'changed')
        return clean()
    assert store.run(p, START, END, changed)['status'] == 'failed'
    assert store.incidents()[0]['status'] == 'open'
