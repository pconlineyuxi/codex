"""Headless BA review for Codex; no Streamlit, incident recovery or messaging."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv
from bi_check_agent import core, service, lifecycle_patrol
from bi_check_agent.diagnostics import public as public_diagnostics
from bi_check_agent.models import AnomalyQueryRequest
from bi_check_agent.rule_catalog import rules_with_tag

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo('America/New_York')
DATA_RULES = list(rules_with_tag('patrol_data_issue'))
CORE_RULES = list(rules_with_tag('default_patrol'))
LIMITATION = '刷新完整性、源币种及源日期时区未核实；本次为只读核查，不判定数据通过或正式异常恢复。'
SALES_RULES = list(rules_with_tag('awaits_sales'))
COST_RULES = list(rules_with_tag('awaits_cost'))
MATURITY_NOTE = ('订单/广告日期观察不能替代创建/出库批次复核。下方原始条件命中保留为观察；'
                 '销售和成本另按创建日、实际扫描出库日以及预期补齐时限复核。'
                 '尚未接入补跑任务成功凭据，超过预期时点仍命中也只能列为疑点。')


def maturity_reason(rule, mode):
    if mode == 'demo':
        return ''
    reasons = []
    if rule in SALES_RULES:
        reasons.append('本表按订单日期观察；创建日的预期时限复核见上方，补跑成功凭据未接入')
    if rule in COST_RULES:
        reasons.append('本表未按商品实际出库筛选；出库日的预期时限复核见上方，成本批次成功凭据未接入')
    if rule in rules_with_tag('awaits_ads'):
        reasons.append('广告批次完整性未核实；同日账面销售不是广告归因销售')
    return '；'.join(reasons)


def advertising_observation(rows, findings):
    """Explain the unit of an ad hit without exposing order records."""
    groups = [r for r in findings if r['anomaly_rules'] == 'ad_spend_without_sales']
    columns = ['market_place', 'store', 'sku', 'day']
    def key(row):
        return tuple(None if pd.isna(row.get(c)) else str(row[c]) for c in columns)
    order_counts = Counter()
    if not rows.empty:
        for row in rows.loc[rows.type == 'order', columns].to_dict('records'):
            order_counts[key(row)] += 1
    by_store = Counter((r.get('market_place'), r.get('store')) for r in groups)
    no_orders = sum(order_counts[key(r)] == 0 for r in groups)
    return {'group_count':len(groups), 'platform_count':len({r.get('market_place') for r in groups}),
        'store_count':len(by_store), 'distinct_sku_count':len({r.get('sku') for r in groups}),
        'no_matching_order_groups':no_orders, 'orders_present_zero_net_sales_groups':len(groups)-no_orders,
        'by_platform_store':[{'platform':p,'store':s,'sku_day_groups':n} for (p,s),n in by_store.items()],
        'interpretation':'分组单位是平台×店铺×SKU×日，不是平台数。无匹配订单仅指本次日期和固定筛选范围；零净销售不等于广告无转化。'}


def previous_day(now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('必须传入带时区的时间')
    return current.astimezone(ZONE).date() - timedelta(days=1)


def build_report(day, rules, mode='review', query_fn=None, lifecycle_fn=None):
    if mode not in {'review', 'verified', 'demo'}:
        raise ValueError('未知巡查模式')
    request = AnomalyQueryRequest(start_date=day, end_date=day+timedelta(days=1),
        anomaly_rules=rules, aggregation_dimensions=['marketplace', 'store', 'sku', 'day'])
    run_lifecycle = mode != 'demo' and (query_fn is None or lifecycle_fn is not None)
    query_fn = query_fn or service.query
    result = query_fn(request, {'review':'live_test', 'verified':'live', 'demo':'demo'}[mode])
    evidence = result['evidence']
    summary = service.summarize_findings(result['anomalies'], request.aggregation_dimensions)
    findings = service.records(summary)
    counts = Counter()
    for item in findings:
        counts[item['anomaly_rules']] += int(item['规则命中样本数'])
    definitions = core.load_yaml('diagnostic_rules.yaml')['queryable_rules']
    rule_summary = []
    for rule in rules:
        pending_reason = maturity_reason(rule, mode)
        rule_summary.append({'rule':rule, 'label':service.RULE_LABELS[rule],
            'category':('待核对到齐条件' if pending_reason else ('疑似数据问题' if rule in DATA_RULES else '经营信号 / 符号待核查')),
            'assessment':'maturity_unverified' if pending_reason else 'observation',
            'pending_reason':pending_reason,
            'hit_count':counts[rule], 'unit':'SKU/店铺/平台/日分组' if definitions[rule]['level']=='aggregate' else '来源记录',
            'unknown_inputs':[v for v in evidence.get('unknown_rule_inputs',[]) if v['rule']==rule]})
    empty = not evidence['row_count']
    unknown = bool(evidence.get('unknown_rule_inputs'))
    shared_diagnostics=evidence.get('diagnostics') or {}
    unknown |= shared_diagnostics.get('source_status')=='failed' or bool(shared_diagnostics.get('summary',{}).get('unknown'))
    has_observations = any(r['hit_count'] and not r['pending_reason'] for r in rule_summary)
    lifecycle = None
    if run_lifecycle:
        try:
            lifecycle = (lifecycle_fn or lifecycle_patrol.review)(day, rules)
        except Exception:
            lifecycle = {'status':'failed','job_completion_verified':False,
                'note':'创建/出库时间复核未完成，请核查只读来源、关联字段与读取上限；未输出原始连接错误。'}
        if lifecycle['status'] in {'failed','partial_assessment','empty_unverified'}:
            unknown = True
        if lifecycle['status'] != 'failed':
            has_observations |= any(any(lifecycle[name]['rule_counts'].values()) for name in ['sales','cost'])
            empty = empty and not any(lifecycle[name]['candidate_lines'] for name in ['sales','cost'])
    status = 'partial_assessment' if unknown else ('empty_unverified' if empty else (
        'observations' if has_observations else ('maturity_unverified' if mode != 'demo' else 'no_hits')))
    return {'schema_version':2, 'mode':mode, 'status':status, 'query_succeeded':True,
        'checked_at':datetime.now(timezone.utc).isoformat(),
        'window':{'start':str(day), 'end_exclusive':str(day+timedelta(days=1)), 'timezone':str(ZONE)},
        'scope':{'platforms':'all', 'stores':'all', 'fixed_excluded_main_ir':list(core.FIXED_EXCLUDED_MAIN_IR), 'null_main_ir_excluded':True},
        'source_record_count':evidence['row_count'], 'rules':rule_summary,
        'finding_groups':findings, 'finding_group_count':len(findings),
        'advertising_observation':advertising_observation(result['rows'], findings),
        'lifecycle_review':lifecycle,
        'diagnostics':public_diagnostics(evidence.get('diagnostics')),
        'evidence':evidence, 'limitations':([LIMITATION, MATURITY_NOTE] if mode=='review' else ([MATURITY_NOTE] if mode=='verified' else ['演示合成样本，不代表公司实际数据。'])),
        'interpretation':'这里统计规则条件命中，不是已确认异常。不同规则可能命中同一记录，命中数不能相加当作订单数；来源记录与广告分组不可混加。'}


def failed_report(day, mode, exc):
    # Never include raw driver errors, connection strings or credentials.
    detail = '只读检查未完成，请核查数据库连通性、只读权限、配置及读取上限。'
    message = str(exc)
    safe_prefixes = ('查询超过累计', '单日数据超过', '真实查询尚未启用', '无法确认数据就绪',
                     '查询期间刷新凭据', '真实查询没有数据', '未配置 DATABASE_URL')
    if message.startswith(safe_prefixes): detail = message
    return {'schema_version':1,'mode':mode,'status':'failed','query_succeeded':False,
        'checked_at':datetime.now(timezone.utc).isoformat(),
        'window':{'start':str(day),'end_exclusive':str(day+timedelta(days=1)),'timezone':str(ZONE)},
        'error':{'type':type(exc).__name__,'message':detail}, 'rules':[], 'finding_groups':[],
        'limitations':['本次执行失败，不能解释为没有异常；上次成功结果另行保留。']}


def meaningful_signature(report):
    # Date rollover and differing raw row counts alone are not an actionable change.
    deferred = {r['rule'] for r in report['rules'] if r.get('assessment') == 'maturity_unverified'}
    groups = [{k:v for k,v in row.items() if k!='day'} for row in report['finding_groups'] if row['anomaly_rules'] not in deferred]
    rules = [{k:v for k,v in row.items() if row['rule'] not in deferred or k not in {'hit_count','unknown_inputs'}} for row in report['rules']]
    payload = {'mode':report['mode'],'status':report['status'],'error':report.get('error'),
        'rules':rules,'groups':sorted(groups,key=lambda x:json.dumps(x,sort_keys=True)),
        'rule_version':report.get('evidence',{}).get('rule_version'), 'scope':report.get('scope')}
    if report.get('lifecycle_review'):
        payload['lifecycle_review'] = lifecycle_patrol.signature_payload(report['lifecycle_review'])
    if report.get('diagnostics'):
        payload['diagnostics']=[{k:v for k,v in item.items() if k in {'market_place','store','sku','rule','assessment','reason_code','count'}} for item in report['diagnostics']['assessments'] if item['assessment'] not in {'waiting_data','unknown'} and item['reason_code']!='synthetic_demo']
    return sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.writing-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def cell(value):
    return str(value if value is not None else '未提供').replace('|','\\|').replace('\n',' ')


def markdown_report(report):
    w=report['window']
    lines=['# Product Profit 每日只读核查', '',
        f"业务日期：{w['start']}；截至 {w['end_exclusive']} 00:00（不含），纽约时间。", '',
        f"执行状态：{report['status']}；数据模式：{report['mode']}。", '']
    lines.extend(report.get('limitations',[]))
    if not report['query_succeeded']:
        return '\n'.join(lines+['',report['error']['message'],''])
    if report.get('lifecycle_review'):
        lines += lifecycle_patrol.markdown(report['lifecycle_review'])
    if report.get('diagnostics'):
        lines += ['', '## 与工具共用的适用条件及原因核查', '',
            '| 平台 | 店铺 | SKU | 规则 | 判定 | 核查结论 | 尚缺证据 |',
            '| --- | --- | --- | --- | --- | --- | --- |']
        for item in report['diagnostics']['assessments'][:30]:
            lines.append('| '+' | '.join(cell(v) for v in [item['market_place'],item['store'],item['sku'],service.RULE_LABELS[item['rule']],item['assessment_label'],item['conclusion'],'；'.join(item['missing_evidence'])])+' |')
        if report['diagnostics'].get('source_error'):lines += ['',report['diagnostics']['source_error']]
    lines += ['', '## 按订单/广告日期保留的辅助观察', '',
        f"检查 {report['source_record_count']} 条来源记录，执行 {len(report['rules'])} 条规则；覆盖全部平台、店铺。",'',
        '固定排除四类费用/折扣 main_ir 及 NULL main_ir。', '', report['interpretation'],'',
        '| 规则 | 判断状态 | 条件命中数 | 计数单位 | 尚缺依据 |','| --- | --- | ---: | --- | --- |']
    for row in report['rules']:
        missing='；'.join(filter(None, [row.get('pending_reason','')] + [f"{v['field']}：{v['unknown_rows']} 条" for v in row['unknown_inputs']])) or '未发现缺失输入；仍需业务核对'
        lines.append('| '+' | '.join(cell(v) for v in [row['label'],row['category'],row['hit_count'],row['unit'],missing])+' |')
    ads = report['advertising_observation']
    if ads['group_count']:
        lines += ['',f"广告与同日账面销售观察：{ads['group_count']} 个 SKU/日分组，涉及 {ads['platform_count']} 个平台、{ads['store_count']} 个平台/店铺组合、{ads['distinct_sku_count']} 个不同 SKU。",
            f"其中 {ads['no_matching_order_groups']} 个分组在本次范围内无匹配订单，{ads['orders_present_zero_net_sales_groups']} 个有订单记录但净销售额为零。",ads['interpretation'],'',
            '| 平台 | 店铺 | SKU/日分组数 |','| --- | --- | ---: |']
        for item in ads['by_platform_store']:
            lines.append('| '+' | '.join(cell(item[k]) for k in ['platform','store','sku_day_groups'])+' |')
    if report['finding_groups']:
        selected=sorted(report['finding_groups'],key=lambda x:(-x['规则命中样本数'],str(x.get('sku'))))[:30]
        lines += ['',f"以下展示 {len(selected)}/{report['finding_group_count']} 个条件命中分组（未判异常）；完整分组见同目录 report.json。",'',
            '| 平台 | 店铺 | SKU | 规则 | 命中数 |','| --- | --- | --- | --- | ---: |']
        for row in selected:
            lines.append('| '+' | '.join(cell(v) for v in [row.get('market_place'),row.get('store'),row.get('sku'),service.RULE_LABELS[row['anomaly_rules']],row['规则命中样本数']])+' |')
    else:
        lines += ['','本次未命中规则；不等于数据正确或已确认没有业务数据。']
    lines += ['','这是结果视图核查，尚未验证上游根因。报告仅保留维度汇总和证据，不输出订单行。','']
    return '\n'.join(lines)


def persist(report, output):
    mode_dir=Path(output)/report['mode']
    latest=mode_dir/'latest.json'
    previous=None
    if latest.exists():
        try: previous=json.loads(latest.read_text())
        except (ValueError,OSError): pass
    signature=meaningful_signature(report)
    report['changed_since_previous_run']=not previous or previous.get('notification_signature')!=signature
    report['notification_signature']=signature
    report['previous_successful_window']=None
    successful=mode_dir/'last-successful.json'
    if successful.exists():
        try: report['previous_successful_window']=json.loads(successful.read_text())['window']
        except (ValueError,KeyError,OSError): pass
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    run_dir=mode_dir/report['window']['start']/stamp
    run_dir.mkdir(parents=True)
    report['artifacts']={'json':str((run_dir/'report.json').resolve()),'markdown':str((run_dir/'report.md').resolve())}
    atomic_json(run_dir/'report.json',report)
    (run_dir/'report.md').write_text(markdown_report(report),encoding='utf-8')
    atomic_json(latest,report)
    if report['query_succeeded']: atomic_json(successful,report)
    return report


def main(argv=None):
    load_dotenv(ROOT/'.env')
    p=argparse.ArgumentParser(description='Codex headless Product Profit review (read only)')
    p.add_argument('--date',type=date.fromisoformat,default=None,help='Business date, default yesterday in New York')
    p.add_argument('--mode',choices=['review','verified','demo'],default='review')
    p.add_argument('--rules',choices=['all','core'],default='all')
    p.add_argument('--output-dir',type=Path,default=ROOT/'.runtime/codex-patrol')
    args=p.parse_args(argv)
    day=args.date or previous_day()
    if day >= datetime.now(ZONE).date(): p.error('日常巡查只检查已经结束的业务日')
    rules=list(service.RULE_LABELS) if args.rules=='all' else CORE_RULES
    try: report=build_report(day,rules,args.mode)
    except Exception as exc: report=failed_report(day,args.mode,exc)
    report=persist(report,args.output_dir)
    print(json.dumps({k:report[k] for k in ['status','query_succeeded','window','changed_since_previous_run','artifacts']},ensure_ascii=False))
    return 0 if report['query_succeeded'] else 1


if __name__=='__main__':
    raise SystemExit(main())
