"""Shared read-only assessments. Rule hits are candidates, never root-cause proof."""
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
import os
from zoneinfo import ZoneInfo

import pandas as pd

from bi_check_agent import core
from bi_check_agent.rule_catalog import rules_with_tag

ZONE = ZoneInfo('America/New_York')
KEY = ['order_id', 'market_place', 'store', 'sku', 'ir', 'date_order']
LABELS = {'waiting_data':'等待数据', 'unknown':'无法判断', 'business_observation':'经营观察',
          'suspected_error':'疑似数据错误', 'verified_difference':'已验证数据差异'}
COST_RULES = frozenset(rules_with_tag('lifecycle_cost'))
SALES_RULES = frozenset(rules_with_tag('lifecycle_sales'))


def key_for(row):
    def value(k):
        v = row.get(k)
        if v is None or pd.isna(v): return None
        return pd.Timestamp(v).isoformat() if k == 'date_order' else str(v)
    values=[value(k) for k in KEY]
    if values[-1] is None:
        values.append(None if pd.isna(row.get('day')) else str(row.get('day')))
    return sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def cost_due(scan, now):
    """The business deadline, shared with creation/outbound cohort checks."""
    if scan is None or pd.isna(scan): return None
    stamp = pd.Timestamp(scan)
    stamp = stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp.tz_convert('UTC')
    day = stamp.tz_convert(ZONE).date()
    return now >= datetime.combine(day + timedelta(days=1), time(9), ZONE)


SOURCE_SQL = """
SELECT p.order_line_id,p.order_name AS order_id,p.market_place,p.store,p.sku,p.ir,p.date_order,
       p.product_cost_unit AS source_cost,p.price_subtotal-ABS(p.promotion) AS source_sales,
       sl.product_cost_unit AS raw_cost,so.create_date AS created_at,
       so.state AS order_state,so.walmart_status,
       outbound.last_scan_at, matches.line_matches,
       mv.result_matches,mv.result_cost,mv.result_sales
FROM bi.ba_profit_order_line_info p
JOIN public.sale_order_line sl ON sl.id=p.order_line_id
JOIN public.sale_order so ON so.id=sl.order_id
LEFT JOIN LATERAL (
    SELECT MAX(h.scan_time) AS last_scan_at FROM public.order_outbound_history h
    WHERE h.order_id=sl.order_id AND h.product_id=sl.product_id AND h.scan_qty>0
) outbound ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS line_matches FROM public.sale_order_line x
    WHERE x.order_id=sl.order_id AND x.product_id=sl.product_id
) matches ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS result_matches,MIN(m.product_cost_unit) AS result_cost,
           MIN(m.price_subtotal-ABS(m.promotion)) AS result_sales
    FROM bi.ba_mv_profit_order_line_and_ad_info m
    WHERE m.type='order' AND m.order_name=p.order_name
      AND m.market_place IS NOT DISTINCT FROM p.market_place
      AND m.store IS NOT DISTINCT FROM p.store AND m.sku IS NOT DISTINCT FROM p.sku
      AND m.ir IS NOT DISTINCT FROM p.ir AND m.date_order=p.date_order
      AND m.main_ir NOT IN ('SHIPPING FEE','SHIPMENT DISCOUNT','GIFT WRAPPER FEE','PROMOTION DISCOUNT')
) mv ON TRUE
WHERE p.order_name IN ({placeholders})
  AND p.date_order>=:start AND p.date_order<:end
  AND p.main_ir NOT IN ('SHIPPING FEE','SHIPMENT DISCOUNT','GIFT WRAPPER FEE','PROMOTION DISCOUNT')
LIMIT :max_rows
"""


def fetch_source(rows, request, query_fn):
    names = sorted(set(rows.order_id.dropna().astype(str))) if 'order_id' in rows else []
    frames, total, size = [], 0, 0
    cap = int(os.getenv('MAX_RESULT_ROWS', '50000'))
    if cap < 1: raise ValueError('读取上限必须大于零')
    for offset in range(0, len(names), 200):
        params = {f'o{i}':n for i,n in enumerate(names[offset:offset+200])}
        params.update(start=str(request.start_date),end=str(request.end_date),max_rows=cap+1)
        placeholders = ','.join(':'+k for k in params if k.startswith('o'))
        kind='sales' if set(request.anomaly_rules)&SALES_RULES else 'cost'
        sql=SOURCE_SQL
        if kind=='cost':
            # The deployed profit view takes cost unchanged from this base view.
            # Avoid its unrelated settlement/promotion joins for cost-only probes.
            sql=sql.replace('FROM bi.ba_profit_order_line_info p','FROM bi.ba_order_line_info p').replace('p.price_subtotal-ABS(p.promotion) AS source_sales','NULL::double precision AS source_sales')
        from bi_check_agent.db import source_timezone
        with source_timezone():
            frame = query_fn(sql.format(placeholders=placeholders), params)
        if len(frame)>cap: raise ValueError('原因核查来源超限')
        total += len(frame)
        size += int(frame.memory_usage(index=True,deep=True).sum())
        if total>int(os.getenv('MAX_QUERY_TOTAL_ROWS','500000')) or size>int(os.getenv('MAX_QUERY_MEMORY_MB','256'))*1024*1024:
            raise ValueError('原因核查超过累计上限')
        frame=frame.copy()
        frame['_source_kind']=kind
        frames.append(frame)
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()


def assess(rule, row, source, now, *, matches=1):
    """No free-text causal claims. Each conclusion has explicit prerequisite facts."""
    result = {'assessment':'business_observation','cause_status':'unresolved',
              'reason_code':'business_signal','facts':[], 'missing_evidence':[],
              'conclusion':'规则条件命中，需结合业务判断。'}
    if rule not in COST_RULES | SALES_RULES:
        if rule in rules_with_tag('patrol_data_issue'):
            result.update(assessment='suspected_error',reason_code='rule_candidate',conclusion='规则条件命中，尚需验证业务适用条件。')
        return result
    result.update(assessment='unknown',reason_code='source_unavailable',conclusion='缺少唯一匹配的来源证据，暂不能判断。',
                  missing_evidence=['订单行、实际出库与直接利润来源'])
    if matches != 1:
        result.update(reason_code='ambiguous_match',conclusion='来源或结果不唯一，无法把异常归属到具体订单行。')
        return result
    if source is None: return result
    required = ['order_line_id','result_matches','result_cost','result_sales','source_cost','source_sales','raw_cost','last_scan_at','line_matches','created_at','order_state','walmart_status']
    if any(k not in source for k in required): return result
    if source['order_state']!='sale' or source['walmart_status']=='cancelled':
        result.update(reason_code='business_scope_changed',conclusion='来源业务状态已变化，需要重新核对覆盖范围。')
        return result
    if pd.isna(source['result_matches']) or source['result_matches'] != 1:
        result.update(reason_code='ambiguous_result',conclusion='结果视图缺少唯一对应记录，无法验证本次候选。')
        return result
    field, observed = ('result_cost','product_cost_unit') if rule in COST_RULES else ('result_sales','sales')
    def same(a,b):
        if pd.isna(a) or pd.isna(b): return pd.isna(a) and pd.isna(b)
        return abs(float(a)-float(b))<=1e-8
    if not same(source[field],row.get(observed)):
        result.update(reason_code='snapshot_changed',conclusion='核查时结果已变化，请重新查询后判断。')
        return result
    result['missing_evidence'] = ['对应补齐任务的完成记录及覆盖范围']
    result['facts'] = ['已唯一关联到订单行及本次结果记录。']
    result['measurements']={k:None if pd.isna(source[k]) else float(source[k]) for k in ['raw_cost','source_cost','result_cost','source_sales','result_sales']}
    result['sources']=['public.sale_order_line','public.order_outbound_history',
                       'bi.ba_order_line_info' if source.get('_source_kind')=='cost' else 'bi.ba_profit_order_line_info',
                       'bi.ba_mv_profit_order_line_and_ad_info']
    if rule in COST_RULES:
        if pd.isna(source['line_matches']) or source['line_matches']!=1:
            result.update(reason_code='ambiguous_outbound',conclusion='同一订单商品对应多条订单行，无法确认具体出库归属。')
            return result
        due = cost_due(source['last_scan_at'],now)
        if due is None:
            result.update(assessment='waiting_data',reason_code='not_shipped',conclusion='未发现有效出库扫描，尚不能要求成本已经补齐。')
            return result
        result['facts'].append('已找到有效出库扫描。')
        if not due:
            result.update(assessment='waiting_data',reason_code='before_cost_deadline',conclusion='尚未到出库后次日纽约时间 09:00 的预期成本补齐时点。')
            return result
        result['facts'].append('已超过预期成本补齐时点；此时点不代表任务成功。')
        if pd.isna(source['raw_cost']):
            result.update(assessment='suspected_error',cause_status='pending',reason_code='raw_cost_missing',
                          conclusion='已定位到销售订单行原始成本字段为空；为何未写入仍待任务证据核实。')
            result['facts'].append('销售订单行原始成本字段为空。')
        elif not same(source['source_cost'], source['result_cost']):
            result.update(assessment='verified_difference',cause_status='pending',reason_code='source_result_mismatch',
                          conclusion='同一次来源核查中，直接利润来源与结果视图成本不一致；刷新或计算原因仍待核实。')
            result['facts'].append('同一次数据库读取已验证来源与结果成本不一致。')
        else:
            result.update(assessment='suspected_error',cause_status='pending',reason_code='cost_still_non_positive',
                          conclusion='出库并超过预期时点后，来源成本仍命中规则；需核查成本写入及计算依据。')
    else:
        created = pd.Timestamp(source['created_at'])
        if pd.isna(created): return result
        created = created.tz_localize('UTC') if created.tzinfo is None else created.tz_convert('UTC')
        due = datetime.combine(created.tz_convert(ZONE).date()+timedelta(days=1),time(),ZONE)
        if now < due:
            result.update(assessment='waiting_data',reason_code='before_sales_deadline',conclusion='尚未到创建当日销售补齐的预期时点。')
        elif not same(source['source_sales'],source['result_sales']):
            result.update(assessment='verified_difference',cause_status='pending',reason_code='source_result_mismatch',conclusion='直接利润来源与结果视图销售金额不一致，原因待核实。')
            result['facts'].append('同一次数据库读取已验证来源与结果销售金额不一致。')
        else:
            result.update(assessment='suspected_error',cause_status='pending',reason_code='sales_still_abnormal',conclusion='超过预期补齐时点后来源销售仍命中规则，需核对原始销售及补齐任务。')
    return result


def build(rows, anomalies, request, mode, query_fn, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None: raise ValueError('核查时间必须带时区')
    source, failure = pd.DataFrame(), None
    targeted = bool(set(request.anomaly_rules) & (COST_RULES | SALES_RULES)) and not anomalies.empty
    if targeted and mode!='demo':
        try:
            frames=[]
            for rules in [COST_RULES,SALES_RULES]:
                candidates=anomalies[anomalies['anomaly_rules'].map(lambda value:bool(set(value.split(', ')) & rules))]
                if not candidates.empty:
                    part=request.model_copy(update={'anomaly_rules':sorted(set(request.anomaly_rules)&rules)})
                    frames.append(fetch_source(candidates,part,query_fn))
            source=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()
        except Exception: failure='来源核查未完成，保留候选，不确认原因或恢复。'
    indexed = {}
    for record in source.to_dict('records'):
        indexed.setdefault((key_for(record),record['_source_kind']),[]).append(record)
    counts = Counter(key_for(row) for row in rows.to_dict('records') if row.get('type')=='order')
    grouped = {}
    unresolved = set()
    for row in anomalies.to_dict('records'):
        obj = key_for(row)
        for rule in row['anomaly_rules'].split(', '):
            candidates = indexed.get((obj,'cost' if rule in COST_RULES else 'sales'),[])
            item = assess(rule,row,candidates[0] if len(candidates)==1 else None,now,
                          matches=max(len(candidates),counts.get(obj,1)))
            if mode=='demo':
                item.update(assessment='business_observation' if rule not in COST_RULES|SALES_RULES else 'suspected_error',
                            cause_status='unresolved',reason_code='synthetic_demo',facts=['合成演示样本。'],missing_evidence=[],conclusion='演示规则命中，不代表真实业务原因。')
            if item['assessment'] in {'unknown','waiting_data'}: unresolved.add(obj)
            group = tuple(None if pd.isna(row.get(k)) else str(row.get(k)) for k in ['market_place','store','sku','day'])
            identity = (group,rule,item['assessment'],item['reason_code'])
            measurements=item.pop('measurements',{})
            if identity not in grouped:
                grouped[identity] = {**dict(zip(['market_place','store','sku','day'],group)),**item,'rule':rule,'count':0,'object_keys':[],'measurement_ranges':{}}
            grouped[identity]['count']+=1
            grouped[identity]['object_keys'].append(obj)
            for field,value in measurements.items():
                span=grouped[identity]['measurement_ranges'].setdefault(field,{'min':None,'max':None,'missing_count':0})
                if value is None:span['missing_count']+=1
                else:
                    span['min']=value if span['min'] is None else min(span['min'],value)
                    span['max']=value if span['max'] is None else max(span['max'],value)
    items = list(grouped.values())
    for i,item in enumerate(items):
        item['evidence_id']='diagnosis-'+sha256(json.dumps([request.model_dump(mode='json'),item],sort_keys=True,default=str).encode()).hexdigest()[:16]
        item['assessment_label']=LABELS[item['assessment']]
    return {'schema_version':1,'source_status':'failed' if failure else ('read' if not source.empty else 'not_read'),
            'checked_at':now.isoformat(),
            'source_session_timezone':'UTC','business_timezone':str(ZONE),
            'source_error':failure,'job_completion_verified':False,'assessments':items,
            'unresolved_keys':sorted(unresolved),
            'summary':{state:len({obj for i in items if i['assessment']==state for obj in i['object_keys']}) for state in LABELS},
            'rule_hit_count':sum(i['count'] for i in items),
            'limitations':['任务完成记录尚未接入；数据差异及原始字段缺失不能证明具体任务失败。',
                           '销售与退款抵消不是已确认业务情形，未作为异常解释或豁免。']}


def public(report):
    if report is None: return None
    return {k:([{a:b for a,b in item.items() if a!='object_keys'} for item in value] if k=='assessments' else value)
            for k,value in report.items() if k!='unresolved_keys'}
