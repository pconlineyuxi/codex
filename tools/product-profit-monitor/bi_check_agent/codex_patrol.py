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

from dotenv import load_dotenv
from bi_check_agent import core, service
from bi_check_agent.models import AnomalyQueryRequest

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo('America/New_York')
DATA_RULES = {'zero_units_with_sales', 'zero_sales_with_units', 'missing_product_cost',
              'unit_cost_non_positive', 'removed_hardware_abnormal', 'profit_rate_with_ads_ship_high'}
CORE_RULES = ['missing_product_cost', 'shipping_cost_ratio_high', 'ad_spend_without_sales']
LIMITATION = '刷新完整性、源币种及源日期时区未核实；本次为只读核查，不判定数据通过或正式异常恢复。'


def previous_day(now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('必须传入带时区的时间')
    return current.astimezone(ZONE).date() - timedelta(days=1)


def build_report(day, rules, mode='review', query_fn=None):
    if mode not in {'review', 'verified', 'demo'}:
        raise ValueError('未知巡查模式')
    request = AnomalyQueryRequest(start_date=day, end_date=day+timedelta(days=1),
        anomaly_rules=rules, aggregation_dimensions=['marketplace', 'store', 'sku', 'day'])
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
        rule_summary.append({'rule':rule, 'label':service.RULE_LABELS[rule],
            'category':'疑似数据问题' if rule in DATA_RULES else '经营信号 / 符号待核查',
            'hit_count':counts[rule], 'unit':'SKU/店铺/平台/日分组' if definitions[rule]['level']=='aggregate' else '来源记录',
            'unknown_inputs':[v for v in evidence.get('unknown_rule_inputs',[]) if v['rule']==rule]})
    empty = not evidence['row_count']
    unknown = bool(evidence.get('unknown_rule_inputs'))
    status = 'empty_unverified' if empty else ('partial_assessment' if unknown else ('findings' if findings else 'no_hits'))
    return {'schema_version':1, 'mode':mode, 'status':status, 'query_succeeded':True,
        'checked_at':datetime.now(timezone.utc).isoformat(),
        'window':{'start':str(day), 'end_exclusive':str(day+timedelta(days=1)), 'timezone':str(ZONE)},
        'scope':{'platforms':'all', 'stores':'all', 'fixed_excluded_main_ir':list(core.FIXED_EXCLUDED_MAIN_IR), 'null_main_ir_excluded':True},
        'source_record_count':evidence['row_count'], 'rules':rule_summary,
        'finding_groups':findings, 'finding_group_count':len(findings),
        'evidence':evidence, 'limitations':[LIMITATION] if mode=='review' else (['演示合成样本，不代表公司实际数据。'] if mode=='demo' else []),
        'interpretation':'命中不等于已确认错误；不同规则可能命中同一记录，命中数不能相加当作订单数。源记录与广告分组的计数不可混加。'}


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
    groups = [{k:v for k,v in row.items() if k!='day'} for row in report['finding_groups']]
    payload = {'mode':report['mode'],'status':report['status'],'error':report.get('error'),
        'rules':report['rules'],'groups':sorted(groups,key=lambda x:json.dumps(x,sort_keys=True)),
        'rule_version':report.get('evidence',{}).get('rule_version'), 'scope':report.get('scope')}
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
    lines += ['',f"检查 {report['source_record_count']} 条来源记录，执行 {len(report['rules'])} 条规则；覆盖全部平台、店铺。",'',
        '固定排除四类费用/折扣 main_ir 及 NULL main_ir。', '', report['interpretation'],'',
        '| 规则 | 分类 | 命中数 | 计数单位 | 无法判断的输入 |','| --- | --- | ---: | --- | --- |']
    for row in report['rules']:
        missing='；'.join(f"{v['field']}：{v['unknown_rows']} 条" for v in row['unknown_inputs']) or '无'
        lines.append('| '+' | '.join(cell(v) for v in [row['label'],row['category'],row['hit_count'],row['unit'],missing])+' |')
    if report['finding_groups']:
        selected=sorted(report['finding_groups'],key=lambda x:(-x['规则命中样本数'],str(x.get('sku'))))[:30]
        lines += ['',f"以下展示 {len(selected)}/{report['finding_group_count']} 个异常汇总分组；完整分组见同目录 report.json。",'',
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
