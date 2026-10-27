"""Check expected sales/cost completion by creation/shipment cohort, read only."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import os

import pandas as pd

from bi_check_agent import core, db, service
from bi_check_agent.rule_catalog import rules_with_tag

ZONE = ZoneInfo('America/New_York')
SALES_RULES = list(rules_with_tag('lifecycle_sales'))
COST_RULES = list(rules_with_tag('lifecycle_cost'))
NOTE = ('按预期补齐时限复核，不代表补跑任务已成功。销售额按创建当日应补齐；成本按出库后次日纽约时间 09:00 复核。'
        'scan_time 的 UTC 口径已由用户确认；sale_order.create_date 按 UTC 解释，与本次时间核查一致，仍属待业务确认的假设。'
        '金额来自物化结果视图的直接订单来源 bi.ba_profit_order_line_info，以订单行 ID 关联；与辅助观察不是同一快照。'
        '多订单行对应同一出库商品或来源不唯一时不强行分配。仅覆盖指定创建日/出库日，未自动回查更早未解决的批次。')


def utc_bounds(day):
    start = datetime.combine(day, time(), ZONE)
    end = datetime.combine(day + timedelta(days=1), time(), ZONE)
    # Source timestamp columns store naive UTC; use explicit DST-aware bounds.
    return start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None)


def _bounded(sql, params, query_fn):
    cap = int(os.getenv('MAX_RESULT_ROWS', '50000'))
    if cap < 1:
        raise ValueError('读取上限必须大于零')
    result = query_fn(sql + '\nLIMIT :max_rows', {**params, 'max_rows': cap + 1})
    if len(result) > cap:
        raise RuntimeError('生命周期来源超过读取上限，不能完整判断')
    return result


def fetch_cohorts(day, query_fn=None):
    query_fn = query_fn or db.run_query
    start, end = utc_bounds(day)
    params = {'start':start, 'end':end}
    sales = _bounded("""
SELECT sl.id AS order_line_id, so.create_date AS created_at
FROM public.sale_order so
JOIN public.sale_order_line sl ON sl.order_id=so.id
WHERE so.create_date >= :start AND so.create_date < :end
  AND so.state='sale' AND (so.walmart_status <> 'cancelled' OR so.walmart_status IS NULL)
""", params, query_fn)
    cost = _bounded("""
WITH events AS (
    SELECT order_id, product_id, MAX(scan_time) AS last_scan_at
    FROM public.order_outbound_history
    WHERE scan_time >= :start AND scan_time < :end AND scan_qty > 0
    GROUP BY order_id, product_id
)
SELECT e.order_id, e.product_id, e.last_scan_at, so.create_date AS created_at,
       COUNT(sl.id) AS line_matches, MIN(sl.id) AS order_line_id
FROM events e
JOIN public.sale_order so ON so.id=e.order_id
LEFT JOIN public.sale_order_line sl ON sl.order_id=e.order_id AND sl.product_id=e.product_id
WHERE so.state='sale' AND (so.walmart_status <> 'cancelled' OR so.walmart_status IS NULL)
GROUP BY e.order_id,e.product_id,e.last_scan_at,so.create_date
""", params, query_fn)
    unique_cost = cost[cost.line_matches == 1] if not cost.empty else cost
    ids = sorted({int(x) for x in list(sales.get('order_line_id', [])) + list(unique_cost.get('order_line_id', [])) if pd.notna(x)})
    frames = []
    total_rows = total_bytes = 0
    for offset in range(0, len(ids), 1000):
        batch = ids[offset:offset+1000]
        bindings = {f'line_{i}':x for i,x in enumerate(batch)}
        placeholders = ','.join(':'+k for k in bindings)
        # Same order-side view and amount formula as Product Profit. Stable line
        # IDs avoid name joins and counting all products as shipped together.
        frame = _bounded(f"""
SELECT p.order_line_id,p.market_place,p.store,p.sku,p.main_ir,
       p.price_subtotal AS gross_sales,p.promotion AS raw_promotion,
       ABS(p.promotion) AS promo_cost,p.price_subtotal-ABS(p.promotion) AS sales,
       p.units,p.product_cost_unit,p.hardware_cost,p.removed_hardware
FROM bi.ba_profit_order_line_info p
WHERE p.order_line_id IN ({placeholders})
""", bindings, query_fn)
        total_rows += len(frame)
        total_bytes += int(frame.memory_usage(index=True,deep=True).sum())
        if total_rows > int(os.getenv('MAX_QUERY_TOTAL_ROWS', '500000')) or total_bytes > int(os.getenv('MAX_QUERY_MEMORY_MB','256'))*1024*1024:
            raise RuntimeError('生命周期来源超过累计读取上限，不能完整判断')
        frames.append(frame)
    source = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=['order_line_id'])
    return sales, cost, source


def _evaluate(ids, source, rules, day, due):
    counts = source.order_line_id.value_counts()
    ids = set(ids)
    missing = sum(counts.get(i,0)==0 for i in ids)
    duplicated = sum(counts.get(i,0)>1 for i in ids)
    usable_ids = {i for i in ids if counts.get(i,0)==1}
    rows = source[source.order_line_id.isin(usable_ids)].copy()
    excluded = 0
    if not rows.empty:
        allowed = rows.main_ir.notna() & ~rows.main_ir.isin(core.FIXED_EXCLUDED_MAIN_IR)
        excluded = int((~allowed).sum())
        rows = rows[allowed].copy()
    eligible = len(rows)
    if not due:
        rows = rows.iloc[0:0]
    rows['type'] = 'order'
    rows['date_order'] = str(day)  # Report day denotes this cohort's event date.
    findings = core.detect_anomalies(rows, rules)
    groups = service.records(service.summarize_findings(findings, ['marketplace','store','sku','day']))
    totals = {rule:sum(int(g['规则命中样本数']) for g in groups if g['anomaly_rules']==rule) for rule in rules}
    overlap = sum({'missing_product_cost','unit_cost_non_positive'} <= set(v.split(', ')) for v in findings.get('anomaly_rules',[]))
    return {'candidate_lines':len(ids), 'eligible_lines':eligible, 'evaluated_lines':len(rows),
        'not_yet_due_lines':eligible if not due else 0, 'missing_source_lines':int(missing),
        'duplicate_source_lines':int(duplicated), 'excluded_fixed_filter_lines':excluded,
        'rule_counts':totals, 'overlapping_cost_rule_lines':int(overlap),
        'groups':groups, 'unknown_inputs':service.rule_unknowns(rows,rules)}


def review(day, selected_rules, now=None, fetch_fn=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('检查时间必须带时区')
    fetch_fn = fetch_fn or fetch_cohorts
    sales, cost, source = fetch_fn(day)
    sales_due = datetime.combine(day+timedelta(days=1), time(), ZONE)
    cost_due = datetime.combine(day+timedelta(days=1), time(9), ZONE)
    from bi_check_agent.diagnostics import cost_due as reached_cost_deadline
    # Use the same deadline function as interactive and worker assessments.
    end_of_day_scan = utc_bounds(day)[1] - timedelta(microseconds=1)
    sales_rules = [r for r in SALES_RULES if r in selected_rules]
    cost_rules = [r for r in COST_RULES if r in selected_rules]
    sales_result = _evaluate(sales.get('order_line_id',[]),source,sales_rules,day,current>=sales_due)
    unique_cost = cost[cost.line_matches==1] if not cost.empty else cost
    cost_result = _evaluate(unique_cost.get('order_line_id',[]),source,cost_rules,day,reached_cost_deadline(end_of_day_scan,current))
    cost_result.update({'candidate_order_products':len(cost),
        'unmatched_order_products':int((cost.line_matches==0).sum()) if not cost.empty else 0,
        'ambiguous_order_products':int((cost.line_matches>1).sum()) if not cost.empty else 0,
        'created_before_outbound_day':int((pd.to_datetime(unique_cost.created_at)<utc_bounds(day)[0]).sum()) if not unique_cost.empty else 0})
    partial = any(r['missing_source_lines'] or r['duplicate_source_lines'] or r['unknown_inputs'] or r['not_yet_due_lines'] for r in [sales_result,cost_result]) or cost_result['unmatched_order_products'] or cost_result['ambiguous_order_products']
    status = 'empty_unverified' if sales.empty and cost.empty else ('partial_assessment' if partial else 'reviewed')
    return {'status':status, 'job_completion_verified':False,
        'basis':'expected_deadline', 'event_day':str(day), 'source_timezone':'UTC', 'business_timezone':str(ZONE),
        'sales_deadline':sales_due.isoformat(), 'cost_deadline':cost_due.isoformat(),
        'sources':['public.sale_order','public.sale_order_line','public.order_outbound_history','bi.ba_profit_order_line_info'],
        'sales':sales_result,'cost':cost_result,'note':NOTE}


def signature_payload(result):
    if result['status']=='failed':
        return {'status':'failed'}
    return {'status':result['status'], 'basis':result['basis'],
        **{name:{'rule_counts':result[name]['rule_counts'],
                 'missing_source_lines':result[name]['missing_source_lines'],
                 'duplicate_source_lines':result[name]['duplicate_source_lines'],
                 'unknown_inputs':result[name]['unknown_inputs'],
                 'unmatched_order_products':result[name].get('unmatched_order_products',0),
                 'ambiguous_order_products':result[name].get('ambiguous_order_products',0),
                 'groups':[{k:v for k,v in row.items() if k!='day'} for row in result[name]['groups']]}
           for name in ['sales','cost']}}


def markdown(result):
    if result['status']=='failed':
        return ['', '创建/出库时间复核未完成，不能使用订单日期观察结果代替本次复核。', '']
    lines = ['', '## 按创建和出库时间复核', '', result['note'], '',
        '| 检查范围 | 候选数 | 实际检查订单行 | 无来源/来源不唯一 |',
        '| --- | ---: | ---: | ---: |']
    for name,label in [('sales','当天创建的订单'),('cost','当天正数扫描出库的商品')]:
        r=result[name]
        lines.append(f"| {label} | {r.get('candidate_order_products',r['candidate_lines'])} | {r['evaluated_lines']} | {r['missing_source_lines']}/{r['duplicate_source_lines']} |")
    cost=result['cost']
    lines += ['',f"出库商品中，{cost['unmatched_order_products']} 组未匹配订单行，{cost['ambiguous_order_products']} 组对应多条订单行，均未强行分配；唯一匹配中 {cost['created_before_outbound_day']} 组早于本次出库日创建。",'',
        '| 规则 | 超过预期补齐时点仍命中（待核查） |','| --- | ---: |']
    for name in ['sales','cost']:
        for rule,count in result[name]['rule_counts'].items():
            lines.append(f"| {service.RULE_LABELS[rule]} | {count} |")
        if result[name]['unknown_inputs']:
            lines.append(f"\n{name} 存在无法判断的输入：{result[name]['unknown_inputs']}。")
        if result[name]['not_yet_due_lines']:
            lines.append(f"\n{name} 尚未到预期时点：{result[name]['not_yet_due_lines']} 条。")
    groups = result['sales']['groups'] + result['cost']['groups']
    if groups:
        lines += ['',f"时限复核疑点分组（展示 {min(len(groups),15)}/{len(groups)} 组）：",'',
            '| 平台 | 店铺 | SKU | 规则 | 订单行数 |','| --- | --- | --- | --- | ---: |']
        def cell(value):return str(value if value is not None else '未提供').replace('|','\\|').replace('\n',' ')
        for row in sorted(groups,key=lambda r:-r['规则命中样本数'])[:15]:
            lines.append('| '+' | '.join(cell(v) for v in [row['market_place'],row['store'],row['sku'],service.RULE_LABELS[row['anomaly_rules']],row['规则命中样本数']])+' |')
    lines += ['',f"两条成本规则同时命中 {cost['overlapping_cost_rule_lines']} 条订单行，不可相加。这些仍是预期时限下的疑点，未核实任务成功记录，不能判定写入失败。", '']
    return lines
