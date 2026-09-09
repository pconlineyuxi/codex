"""Durable single-host monitoring state; no business DB or network inside transactions."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import sqlite3
import time
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _key(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def rule_source_version():
    return hashlib.sha256((Path(__file__).resolve().parents[1] / 'config' / 'diagnostic_rules.yaml').read_bytes()).hexdigest()


def scope_key(plan):
    config = {k: v for k, v in plan.items() if k not in {'name', 'enabled', 'shadow', 'hour', 'minute', 'lookback_days'}}
    return _key({'plan': config, 'rule_source_version': rule_source_version()})


class MonitorStore:
    def __init__(self, db_path=None, lease_seconds=300):
        self.path = str(db_path or os.getenv('PROFIT_STATE_DB', '.runtime/monitor.sqlite3'))
        self.lease_seconds = lease_seconds
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, plan_id TEXT, scope TEXT, window_start TEXT, window_end TEXT, status TEXT, attempts INTEGER, lease_until REAL, token TEXT, updated REAL, body TEXT, UNIQUE(scope,window_start,window_end));
            CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, scope TEXT, window_start TEXT, window_end TEXT, status TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, status TEXT, body TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS failures(scope TEXT PRIMARY KEY, active INTEGER, sequence INTEGER);
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=30000')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def save_plan(self, plan):
        p = dict(plan)
        p.setdefault('id', str(uuid.uuid4()))
        for k, v in dict(name='Product Profit', mode='demo', enabled=False, shadow=True, hour=9, minute=0, lookback_days=1, rules=[], filters={}, version=1).items():
            p.setdefault(k, v)
        if p['mode'] not in ('demo', 'live') or not 0 <= int(p['hour']) <= 23 or not 0 <= int(p['minute']) <= 59 or not 1 <= int(p['lookback_days']) <= 366:
            raise ValueError('Invalid plan mode or schedule')
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO plans VALUES(?,?)', (p['id'], _json(p)))
        return p

    def plans(self):
        with self._db() as db:
            return [json.loads(r['body']) for r in db.execute('SELECT body FROM plans ORDER BY id')]

    def runs(self):
        with self._db() as db:
            return [self._run_row(r) for r in db.execute('SELECT * FROM runs ORDER BY updated DESC')]

    @staticmethod
    def _run_row(row):
        r = dict(row)
        body = json.loads(r.pop('body') or '{}')
        r.pop('token', None)
        return {**body, **r}

    def incidents(self):
        with self._db() as db:
            return [json.loads(r['body']) for r in db.execute('SELECT body FROM incidents ORDER BY rowid DESC')]

    def incident(self, incident_id):
        with self._db() as db:
            row = db.execute('SELECT body FROM incidents WHERE id=?', (incident_id,)).fetchone()
            if not row:
                return None
            return {**json.loads(row['body']), 'history': [json.loads(r['body']) for r in db.execute('SELECT body FROM history WHERE incident_id=? ORDER BY id', (incident_id,))]}

    def outbox(self):
        with self._db() as db:
            return [{**json.loads(r['body']), 'id': r['id'], 'status': r['status']} for r in db.execute('SELECT * FROM outbox ORDER BY rowid ASC')]

    def _queue(self, db, plan, run_id, events, evidence):
        if not events or plan.get('mode') != 'live' or plan.get('shadow', True):
            return
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _json([run_id, events])))
        body = {'plan_id': plan['id'], 'plan_name': plan.get('name'), 'mode': plan['mode'], 'shadow': False, 'run_id': run_id, 'events': events, 'evidence': evidence, 'uuid': event_id}
        db.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?,?)', (event_id, 'pending', _json(body), time.time()))

    def run(self, plan, window_start, window_end, evaluate):
        # ISO dates are canonical keys; each window is independently complete.
        from datetime import date
        start, end = date.fromisoformat(window_start), date.fromisoformat(window_end)
        if start >= end:
            raise ValueError('Window end is exclusive and must be after start')
        source_version = rule_source_version()
        scope = scope_key(plan)
        run_id = _key([scope, window_start, window_end])
        token, now = str(uuid.uuid4()), time.time()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if old and old['status'] == 'running' and old['lease_until'] > now:
                return self._run_row(old)
            attempts = old['attempts'] + 1 if old else 1
            previous = json.loads(old['body'] or '{}') if old else {}
            db.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)', (run_id, plan['id'], scope, window_start, window_end, 'running', attempts, now + self.lease_seconds, token, now, _json(previous)))
        stopped = threading.Event()
        def renew_lease():
            while not stopped.wait(max(0.1, self.lease_seconds / 3)):
                try:
                    with self._db() as db:
                        changed = db.execute("UPDATE runs SET lease_until=? WHERE id=? AND token=? AND status='running'", (time.time() + self.lease_seconds, run_id, token)).rowcount
                        if not changed:
                            return
                except sqlite3.Error:
                    return  # The fencing token still prevents a stale result from committing.
        heartbeat = threading.Thread(target=renew_lease, daemon=True)
        heartbeat.start()
        try:
            result = evaluate(plan, window_start, window_end)
            if not isinstance(result, dict) or not isinstance(result.get('findings'), list) or not isinstance(result.get('evidence'), dict):
                raise ValueError('Invalid evaluator result')
            if result['evidence'].get('complete') is not True or result.get('status') in ('incomplete', 'failed'):
                raise ValueError('Incomplete evaluation')
            for field, expected in (('mode', plan['mode']), ('start', window_start), ('end', window_end)):
                if field in result['evidence'] and result['evidence'][field] != expected:
                    raise ValueError('Evaluation evidence scope does not match requested window or mode')
            evaluated_version = result['evidence'].get('rule_version')
            if rule_source_version() != source_version or (evaluated_version and not source_version.startswith(evaluated_version)):
                raise ValueError('Rule configuration changed during evaluation')
            seen = set()
            for f in result['findings']:
                for field in ('key', 'rule', 'object', 'value', 'evidence'):
                    if field not in f:
                        raise ValueError('Incomplete finding')
                if not isinstance(f['value'], (int, float)) or isinstance(f['value'], bool) or not isinstance(f['evidence'], dict):
                    raise ValueError('Invalid finding value or evidence')
                identity = _key([f['rule'], f['key']])
                if identity in seen:
                    raise ValueError('Duplicate finding key')
                seen.add(identity)
            _json(result)
            error = None
        except Exception as exc:
            result = {'evidence': {}, 'findings': []}
            # Do not persist exception text: it can contain SQL, credentials, or data.
            error = type(exc).__name__
        finally:
            stopped.set()
            heartbeat.join(timeout=1)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if current['token'] != token:
                return {**self._run_row(current), 'superseded': True}
            events = []
            fault = db.execute('SELECT * FROM failures WHERE scope=?', (run_id,)).fetchone()
            seq = fault['sequence'] if fault else 0
            if error:
                if not fault or not fault['active']:
                    seq += 1
                    events.append({'type': 'run_failed', 'run_id': run_id, 'sequence': seq, 'error': error, 'window_start': window_start, 'window_end': window_end})
                db.execute('INSERT OR REPLACE INTO failures VALUES(?,?,?)', (run_id, 1, seq))
            else:
                if fault and fault['active']:
                    events.append({'type': 'run_recovered', 'run_id': run_id, 'sequence': seq, 'window_start': window_start, 'window_end': window_end})
                    db.execute('UPDATE failures SET active=0 WHERE scope=?', (run_id,))
                active_ids = set()
                for f in result['findings']:
                    iid = _key([scope, window_start, window_end, f['rule'], f['key']])
                    active_ids.add(iid)
                    row = db.execute('SELECT body FROM incidents WHERE id=?', (iid,)).fetchone()
                    old_i = json.loads(row['body']) if row else None
                    event = 'new' if old_i is None else 'recurrence' if old_i['status'] == 'recovered' else None
                    threshold = f.get('worsening_delta')
                    baseline = old_i.get('notified_value', old_i['value']) if old_i else f['value']
                    if old_i and event is None and isinstance(threshold, (int, float)) and threshold > 0 and f['value'] - baseline >= threshold:
                        event = 'worsening'
                    item = {**f, 'run_id': run_id, 'evidence': {**f['evidence'], 'run_id': run_id}, 'id': iid, 'scope': scope, 'plan_id': plan['id'], 'mode': plan['mode'], 'version': plan.get('version', 1), 'rule_source_version': source_version, 'window_start': window_start, 'window_end': window_end, 'first_seen': old_i['first_seen'] if old_i else now, 'last_seen': now, 'status': 'open', 'notified_value': f['value'] if event else baseline}
                    db.execute('INSERT OR REPLACE INTO incidents VALUES(?,?,?,?,?,?)', (iid, scope, window_start, window_end, 'open', _json(item)))
                    if event:
                        record = {'type': event, 'run_id': run_id, 'incident_id': iid, 'at': now, 'value': f['value'], 'rule': f['rule'], 'object': f['object'], 'window_start': window_start, 'window_end': window_end}
                        events.append(record)
                        db.execute('INSERT INTO history(incident_id,body) VALUES(?,?)', (iid, _json(record)))
                for row in db.execute('SELECT id,body FROM incidents WHERE scope=? AND window_start=? AND window_end=? AND status=?', (scope, window_start, window_end, 'open')).fetchall():
                    if row['id'] in active_ids:
                        continue
                    item = json.loads(row['body'])
                    item.update(status='recovered', last_seen=now, run_id=run_id, evidence={**item['evidence'], 'run_id': run_id})
                    record = {'type': 'recovered', 'run_id': run_id, 'incident_id': row['id'], 'at': now, 'window_start': window_start, 'window_end': window_end}
                    events.append(record)
                    db.execute('UPDATE incidents SET status=?,body=? WHERE id=?', ('recovered', _json(item), row['id']))
                    db.execute('INSERT INTO history(incident_id,body) VALUES(?,?)', (row['id'], _json(record)))
            body = {'mode': plan['mode'], 'rule_source_version': source_version, 'evidence': result['evidence'], 'events': events, 'error': error, 'attempt_history': previous.get('attempt_history', []) + [{'attempt': attempts, 'at': now, 'status': 'failed' if error else 'succeeded', 'error': error}]}
            db.execute('UPDATE runs SET status=?,lease_until=0,updated=?,body=? WHERE id=?', ('failed' if error else 'succeeded', time.time(), _json(body), run_id))
            self._queue(db, plan, run_id, events, result['evidence'])
            return self._run_row(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())

    def open_windows(self, plan):
        with self._db() as db:
            return [(r[0], r[1]) for r in db.execute('SELECT DISTINCT window_start,window_end FROM incidents WHERE scope=? AND status=?', (scope_key(plan), 'open'))]

    def due_windows(self, now=None):
        local = (now or datetime.now(ZoneInfo('America/New_York'))).astimezone(ZoneInfo('America/New_York'))
        runs = {r['id']: r for r in self.runs()}
        due = []
        for plan in self.plans():
            try:
                if not plan['enabled'] or (local.hour, local.minute) < (int(plan['hour']), int(plan['minute'])):
                    continue
                windows = set(self.open_windows(plan))
                for offset in range(1, int(plan['lookback_days']) + 1):
                    day = (local.date() - timedelta(days=offset)).isoformat()
                    end = (local.date() - timedelta(days=offset - 1)).isoformat()
                    windows.add((day, end))
                # Interrupted/failed windows survive restarts even when outside lookback.
                for r in runs.values():
                    if r['scope'] == scope_key(plan) and r['status'] in ('running', 'failed'):
                        windows.add((r['window_start'], r['window_end']))
                for start, end in sorted(windows):
                    r = runs.get(_key([scope_key(plan), start, end]))
                    checked_today = r and datetime.fromtimestamp(r['updated'], ZoneInfo('America/New_York')).date() == local.date()
                    if r and r['status'] == 'running' and r['lease_until'] > local.timestamp():
                        continue
                    if checked_today and r['status'] == 'succeeded':
                        continue
                    if r and r['status'] == 'failed' and local.timestamp() - r['updated'] < 300:
                        continue
                    due.append((plan, start, end))
            except Exception as exc:
                logging.error('Invalid persisted plan skipped: %s', type(exc).__name__)
        return due

    def dispatch(self, sender=None):
        from .notifications import FeishuSender, DeliveryUncertain
        sender = sender or FeishuSender()
        if not getattr(sender, 'enabled', False):
            return []
        deliveries = []
        current_plans = {p['id']: p for p in self.plans()}
        for item in self.outbox():
            if item['status'] not in ('pending', 'failed'):
                continue
            current_plan = current_plans.get(item['plan_id'])
            if not current_plan or current_plan.get('mode') != 'live' or current_plan.get('shadow', True):
                continue
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                claimed = db.execute("UPDATE outbox SET status='uncertain',updated=? WHERE id=? AND status IN ('pending','failed')", (time.time(), item['id'])).rowcount
            if not claimed:
                continue
            # Pre-mark uncertain: a crash after HTTP acceptance cannot blindly resend.
            try:
                sender.send(item)
                status = 'sent'
            except DeliveryUncertain:
                status = 'uncertain'
            except Exception:
                status = 'failed'
            with self._db() as db:
                db.execute('UPDATE outbox SET status=?,updated=? WHERE id=?', (status, time.time(), item['id']))
            deliveries.append({'id': item['id'], 'status': status})
        return deliveries
