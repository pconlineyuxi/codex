"""Local review follow-up, separate from production incident/recovery state.

Database identities stay private; reports may show user-authorized order numbers.
Missing, excluded or failed reads never count as a cleared finding.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import fcntl
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from bi_check_agent import cost_provenance, core, lifecycle_patrol, service, patrol_policy, outbound_evidence
from bi_check_agent.rule_catalog import RULES

ZONE = ZoneInfo('America/New_York')
HIGH_RULES = set(patrol_policy.POLICY['high_rules'])
LABELS = {**service.RULE_LABELS, 'outbound_match':'出库关联待核对', 'source_integrity':'来源完整性待核查'}
STATE_LABELS = {'open':'仍命中', 'clear':'本次已不再命中', 'unknown':'复查无法确认',
                'exempt':'业务例外，不适用', 'deferred':'长期未履约待核对'}
REASON_LABELS = {'new':'首次发现', 'changed':'状态变化', 'reopened':'再次命中', 'reminder':'持续未解决', 'check_changed':'检查状态变化'}
EVIDENCE_LABELS = {'legacy_group_hit':'原分组仍命中','legacy_group_no_longer_hit':'原分组本次不再命中',
                   'legacy_group_unverified':'原分组证据不足','source_incomplete':'来源完整性未确认',
                   'source_available':'来源现可读取','duplicate_source':'金额来源重复','missing_source':'缺少金额来源',
                   'unknown_input':'缺少判断所需字段','excluded_fixed_filter':'已不在原筛选范围',
                   'record_or_rule_not_available':'原记录或规则当前不可核对','query_failed':'本次读取失败',
                   'evidence_read_failed':'出库关联证据读取失败，保留疑点','unique_match':'已通过订单商品或库存移动证据核实关联','ambiguous_or_missing_match':'出库商品无法唯一关联','open':'本次仍命中','clear':'本次不再命中'}
EVIDENCE_LABELS.update(patrol_policy.REASONS)


@contextmanager
def locked(mode_dir):
    mode_dir.mkdir(parents=True, exist_ok=True)
    with (mode_dir/'tracking.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('已有历史复查正在运行，本次未修改跟踪状态。') from None
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)


def load_state(mode_dir):
    path = mode_dir/'tracking-state.json'
    if not path.exists():
        return {'schema_version':1, 'cases':{}, 'imported_reports':[], 'health':{}}
    state = json.loads(path.read_text())
    if state.get('schema_version') != 1 or not isinstance(state.get('cases'), dict):
        raise ValueError('历史跟踪状态不可读；保留原文件，不能重建或关闭疑点。')
    for case in state['cases'].values():
        if case.get('state') not in STATE_LABELS or not case.get('first_seen_at'):
            raise ValueError('历史跟踪条目不完整；保留原文件。')
    return state


def identity(item):
    fields = ['event_day','cohort','rule','identity_kind']
    if item['identity_kind']=='legacy_group': fields += ['market_place','store','sku']
    elif item['rule']=='outbound_match': fields += ['order_id','product_id']
    else: fields += ['order_line_id']
    return sha256(json.dumps({k:item.get(k) for k in fields},sort_keys=True).encode()).hexdigest()


def dimensions(row):
    return {k:None if pd.isna(row.get(k)) else str(row[k]) for k in ['market_place','store','sku']}


def collect_snapshot(day, now, sales, cost, source, sales_rules, cost_rules):
    """Internal evidence callback; never attached to the public lifecycle report."""
    observations = []
    seen_pairs = set()
    source_counts = source.order_line_id.value_counts()
    source_by_id = {int(r['order_line_id']):r for r in source.to_dict('records')}
    rows = source.copy()
    if not rows.empty:
        rows = rows[rows.main_ir.notna() & ~rows.main_ir.isin(core.FIXED_EXCLUDED_MAIN_IR)].copy()
        rows['type'], rows['date_order'] = 'order', str(day)
    findings = core.detect_anomalies(rows, list(dict.fromkeys(sales_rules+cost_rules)),preserve_line_id=True)
    hits = {int(r['order_line_id']):set(r['anomaly_rules'].split(', ')) for r in findings.to_dict('records')}
    for cohort, events, rules in [('sales',sales,sales_rules),('cost',cost,cost_rules)]:
        due = now >= datetime.combine(day+timedelta(days=1),time(9 if cohort=='cost' else 0),ZONE)
        for event in events.to_dict('records'):
            line = int(event['order_line_id']) if pd.notna(event.get('order_line_id')) else None
            row = source_by_id.get(line,{}) if source_counts.get(line,0)==1 else {}
            base = {'event_day':str(day),'cohort':cohort,'identity_kind':'exact',
                    'order_line_id':line, **dimensions(row),
                    'order_numbers':patrol_policy.order_names([{**row,**event}]),'order_reference_basis':'exact'}
            evidence = {**row,**event}
            for key in ('actual_ir','cost_review'):
                value = evidence.get(key)
                if isinstance(value,dict) or isinstance(value,str): base[key] = value
            if cohort=='cost':
                base.update(order_id=int(event['order_id']),product_id=int(event['product_id']))
                pair = (event['order_id'],event['product_id'])
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    observations.append({**base,'rule':'outbound_match','state':'clear' if outbound_evidence.resolved(event) else ('unknown' if event.get('association_basis')=='evidence_read_failed' else 'open'),
                                         'reason':'unique_match' if outbound_evidence.resolved(event) else ('evidence_read_failed' if event.get('association_basis')=='evidence_read_failed' else 'ambiguous_or_missing_match'),
                                         'association_basis':event.get('association_basis','unique_order_product'), 'count':1})
                if not outbound_evidence.resolved(event): continue
            for rule in rules:
                reason = None
                decision = patrol_policy.decision(rule,{**row,**event},now) if due and source_counts.get(line,0)<=1 else None
                if decision and (source_counts.get(line,0)==0 and decision['reason']=='confirmed_cancelled'
                                 or source_counts.get(line,0)==1 and pd.notna(row.get('main_ir')) and row.get('main_ir') not in core.FIXED_EXCLUDED_MAIN_IR):
                    observations.append({**base,'rule':rule,**decision,'count':1})
                    continue
                if not due: reason = 'not_yet_due'
                elif source_counts.get(line,0)==0: reason = 'missing_source'
                elif source_counts.get(line,0)>1: reason = 'duplicate_source'
                elif pd.isna(row.get('main_ir')) or row.get('main_ir') in core.FIXED_EXCLUDED_MAIN_IR: reason = 'excluded_fixed_filter'
                else:
                    for field in RULES[rule].required_fields:
                        try: finite = math.isfinite(float(row.get(field)))
                        except (TypeError,ValueError): finite = False
                        if not finite: reason = 'unknown_input'; break
                state = 'unknown' if reason else ('open' if rule in hits.get(line,set()) else 'clear')
                observations.append({**base,'rule':rule,'state':state,'reason':reason or state,'count':1})
    incomplete = any(isinstance(r.get('cost_review'),dict) and cost_provenance.incomplete(r['cost_review']) for r in source.to_dict('records'))
    return {'event_day':str(day),'checked_at':now.isoformat(),'observations':observations,
            'cost_provenance_incomplete':incomplete}


def _new_case(item, when, notified=None):
    return {**item, 'first_seen_at':when, 'open_since_at':when, 'last_checked_at':when,
            'last_state_change_at':when, 'last_notified_at':notified,
            'priority':'high' if item['rule'] in HIGH_RULES and item['state']=='open' else 'normal'}


def import_reports(state, mode_dir, day, days):
    """Seed only recorded lifecycle doubts, never maturity-unverified raw hits."""
    start = day-timedelta(days=days-1)
    imported = set(state['imported_reports'])
    available, damaged = set(), []
    for folder in sorted(mode_dir.iterdir()):
        try: report_day = date.fromisoformat(folder.name)
        except ValueError: continue
        if not folder.is_dir() or not start <= report_day <= day: continue
        for path in sorted(folder.glob('*/report.json')):
            relative = str(path.relative_to(mode_dir))
            if relative in imported:
                available.add(str(report_day)); continue
            try:
                report = json.loads(path.read_text())
                lifecycle = report.get('lifecycle_review') or {}
                when = report['checked_at']
                datetime.fromisoformat(when)
            except (ValueError,KeyError,OSError):
                damaged.append(relative); continue
            if lifecycle.get('status')=='failed' or not lifecycle:
                # A failed historical batch must not silently disappear.
                seeds = [{'cohort':c,'rule':'source_integrity','state':'unknown','count':1} for c in ('sales','cost')]
            else:
                seeds = []
                for cohort in ('sales','cost'):
                    section = lifecycle[cohort]
                    for group in section['groups']:
                        seeds.append({**{k:group.get(k) for k in ('market_place','store','sku')},
                                      'cohort':cohort,'rule':group['anomaly_rules'],'state':'open','count':group['规则命中样本数']})
                    if cohort=='cost' and (section.get('ambiguous_order_products',0)+section.get('unmatched_order_products',0)):
                        seeds.append({'cohort':cohort,'rule':'outbound_match','state':'open',
                                      'count':section.get('ambiguous_order_products',0)+section.get('unmatched_order_products',0)})
                    if section['missing_source_lines'] or section['duplicate_source_lines'] or section['unknown_inputs']:
                        seeds.append({'cohort':cohort,'rule':'source_integrity','state':'unknown','count':1})
            # New reports already have private identities in the state file.
            if 'history_review' not in report:
                for seed in seeds:
                    seed = {'event_day':str(report_day),'identity_kind':'legacy_group','reason':'legacy_summary',
                            'market_place':None,'store':None,'sku':None, **seed}
                    key = identity(seed)
                    if key not in state['cases']:
                        state['cases'][key] = _new_case(seed,when,when)
            available.add(str(report_day)); imported.add(relative)
    state['imported_reports'] = sorted(imported)
    missing = [str(start+timedelta(days=i)) for i in range(days) if str(start+timedelta(days=i)) not in available and start+timedelta(days=i)!=day]
    return {'window_start':str(start),'window_end':str(day),'unavailable_report_days':missing,'unreadable_reports':damaged}


def _legacy_observation(case, snapshot):
    rows = [r for r in snapshot['observations'] if r['cohort']==case['cohort']]
    if case['rule']=='source_integrity':
        uncertain = [r for r in rows if r['state']=='unknown' and r['reason']!='excluded_fixed_filter']
        return {**case,'state':'unknown' if uncertain or not rows else 'clear','reason':'source_incomplete' if uncertain or not rows else 'source_available'}
    selected = [r for r in rows if r['rule']==case['rule'] and (case['rule']=='outbound_match' or
                all(r.get(k)==case.get(k) for k in ('market_place','store','sku')))]
    # A missing amount row may have no SKU. It must block a legacy group clear.
    incomplete = any(r['state']=='unknown' for r in rows if r['rule']==case['rule'])
    # Legacy reports cannot tell whether a newly ambiguous shipment was the
    # original bad row. Other healthy rows of the same SKU cannot replace it.
    if case['cohort']=='cost' and case['rule']!='outbound_match':
        incomplete |= any(r['rule']=='outbound_match' and r['state']!='clear' for r in rows)
    hits = sum(r['count'] for r in selected if r['state']=='open')
    exception = next((r for r in selected if r['state'] in {'exempt','deferred'}),None)
    incomplete |= sum(r['count'] for r in selected) < case['count']
    applicable_exception = exception and not incomplete and all(r['state'] in {'clear',exception['state']} for r in selected)
    state = 'open' if hits else (exception['state'] if applicable_exception else ('unknown' if incomplete or not selected else 'clear'))
    current_rows = [r for r in selected if r['state']==state]
    # An old count-only association group cannot identify its original orders.
    # Once clear, never label every healthy order on that day as an old case.
    if case['rule']=='outbound_match' and state=='clear': current_rows=[]
    extra = {k:current_rows[0][k] for k in ('actual_ir','cost_review') if k in current_rows[0]} if len(current_rows)==1 else {}
    current_case = {k:v for k,v in case.items() if k != 'cost_review'}
    return {**current_case,**extra,'state':state,'count':hits if hits else case['count'],
            'order_numbers':sorted({n for r in current_rows for n in r.get('order_numbers',[])}),
            'order_reference_basis':'current_group',
            'reason':'legacy_group_hit' if hits else (exception['reason'] if applicable_exception else ('legacy_group_unverified' if state=='unknown' else 'legacy_group_no_longer_hit'))}


def _update(case, observation, now):
    prior = case['state']
    prior_evidence = (case.get('cost_review') or {}).get('code')
    if 'cost_review' not in observation:
        case.pop('cost_review',None)  # Do not show stale gift proof after a failed read.
    case.update({k:v for k,v in observation.items() if k in {'state','reason','count','market_place','store','sku','actual_ir','cost_review','association_basis'} and v is not None})
    if case.get('identity_kind')=='legacy_group' and case['rule']=='outbound_match' and observation.get('state')=='clear':
        case['order_numbers'] = []
        case['order_reference_basis'] = 'current_group'
    elif observation.get('order_numbers'):
        case['order_numbers'] = observation['order_numbers']
        case['order_reference_basis'] = observation.get('order_reference_basis','exact')
    elif case.get('order_numbers'):
        case['order_reference_basis'] = 'last_known'
    case['last_checked_at'] = now.isoformat()
    if case['state']=='open' and case['rule'] in HIGH_RULES: case['priority']='high'
    if case['state'] in {'exempt','deferred'}: case['priority']='normal'
    if case['state']!=prior:
        case['last_state_change_at'] = now.isoformat()
        if prior in {'clear','exempt','deferred'}: case['open_since_at'] = now.isoformat()
        return 'reopened' if prior in {'clear','exempt','deferred'} and case['state']=='open' else 'changed'
    new_evidence = (case.get('cost_review') or {}).get('code')
    if new_evidence and new_evidence != prior_evidence and case['state'] in {'open','unknown'}:
        return 'check_changed'
    return None


def _due(case, now):
    if case['state'] in {'clear','exempt','deferred'}: return False
    today = now.astimezone(ZONE).date()
    since = datetime.fromisoformat(case['open_since_at']).astimezone(ZONE).date()
    last = datetime.fromisoformat(case['last_notified_at']).astimezone(ZONE).date() if case.get('last_notified_at') else None
    interval = patrol_policy.POLICY['high_interval_days' if case['priority']=='high' else 'normal_interval_days']
    return (today-since).days >= interval and (last is None or (today-last).days>=interval)


def _public(case, now, reason=None):
    # Business order numbers are authorized; raw database IDs remain private.
    return {**{k:case.get(k) for k in ('event_day','cohort','market_place','store','sku','rule','state','reason','count','priority','identity_kind','first_seen_at','last_checked_at','last_notified_at','order_numbers','order_reference_basis','actual_ir','cost_review','association_basis')},
            'age_days':max(0,(now.astimezone(ZONE).date()-datetime.fromisoformat(case['open_since_at']).astimezone(ZONE).date()).days),
            'notification_reason':reason}


def reconcile(state, snapshot, now, changes, case_keys=None):
    day = snapshot['event_day']
    existing = {k:v for k,v in state['cases'].items() if v['event_day']==day and (case_keys is None or k in case_keys)}
    by_key = {identity(r):r for r in snapshot['observations']}
    legacy = [c for c in existing.values() if c['identity_kind']=='legacy_group']
    for key, case in existing.items():
        observation = _legacy_observation(case,snapshot) if case['identity_kind']=='legacy_group' else by_key.get(key)
        if observation is None: observation = {'state':'unknown','reason':'record_or_rule_not_available'}
        change = _update(case,observation,now)
        if change: changes[key] = change
    for key, row in by_key.items():
        if key in state['cases'] or row['state'] in {'clear','exempt'} or row['reason'] in {'excluded_fixed_filter','not_yet_due'}: continue
        covered = any(c['cohort']==row['cohort'] and c['rule']==row['rule'] and
                      (c['rule']=='outbound_match' or all(c.get(k)==row.get(k) for k in ('market_place','store','sku'))) for c in legacy)
        if covered: continue
        state['cases'][key] = _new_case(row,now.isoformat())
        changes[key] = 'new'


def review_history(report, state, mode_dir, snapshots, now, days=None, review_fn=None, progress=None):
    if now.tzinfo is None: raise ValueError('复查时间必须带时区')
    days = patrol_policy.POLICY['history_days'] if days is None else days
    day = date.fromisoformat(report['window']['start'])
    coverage = import_reports(state,mode_dir,day,days)
    changes, failures, checked = {}, [], []
    evidence_incomplete_days = []
    by_day = defaultdict(list)
    for case in state['cases'].values():
        if case['state'] not in {'clear','exempt'} or date.fromisoformat(case['event_day']) >= day-timedelta(days=days-1):
            by_day[case['event_day']].append(case)
    by_day.setdefault(str(day),[])
    snapshot_map = {s['event_day']:s for s in snapshots}
    selected = list(service.RULE_LABELS)
    for event_day, cases in sorted(by_day.items()):
        if progress: progress(event_day,'started')
        snapshot = snapshot_map.get(event_day)
        if snapshot is None:
            captured = []
            try:
                targets = None if any(c['identity_kind']=='legacy_group' for c in cases) else cases
                if event_day==str(day) and not cases and not snapshots: raise RuntimeError('current cohort unavailable')
                if review_fn:
                    review_fn(date.fromisoformat(event_day),selected,now,captured.append,targets)
                else:
                    lifecycle_patrol.review(date.fromisoformat(event_day),selected,now,
                        fetch_fn=lambda d: lifecycle_patrol.fetch_cohorts(d,targets=targets),tracking_sink=captured.append)
                snapshot = captured[0]
            except Exception:
                failures.append(event_day)
                if progress: progress(event_day,'failed')
                for case in cases:
                    change = _update(case,{'state':'unknown','reason':'query_failed'},now)
                    if change: changes[identity(case)] = change
                continue
        if snapshot.get('cost_provenance_incomplete'): evidence_incomplete_days.append(event_day)
        reconcile(state,snapshot,now,changes,case_keys={identity(c) for c in cases})
        checked.append(event_day)
        if progress: progress(event_day,'checked')
    notifications = []
    for key, case in state['cases'].items():
        reason = changes.get(key)
        if not reason and case['event_day'] in by_day and _due(case,now): reason = 'reminder'
        if reason:
            case['last_notified_at'] = now.isoformat()
            notifications.append(_public(case,now,reason))
    health = {'main_query':'ok' if report['query_succeeded'] else 'failed',
              'lifecycle':('unavailable' if not report.get('lifecycle_review') else ('failed' if report['lifecycle_review']['status']=='failed' else 'ok')),
              'supplementary':(report.get('diagnostics') or {}).get('source_status','unavailable'),
              'cost_provenance':'unavailable' if failures else ('incomplete' if evidence_incomplete_days else 'ok'),
              'history':'failed' if failures or coverage['unreadable_reports'] else 'ok'}
    health_changes = {k:{'previous':state['health'].get(k),'current':v} for k,v in health.items()
                      if state['health'].get(k)!=v and (k in state['health'] or v in {'failed','unavailable','incomplete'})}
    state['health'] = health
    active = [c for c in state['cases'].values() if c['state'] not in {'clear','exempt'}]
    history = {**coverage,'status':'partial_assessment' if failures or evidence_incomplete_days or coverage['unreadable_reports'] or any(c['state']=='unknown' for c in active) else 'reviewed',
               'checked_event_days':checked,'failed_event_days':failures,'cost_evidence_incomplete_days':evidence_incomplete_days,'tracked_rule_items':len(state['cases']),
               'active_rule_items':len(active),'state_counts':dict(Counter(c['state'] for c in state['cases'].values())),
               'items':[_public(c,now) for c in active], 'notifications':notifications,
               'note':'按稳定标识复查；旧报告仅能按原日期与原分组回查，明确标为 legacy_group。超过 14 天仍未解决的条目继续保留。条目数是规则项，跨规则不可相加当订单数。本次不再命中不是正式异常恢复。'}
    notification = {'should_notify':bool(notifications or health_changes),'items':notifications,'health_changes':health_changes,
                    'policy':{k:patrol_policy.POLICY[k] for k in ('version','high_interval_days','normal_interval_days','stale_unfulfilled_days')}}
    notification['policy']['lookback_days'] = days
    state['last_run_at'] = now.isoformat()
    report['history_review'],report['notification'] = history,notification
    if history['status']=='partial_assessment' and report['query_succeeded']: report['status']='partial_assessment'
    return state


def markdown(history, notification):
    def cell(v): return str(v if v is not None else '待确认').replace('|','\\|').replace('\n',' ')
    lines = ['', '## 历史疑点复查与提醒', '',
             f"历史报告回看：{history['window_start']} 至 {history['window_end']}；未解决项超过窗口继续跟踪。",
             f"本次复查状态：{history['status']}；待跟踪 {history['active_rule_items']} 个规则项（不是去重订单数）。", history['note'], '',
             f"高优先级每 {patrol_policy.POLICY['high_interval_days']} 天提醒；其他疑点首次发现、状态变化时及每 {patrol_policy.POLICY['normal_interval_days']} 天提醒。长期未履约只在首次进入或状态变化时提醒；业务例外不持续提醒。同日重复运行不重复发送持续提醒。",
             f"本次是否需要提醒：{'是' if notification['should_notify'] else '否'}。"]
    if history['unavailable_report_days']: lines.append('缺少历史报告的日期：'+', '.join(history['unavailable_report_days'])+'；未声称覆盖这些日期。')
    if history.get('cost_evidence_incomplete_days'): lines.append('零成本来源证据未完整读取的日期：'+', '.join(history['cost_evidence_incomplete_days'])+'；没有按无 PO 排除。')
    if history['failed_event_days']: lines.append('复查失败日期：'+', '.join(history['failed_event_days'])+'；原疑点保留。')
    if history['unreadable_reports']: lines.append('部分历史报告不可读，已保留缺口，不能判断其疑点状态。')
    if notification['health_changes']:
        names={'main_query':'主查询','lifecycle':'创建／出库复核','supplementary':'补充原因核查','history':'历史复查','cost_provenance':'零成本来源复核'}
        statuses={'ok':'成功','failed':'失败','unavailable':'未取得结果',None:'尚无跟踪记录','incomplete':'证据不完整','reviewed':'已复核','not_needed':'本次无零成本候选'}
        lines.append('检查状态变化：'+'；'.join(f"{names.get(k,k)}：{statuses.get(v['previous'],v['previous'])} → {statuses.get(v['current'],v['current'])}" for k,v in notification['health_changes'].items()))
    for title, items in [('本次需要提醒的事项',history['notifications']),('仍需跟踪的事项',history['items'])]:
        if not items: continue
        lines += ['', '### '+title, '', '| 原业务日 | 平台／店铺 | SKU | 订单号 | 规则 | 状态 | 优先级 | 持续天数 | 规则项记录数 | 依据／提醒原因 |',
                  '| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | --- |']
        # Aggregate public rows with identical dimensions, never emit private IDs.
        grouped = Counter()
        for item in sorted(items,key=lambda x:(x['priority']!='high',x['state']=='clear',-x['age_days'],x['event_day'],x['rule'])):
            reference = {'current_group':'（本次分组匹配）','last_known':'（上次已知，本次未确认）'}.get(item.get('order_reference_basis'),'')
            names = ', '.join(item.get('order_numbers') or []) or '未取得'
            values = [item['event_day']+('（销售）' if item['cohort']=='sales' else '（成本）'),f"{item['market_place'] or '待确认'}／{item['store'] or '待确认'}",(item['sku'] or '')+('／实际商品 '+item['actual_ir'] if item.get('actual_ir') else ''),names+reference,
                      LABELS.get(item['rule'],item['rule']),STATE_LABELS[item['state']],'高' if item['priority']=='high' else '普通',item['age_days'],
                      ('旧报告分组回查；' if item['identity_kind']=='legacy_group' else '')+EVIDENCE_LABELS.get(item['reason'],item['reason'])+'；'+REASON_LABELS.get(item['notification_reason'],'')+('；'+item['cost_review']['summary'] if isinstance(item.get('cost_review'),dict) else '')]
            grouped[tuple(values)] += item['count']
        for values, count in grouped.items():
            lines.append('| '+' | '.join(cell(v) for v in [*values[:-1],count,values[-1]])+' |')
    return lines
