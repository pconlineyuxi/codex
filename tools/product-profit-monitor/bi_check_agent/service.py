"""Shared, auditable execution path for interactive queries and scheduled scans."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path

import pandas as pd

from bi_check_agent import core, db
from bi_check_agent.models import AnomalyQueryRequest

RULE_LABELS = {
    'zero_units_with_sales': '数量为零但有销售额', 'zero_sales_with_units': '有数量但销售额为零',
    'negative_units': '负数量待核查', 'negative_sales': '负销售额待核查',
    'negative_shipping_fee': '负运费待核查', 'negative_promotion': '负促销金额待核查',
    'negative_commission': '负佣金待核查', 'negative_ad_spend': '负广告费待核查',
    'missing_product_cost': '产品成本缺失或非正数', 'unit_cost_non_positive': '单件总成本非正数',
    'removed_hardware_abnormal': '拆件成本超过整机与改装成本',
    'shipping_cost_ratio_high': '运费占比偏高', 'unit_shipping_fee_high': '单件运费偏高',
    'promotion_ratio_high': '促销占比偏高', 'ad_spend_ratio_high': '广告占比偏高',
    'ad_spend_without_sales': '有广告支出但无销售', 'profit_rate_with_ads_ship_high': '利润率大于 100%',
}


def _json(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False, allow_nan=False))


def records(frame):
    return json.loads(frame.to_json(orient='records', date_format='iso', force_ascii=False))


def _contract(start, end):
    """Refresh job writes manifest only after all source refreshes commit."""
    if os.getenv('PROFIT_LIVE_CONTRACT_CONFIRMED') != 'yes':
        raise RuntimeError('真实查询尚未启用：请先核对币种、时区和数据刷新契约。')
    path = os.getenv('PROFIT_READY_FILE', '')
    try:
        data = json.loads(Path(path).read_text())
        refreshed = datetime.fromisoformat(data['refreshed_at'].replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - refreshed).total_seconds()
        assert data['status'] == 'complete' and data['snapshot_id']
        assert data['source'] == 'bi.ba_mv_profit_order_line_and_ad_info'
        assert date.fromisoformat(data['coverage_start']) <= start
        assert date.fromisoformat(data['coverage_end']) >= end
        assert data['timezone'] == 'America/New_York'
        assert data['currency'] and data['currency_normalized'] is True
        assert 0 <= age <= int(os.getenv('PROFIT_READY_MAX_AGE_SECONDS', '129600'))
    except Exception:
        raise RuntimeError('无法确认数据就绪：刷新凭据缺失、过期或未覆盖查询窗口。') from None
    return data


def demo_rows(req):
    """Synthetic fixtures generated for the selected dates, never real company data."""
    result=[]
    for offset in range((req.end_date-req.start_date).days):
        day=req.start_date+timedelta(days=offset)
        for i, sku in enumerate(['DEMO-HEALTHY','DEMO-COST','DEMO-SHIP','DEMO-ADS']):
            # Alternate daily numbers to demonstrate deterministic comparison.
            sales=1000.0 + (day.toordinal() % 3)*50
            r=dict(type='order',date_order=day.isoformat(),main_ir=sku,ir=sku,sku=sku,
                   follower='Demo Owner',market_place='Amazon',store='Demo US',
                   order_id=f'DEMO-{day:%Y%m%d}-{i}',gross_sales=sales,promo_cost=20.0,
                   sales=sales-20,units=2.0,product_cost_unit=300.0,hardware_cost=20.0,
                   removed_hardware=0.0,commission=100.0,ad_spend=0.0,shipping_fee=25.0,
                   has_product_cost=True,has_sale_price=True,category='Laptop',brand='Demo')
            if sku=='DEMO-COST':
                r.update(product_cost_unit=None,has_product_cost=False)
            if sku=='DEMO-SHIP':
                r['shipping_fee']=600.0
            if sku=='DEMO-ADS':
                r.update(type='ad_daily',order_id=None,gross_sales=0.,promo_cost=0.,sales=0.,units=0.,
                         product_cost_unit=0.,hardware_cost=0.,removed_hardware=0.,commission=0.,ad_spend=65.,shipping_fee=0.)
            result.append(r)
    d=pd.DataFrame(result)
    mapping=core.load_yaml('filter_mapping.yaml')['filters']
    for field,spec in mapping.items():
        values=getattr(req,field,[])
        if not values: continue
        column={'order_name':'order_id'}.get(spec['sql_field'],spec['sql_field'])
        if column not in d:
            raise ValueError(f'演示样本不包含字段 {field}，请清空此条件或配置真实数据。')
        nulls=any(str(x).upper() in {'NULL','<NULL>'} for x in values)
        normal=[x for x in values if str(x).upper() not in {'NULL','<NULL>'}]
        mask=d[column].isin(normal) | (d[column].isna() if nulls else False)
        if spec['operator']=='not_in': mask=~mask
        d=d[mask]
    d['unit_cost']=d.product_cost_unit+d.hardware_cost-d.removed_hardware
    d['order_cost_total']=d.unit_cost*d.units
    return d


def rule_unknowns(rows, rules):
    required={
        'zero_units_with_sales':['units','sales'], 'zero_sales_with_units':['units','sales'],
        'negative_units':['units'], 'negative_sales':['sales'], 'negative_shipping_fee':['shipping_fee'],
        'negative_promotion':['promo_cost'], 'negative_commission':['commission'],
        'unit_cost_non_positive':['product_cost_unit','hardware_cost','removed_hardware'],
        'removed_hardware_abnormal':['product_cost_unit','hardware_cost','removed_hardware'],
        'shipping_cost_ratio_high':['sales','shipping_fee'], 'unit_shipping_fee_high':['units','shipping_fee'],
        'promotion_ratio_high':['sales','promo_cost'], 'negative_ad_spend':['ad_spend'],
        'ad_spend_ratio_high':['sales','ad_spend'], 'ad_spend_without_sales':['sales','ad_spend'],
        'profit_rate_with_ads_ship_high':['sales','order_cost_total','commission','ad_spend','shipping_fee'],
    }
    aggregate_rules={'negative_ad_spend','ad_spend_ratio_high','ad_spend_without_sales','profit_rate_with_ads_ship_high'}
    result=[]
    for rule in rules:
        subset=rows if rule in aggregate_rules or 'type' not in rows else rows[rows['type']=='order']
        if rule=='promotion_ratio_high' and 'market_place' in subset:
            allowed=core.load_yaml('diagnostic_rules.yaml')['rule_guards'][rule]['skip_when_marketplace_not_in']
            subset=subset[subset.market_place.str.lower().isin([x.lower() for x in allowed])]
        for field in required.get(rule,[]):
            missing=len(subset) if field not in subset else int((~pd.to_numeric(subset[field],errors='coerce').map(__import__('numpy').isfinite)).sum())
            if missing: result.append({'rule':rule,'field':field,'unknown_rows':missing})
    return result


def query(req: AnomalyQueryRequest, mode='demo', progress=None):
    if not req.aggregation_dimensions: raise ValueError('汇总维度不能为空，请至少选择一个汇总维度')
    if mode not in {'demo','live','live_test'}: raise ValueError('未知数据模式')
    _,_,applied,skipped=core.build_order_line_sql(req)
    before=_contract(req.start_date,req.end_date) if mode=='live' else None
    frames=[]
    cap=int(os.getenv('MAX_RESULT_ROWS','50000'))
    if cap < 1: raise ValueError('MAX_RESULT_ROWS 必须大于零')
    if mode=='demo':
        frames=[demo_rows(req)]
        if progress: progress((req.end_date-req.start_date).days,(req.end_date-req.start_date).days)
    else:
        for offset in range((req.end_date-req.start_date).days):
            start=req.start_date+timedelta(days=offset)
            part=req.model_copy(update={'start_date':start,'end_date':start+timedelta(days=1)})
            sql,params,_,_=core.build_order_line_sql(part)
            rows=db.run_query(sql,params)
            if len(rows)>=params['max_rows']:
                raise RuntimeError('单日数据超过读取上限，本次检查不完整。请按店铺或平台缩小计划范围。')
            frames.append(rows)
            if progress: progress(offset+1,(req.end_date-req.start_date).days)
        if mode=='live':
            after=_contract(req.start_date,req.end_date)
            if before != after:
                raise RuntimeError('查询期间刷新凭据发生变化，本次结果不完整，请重试。')
    rows=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()
    if mode=='live' and rows.empty:
        raise RuntimeError('真实查询没有数据，尚不能区分无业务与数据缺失，本次不执行恢复判定。')
    if not rows.empty:
        rows=core._ensure_numeric(core._prepare_date_dimensions(rows))
    anomalies=core.detect_anomalies(rows,req.anomaly_rules)
    evidence={'mode':mode,'complete':mode!='live_test','query_complete':True,'start':str(req.start_date),'end':str(req.end_date),
              'row_count':len(rows),'source':'synthetic_demo' if mode=='demo' else 'bi.ba_mv_profit_order_line_and_ad_info',
              'snapshot': before,'checked_at':datetime.now(timezone.utc).isoformat(),
              'currency':'DEMO USD' if mode=='demo' else ('源金额（币种待核对）' if mode=='live_test' else before['currency']),
              'freshness_verified':mode=='live',
              'testing_note':'真实测试未核实刷新完整性、币种及源日期时区，不能作为正式巡查或恢复证据。' if mode=='live_test' else None,
              'rule_version':sha256((core.CONFIG_DIR/'diagnostic_rules.yaml').read_bytes()).hexdigest()[:16],
              'applied_filters':applied,'unrestricted_filters':skipped,
              'query':req.model_dump(mode='json'),'unknown_rule_inputs':rule_unknowns(rows,req.anomaly_rules),'localization_limit':'已定位到 Product Profit 结果视图，尚未接入上游源表验证。'}
    return {'rows':rows,'aggregate':core.aggregate_product(rows,req.aggregation_dimensions),
            'anomalies':anomalies,'evidence':evidence}


def evaluate_plan(plan,start,end):
    if plan.get('mode','demo') not in {'demo','live'}:
        raise ValueError('真实数据测试模式不能用于定时巡查。')
    rules=plan.get('rules',[])
    if not rules: raise ValueError('巡查计划必须启用至少一条规则')
    if plan.get('mode')=='live' and not plan.get('rules_confirmed',False):
        raise RuntimeError('真实巡查规则尚未完成业务样本复核，请先确认规则。')
    req=AnomalyQueryRequest(start_date=date.fromisoformat(start),end_date=date.fromisoformat(end),
         anomaly_rules=rules, **plan.get('filters',{}))
    result=query(req,plan.get('mode','demo'))
    if result['evidence']['unknown_rule_inputs']:
        raise RuntimeError('规则所需字段存在缺失或不可解析值，本次无法完整判断，原异常不标记恢复。')
    findings={}
    thresholds=core.load_yaml('diagnostic_rules.yaml').get('thresholds',{})
    worsening=plan.get('worsening_deltas',{})
    if not isinstance(worsening,dict) or set(worsening)-set(RULE_LABELS):
        raise ValueError('恶化阈值包含未知规则')
    for delta in worsening.values():
        if not isinstance(delta,(int,float)) or not __import__('math').isfinite(delta) or delta <= 0:
            raise ValueError('恶化增量必须为有限正数')
    for row in records(result['anomalies']):
        for rule in row['anomaly_rules'].split(', '):
            fields={k:row.get(k) for k in ['order_id','sku','ir','market_place','store','day','date_order']}
            key=sha256(json.dumps([rule,fields],sort_keys=True).encode()).hexdigest()
            sales=row.get('sales')
            if sales is None: sales=row.get('Sales')
            units=row.get('units')
            metric,actual,threshold='命中来源记录数',1,None
            if rule=='shipping_cost_ratio_high':metric,actual='运费 / 销售额',row['shipping_fee']/sales
            elif rule=='unit_shipping_fee_high':metric,actual='运费 / 数量',row['shipping_fee']/units
            elif rule=='promotion_ratio_high':metric,actual='促销 / 销售额',row['promo_cost']/sales
            elif rule=='ad_spend_ratio_high':metric,actual='广告 / 销售额',row['Ads cost']/sales
            elif rule=='ad_spend_without_sales':metric,actual='无销售时广告金额',row['Ads cost']
            elif rule=='profit_rate_with_ads_ship_high':metric,actual='利润率',row['Profit rate with Ads & Ship']
            if rule in thresholds:threshold=thresholds[rule]
            item=findings.setdefault(key,{'key':key,'rule':rule,
               'object':' / '.join(str(fields[k]) for k in ['market_place','store','sku','order_id'] if fields.get(k)),
               'value':0,'evidence':{'classification':'疑似数据错误' if rule in {'missing_product_cost','unit_cost_non_positive','zero_units_with_sales'} else '待核查 / 经营信号',
                 'samples':[], 'localization_limit':result['evidence']['localization_limit'],
                 'metric':metric,'threshold':threshold,'affected_sales':0.,'affected_sales_missing':False,
                 'affected_sales_note':'受影响销售额不是损失；跨规则不可直接求和。',
                 'source_record_count':0,'query_evidence':result['evidence'],
                 'rule_label':RULE_LABELS.get(rule,rule)}})
            if metric=='命中来源记录数':item['value']+=1
            else:item['value']=max(item['value'],float(actual))
            item['evidence']['source_record_count']+=1
            if sales is None:item['evidence']['affected_sales_missing']=True
            else:item['evidence']['affected_sales']+=float(sales)
            item['evidence']['actual_value']=item['value']
            if rule in worsening:item['worsening_delta']=float(worsening[rule])
            if len(item['evidence']['samples'])<5:item['evidence']['samples'].append(row)
    return {'findings':list(findings.values()),'evidence':result['evidence']}


def amount_change_percent(previous, current):
    # Percentage change of the amount itself, not its signed profit contribution.
    return None if previous == 0 else (current - previous) / previous * 100


def decompose(previous, current):
    metrics=[('销售额','gross_sales',1),('促销','promo_cost',-1),('产品与改装成本','order_cost_total',-1),
             ('佣金','commission',-1),('广告','ad_spend',-1),('运费','shipping_fee',-1)]
    a=core._ensure_numeric(previous.copy()); b=core._ensure_numeric(current.copy())
    components=[]
    for label,column,sign in metrics:
        for d in [a,b]:
            if column not in d or not d[column].notna().all():
                raise ValueError(f'{label}存在缺失或不可解析值，不能给出完整利润归因；请先检查缺失成本等规则。')
        old=float(a[column].sum());new=float(b[column].sum())
        components.append({'item':label,'previous':old,'current':new,'contribution':round(sign*(new-old),2),'change_percent':amount_change_percent(old,new)})
    oldprofit=sum(sign*float(a[col].sum()) for _,col,sign in metrics)
    newprofit=sum(sign*float(b[col].sum()) for _,col,sign in metrics)
    delta=round(newprofit-oldprofit,2)
    return {'previous_profit':round(oldprofit,2),'current_profit':round(newprofit,2),
            'difference':delta,'components':components,
            'reconciliation_error':round(delta-sum(x['contribution'] for x in components),2),
            'conclusion':'差异贡献是结果视图中的算术分解，尚不能据此确认上游根因。广告仅在所选汇总范围解释。'}


def summarize_findings(anomalies, dimensions):
    """Expose dimension/rule counts only, never underlying order records."""
    if not dimensions: raise ValueError('汇总维度不能为空')
    if anomalies.empty: return pd.DataFrame()
    frame = core._prepare_date_dimensions(anomalies.copy())
    columns = [core.AGGREGATION_DIMENSION_COLUMNS[d] for d in dimensions]
    for column in columns:
        if column not in frame: frame[column] = pd.NA
    frame['anomaly_rules'] = frame['anomaly_rules'].str.split(', ')
    return frame.explode('anomaly_rules').groupby(columns + ['anomaly_rules'], dropna=False).size().reset_index(name='规则命中样本数')


def without_order_samples(value):
    if isinstance(value, dict):
        return {k: without_order_samples(v) for k, v in value.items() if k != 'samples'}
    if isinstance(value, list): return [without_order_samples(v) for v in value]
    return value


def available_business_options(start, end):
    """Read platform/store pairs in the selected date window only."""
    request = AnomalyQueryRequest(start_date=start, end_date=end)
    core.build_where_clause(request)
    frame = db.run_query(
        "SELECT DISTINCT market_place, store FROM bi.ba_mv_profit_order_line_and_ad_info "
        "WHERE date_order >= :start AND date_order < :end "
        "ORDER BY market_place, store",
        {'start': start, 'end': end},
    )
    return records(frame)
