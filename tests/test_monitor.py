from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from bi_check_agent.monitor import MonitorStore
from bi_check_agent.notifications import DeliveryUncertain


def plan(store, **kw):
    return store.save_plan({'id': 'p', 'mode': 'live', 'shadow': False, **kw})


def evaluation(value=10, threshold=None):
    finding = {'key': 'order1', 'rule': 'r', 'object': 'order1', 'value': value, 'evidence': {}}
    if threshold is not None:
        finding['worsening_delta'] = threshold
    return lambda *_: {'findings': [finding], 'evidence': {'complete': True}}


def clean(*_):
    return {'findings': [], 'evidence': {'complete': True}}


def test_lifecycle_and_scope(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    p = plan(s)
    run = lambda fn, cfg=p, start='2026-09-01': s.run(cfg, start, (date.fromisoformat(start) + timedelta(days=1)).isoformat(), fn)
    assert run(evaluation())['events'][0]['type'] == 'new'
    assert run(evaluation(11))['events'] == []
    assert run(evaluation(12, 5))['events'] == []
    assert run(evaluation(15, 5))['events'][0]['type'] == 'worsening'
    assert run(clean, start='2026-09-02')['events'] == []
    assert run(clean, cfg={**p, 'version': 2})['events'] == []
    assert s.incidents()[0]['status'] == 'open'
    assert run(clean)['events'][0]['type'] == 'recovered'
    assert run(evaluation())['events'][0]['type'] == 'recurrence'
    detail = s.incident(s.incidents()[0]['id'])
    assert len(detail['history']) == 4
    assert detail['run_id'] == detail['evidence']['run_id'] == detail['history'][-1]['run_id']
    assert len(s.outbox()) == 4
    assert MonitorStore(tmp_path / 'db').incidents() == s.incidents()


def test_failure_does_not_recover_and_sanitizes(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    p = plan(s)
    s.run(p, '2026-09-01', '2026-09-02', evaluation())
    def fail(*_):
        raise RuntimeError('password=supersecret')
    r = s.run(p, '2026-09-01', '2026-09-02', fail)
    assert r['status'] == 'failed'
    assert s.incidents()[0]['status'] == 'open'
    assert 'supersecret' not in str(s.runs()) + str(s.outbox())
    assert s.run(p, '2026-09-01', '2026-09-02', fail)['events'] == []
    assert s.run(p, '2026-09-01', '2026-09-02', evaluation())['events'][0]['type'] == 'run_recovered'


def test_demo_shadow_and_delivery(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    for cfg in ({'mode': 'demo'}, {'shadow': True}):
        s.run(plan(s, **cfg), '2026-09-01', '2026-09-02', evaluation())
    assert not s.outbox()
    s.run(plan(s, version=2), '2026-09-01', '2026-09-02', evaluation())
    class Sender:
        enabled = True
        calls = 0
        def send(self, item):
            self.calls += 1
            raise DeliveryUncertain()
    sender = Sender()
    assert s.dispatch(sender)[0]['status'] == 'uncertain'
    assert s.dispatch(sender) == []
    assert sender.calls == 1


def test_lease_and_expired_retry(tmp_path):
    s = MonitorStore(tmp_path / 'db', lease_seconds=300)
    p = plan(s)
    def nested(*_):
        assert s.run(p, '2026-09-01', '2026-09-02', clean)['status'] == 'running'
        return clean()
    assert s.run(p, '2026-09-01', '2026-09-02', nested)['attempts'] == 1
    with s._db() as db:
        db.execute("UPDATE runs SET status='running',lease_until=0")
    assert s.run(p, '2026-09-01', '2026-09-02', clean)['attempts'] == 2


def test_daily_new_york_windows(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    plan(s, enabled=True, lookback_days=2, hour=9, minute=0)
    now = datetime(2026, 9, 9, 10, tzinfo=ZoneInfo('America/New_York'))
    assert [(a, b) for _, a, b in s.due_windows(now)] == [('2026-09-07', '2026-09-08'), ('2026-09-08', '2026-09-09')]
    assert s.due_windows(now.replace(hour=8)) == []


def test_rejected_incomplete_never_recovers(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    p = plan(s)
    s.run(p, '2026-09-01', '2026-09-02', evaluation())
    result = s.run(p, '2026-09-01', '2026-09-02', lambda *_: {'findings': [], 'evidence': {'complete': False}})
    assert result['status'] == 'failed'
    assert s.incidents()[0]['status'] == 'open'


def test_failed_delivery_retries_after_restart_without_reverting_incident(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    s.run(plan(s), '2026-09-01', '2026-09-02', evaluation())
    class Sender:
        enabled = True
        fail = True
        ids = []
        def send(self, item):
            self.ids.append(item['uuid'])
            if self.fail:
                raise RuntimeError('rejected')
    sender = Sender()
    assert s.dispatch(sender)[0]['status'] == 'failed'
    sender.fail = False
    restarted = MonitorStore(tmp_path / 'db')
    assert restarted.dispatch(sender)[0]['status'] == 'sent'
    assert sender.ids[0] == sender.ids[1]
    assert restarted.incidents()[0]['status'] == 'open'
    assert restarted.dispatch(sender) == []


def test_feishu_timeout_is_uncertain(monkeypatch):
    import requests
    from bi_check_agent.notifications import FeishuSender
    for k, v in {'FEISHU_ENABLED': 'true', 'FEISHU_RECEIVER_CONFIRMED': 'Yuxi', 'FEISHU_APP_ID': 'test', 'FEISHU_APP_SECRET': 'test', 'FEISHU_RECEIVER_OPEN_ID': 'ou_test', 'MONITOR_PUBLIC_URL': 'https://internal.example.test'}.items():
        monkeypatch.setenv(k, v)
    class Auth:
        def raise_for_status(self): pass
        def json(self): return {'code': 0, 'tenant_access_token': 'fake'}
    def post(url, **kwargs):
        if 'auth/' in url:
            return Auth()
        assert kwargs['json']['receive_id'] == 'ou_test'
        assert kwargs['json']['uuid'] == 'stable-test'
        raise requests.Timeout()
    monkeypatch.setattr(requests, 'post', post)
    import pytest
    with pytest.raises(DeliveryUncertain):
        FeishuSender().send({'mode': 'live', 'shadow': False, 'events': [], 'uuid': 'stable-test'})


def test_rule_source_edits_do_not_recover_previous_scope(tmp_path, monkeypatch):
    import bi_check_agent.monitor as monitor
    s = MonitorStore(tmp_path / 'db')
    p = plan(s)
    monkeypatch.setattr(monitor, 'rule_source_version', lambda: 'version-one')
    s.run(p, '2026-09-01', '2026-09-02', evaluation())
    original_id = s.incidents()[0]['id']
    monkeypatch.setattr(monitor, 'rule_source_version', lambda: 'version-two')
    assert s.run(p, '2026-09-01', '2026-09-02', clean)['events'] == []
    assert s.incident(original_id)['status'] == 'open'
    assert s.open_windows(p) == []


def test_real_demo_service_and_worker_daily_dedup(tmp_path):
    from bi_check_agent.worker import tick
    s = MonitorStore(tmp_path / 'db')
    p = plan(s, mode='demo', enabled=True, hour=0, minute=0, rules=['missing_product_cost'])
    first = tick(s)
    assert len(first) == 1
    assert first[0]['status'] == 'succeeded'
    assert first[0]['evidence']['row_count'] > 0
    assert any(e['type'] == 'new' for e in first[0]['events'])
    assert s.incidents()
    assert not s.outbox()
    assert tick(s) == []


def test_rule_change_during_evaluation_fails_without_recovery(tmp_path, monkeypatch):
    import bi_check_agent.monitor as monitor
    s = MonitorStore(tmp_path / 'db')
    p = plan(s)
    monkeypatch.setattr(monitor, 'rule_source_version', lambda: 'first')
    s.run(p, '2026-09-01', '2026-09-02', evaluation())
    def changes(*_):
        monkeypatch.setattr(monitor, 'rule_source_version', lambda: 'second')
        return clean()
    assert s.run(p, '2026-09-01', '2026-09-02', changes)['status'] == 'failed'
    assert s.incidents()[0]['status'] == 'open'


def test_missing_complete_never_recovers(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    p = plan(s)
    s.run(p, '2026-09-01', '2026-09-02', evaluation())
    for evidence in ({}, {'complete': None}, {'complete': 1}):
        result = s.run(p, '2026-09-01', '2026-09-02', lambda *_: {'findings': [], 'evidence': evidence})
        assert result['status'] == 'failed'
        assert s.incidents()[0]['status'] == 'open'


def test_evidence_mode_and_window_must_match_when_provided(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    p = plan(s)
    s.run(p, '2026-09-01', '2026-09-02', evaluation())
    for evidence in ({'complete': True, 'mode': 'demo'}, {'complete': True, 'start': '2026-08-31'}, {'complete': True, 'end': '2026-09-03'}):
        result = s.run(p, '2026-09-01', '2026-09-02', lambda *_: {'findings': [], 'evidence': evidence})
        assert result['status'] == 'failed'
        assert s.incidents()[0]['status'] == 'open'


def test_state_path_uses_shared_profit_environment(tmp_path, monkeypatch):
    path = tmp_path / 'shared.sqlite3'
    monkeypatch.setenv('PROFIT_STATE_DB', str(path))
    s = MonitorStore()
    assert s.path == str(path)
    p = plan(s)
    assert MonitorStore(path).plans() == [p]


def test_failure_recovery_is_bound_to_same_window(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    p = plan(s)
    def fail(*_):
        raise RuntimeError('unavailable')
    a = s.run(p, '2026-09-01', '2026-09-02', fail)
    assert a['events'][0]['type'] == 'run_failed'
    b = s.run(p, '2026-09-02', '2026-09-03', clean)
    assert not b['events']
    assert s.run(p, '2026-09-01', '2026-09-02', clean)['events'][0]['type'] == 'run_recovered'


def test_outbox_dispatch_preserves_enqueue_order_after_retry(tmp_path):
    s = MonitorStore(tmp_path / 'db')
    p = plan(s)
    s.run(p, '2026-09-01', '2026-09-02', evaluation())
    s.run(p, '2026-09-01', '2026-09-02', clean)
    queued = s.outbox()
    with s._db() as db:
        db.execute("UPDATE outbox SET status='failed', updated=updated+1000 WHERE id=?", (queued[0]['id'],))
    class Sender:
        enabled = True
        events = []
        def send(self, item):
            self.events.extend(e['type'] for e in item['events'])
    sender = Sender()
    assert [r['status'] for r in s.dispatch(sender)] == ['sent', 'sent']
    assert sender.events == ['new', 'recovered']
