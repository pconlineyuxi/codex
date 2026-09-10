"""Read-only analysis of the complete filtered, pre-aggregation data snapshot.

No SQL, eval, file access, or model-generated Python is executed here.
"""
from __future__ import annotations

import json
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

BUSINESS = ['type','date_order','day','week','month','market_place','store','sku','ir','main_ir']
ADDITIVE = ['gross_sales','promo_cost','sales','units','order_cost_total','commission','ad_spend','shipping_fee','profit']
UNIT = ['product_cost_unit','hardware_cost','removed_hardware','unit_cost','shipping_ratio','unit_shipping_fee','net_unit_sales']
COLUMNS = BUSINESS + ADDITIVE + UNIT


class Condition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str
    operator: Literal['eq','ne','in','gt','gte','lt','lte','is_null','not_null','contains']
    value: str | float | bool | list[str] | None = None


class Measure(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str
    statistic: Literal['sum','mean','min','max','median','count','distinct_count','missing_count']


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    period: Literal['previous','current','both'] = 'both'
    operation: Literal['statistics','compare','sample'] = 'statistics'
    filters: list[Condition] = Field(default_factory=list, max_length=12)
    group_by: list[str] = Field(default_factory=list, max_length=4)
    measures: list[Measure] = Field(default_factory=list, max_length=10)
    sample_columns: list[str] = Field(default_factory=list, max_length=20)
    sort_by: str | None = None
    descending: bool = True
    limit: int = Field(default=30, ge=1, le=100)

    @model_validator(mode='after')
    def allowlisted(self):
        for column in self.group_by + self.sample_columns + [f.field for f in self.filters] + [m.field for m in self.measures]:
            if column not in COLUMNS: raise ValueError(f'不支持的分析字段：{column}')
        if any(c not in BUSINESS for c in self.group_by): raise ValueError('分组只能使用业务维度。')
        if any(m.statistic=='sum' and m.field not in ADDITIVE for m in self.measures):
            raise ValueError('不能累加单价、单件成本或比率，请使用均值/分布，或对金额与数量分别求和。')
        if any(m.statistic in {'mean','median','sum'} and m.field in BUSINESS for m in self.measures):
            raise ValueError('文本维度不能计算数值统计。')
        return self


def source_frames(snapshot):
    frames = snapshot.get('_source_frames')
    if not frames or set(frames) not in ({'previous','current'}, {'current'}):
        raise ValueError('这份旧分析尚未保留筛选后的源数据，请重新点击“开始利润分析”后再提问。')
    return frames


def prepare(frame):
    d = frame.copy()
    for column in COLUMNS:
        if column in d: continue
        d[column] = pd.NA
    for c in ADDITIVE + UNIT:
        d[c] = pd.to_numeric(d[c],errors='coerce').replace([np.inf,-np.inf],np.nan)
    d['profit'] = d['gross_sales']-d['promo_cost']-d['order_cost_total']-d['commission']-d['ad_spend']-d['shipping_fee']
    d['shipping_ratio'] = d['shipping_fee']/d['sales'].replace(0,np.nan)
    d['unit_shipping_fee'] = d['shipping_fee']/d['units'].replace(0,np.nan)
    d['net_unit_sales'] = d['sales']/d['units'].replace(0,np.nan)
    return d


def apply_filters(frame, conditions):
    result = frame
    for condition in conditions:
        col = result[condition.field]
        value = condition.value
        if condition.field in ADDITIVE+UNIT and condition.operator in {'eq','ne','in'}:
            try: value=[float(v) for v in value] if isinstance(value,list) else float(value)
            except (TypeError,ValueError): raise ValueError('数值筛选需要数字。') from None
        if condition.operator=='is_null': mask=col.isna()
        elif condition.operator=='not_null': mask=col.notna()
        elif condition.operator=='in':
            if not isinstance(value,list): raise ValueError('in 筛选需要值列表。')
            mask=col.isin(value)
        elif condition.operator=='contains': mask=col.astype('string').str.contains(str(value),regex=False,na=False)
        elif condition.operator in {'gt','gte','lt','lte'}:
            if condition.field in ADDITIVE+UNIT:
                col=pd.to_numeric(col,errors='coerce')
                try:value=float(value)
                except (TypeError,ValueError):raise ValueError('数值筛选需要数字。') from None
            elif condition.field in {'date_order','day','week','month'}:
                col=pd.to_datetime(col,errors='coerce',utc=True)
                value=pd.to_datetime(value,utc=True)
            else:raise ValueError('大小比较只支持数值或日期字段。')
            mask={'gt':col.gt,'gte':col.ge,'lt':col.lt,'lte':col.le}[condition.operator](value)
        elif condition.operator=='eq': mask=col.eq(value)
        else: mask=col.ne(value) & col.notna()
        result=result.loc[mask.fillna(False)]
    return result


def statistic(series, name):
    if name=='count': return int(series.notna().sum())
    if name=='missing_count': return int(series.isna().sum())
    if name=='distinct_count': return int(series.nunique(dropna=True))
    # Missing values are not silently ignored to produce an apparently complete metric.
    if series.empty or series.isna().any(): return None
    return getattr(series,name)()


def grouped_statistics(selected, request):
    records=[]
    groups=selected.groupby(request.group_by,dropna=False,sort=False) if request.group_by else [((),selected)]
    for keys,group in groups:
        keys=keys if isinstance(keys,tuple) else (keys,)
        row=dict(zip(request.group_by,keys))
        row['source_record_count']=len(group)
        for measure in request.measures:
            row[measure.field+'__'+measure.statistic]=statistic(group[measure.field],measure.statistic)
            row[measure.field+'__missing_count']=int(group[measure.field].isna().sum())
        records.append(row)
    cols=request.group_by+['source_record_count']+[m.field+'__'+m.statistic for m in request.measures]+[m.field+'__missing_count' for m in request.measures]
    return pd.DataFrame(records,columns=list(dict.fromkeys(cols)))


def analyze(snapshot, arguments):
    request=AnalysisRequest.model_validate(arguments)
    frames=source_frames(snapshot)
    periods=['previous','current'] if request.period=='both' or request.operation=='compare' else [request.period]
    if request.operation=='compare' and set(frames)!={'previous','current'}:
        raise ValueError('本次巡查只有一个时期，无法比较两期，请使用利润变化分析。')
    if request.period=='both' and request.operation!='compare': periods=list(frames)
    if any(p not in frames for p in periods): raise ValueError('本次巡查不包含请求的时期。')
    comparison_frames={}
    evidence={'operation':request.operation,'request':request.model_dump(mode='json'),'periods':{},
              'notes':['所有统计先遍历本次筛选范围内的全部源记录，再应用本次追问条件；返回条数上限只限制展示，不限制参与计算的源数据。',
                       'source_record_count 是来源记录数，不是订单数；包含 order 与 ad_daily。广告应按平台/店铺/SKU/日期分析，不能归到单个订单。',
                       '存在缺失值的数值统计返回 null，同时报告缺失数。mean 是来源行算术平均，不是销量加权平均；net_unit_sales 是销售额/数量推导值，不是原始 price_unit。']}
    for period in periods:
        d=prepare(frames[period])
        selected=apply_filters(d,request.filters)
        missing_fields={c:int(selected[c].isna().sum()) for c in set(request.sample_columns+[m.field for m in request.measures])}
        if request.operation=='sample':
            columns=request.sample_columns or ['type','date_order','market_place','store','sku','units','sales','shipping_fee','ad_spend','profit']
            result=selected[columns].copy()
            result.insert(0,'source_record_ref',[f'{period}:{i}' for i in selected.index])
            if request.sort_by:
                if request.sort_by not in columns:raise ValueError('样本排序字段必须在 sample_columns 中。')
                result=result.sort_values(request.sort_by,ascending=not request.descending,na_position='last')
            total=len(result)
            result=result.head(min(request.limit,30))
        else:
            result=grouped_statistics(selected,request)
            if request.operation=='compare': comparison_frames[period]=result.copy()
            if request.sort_by and not result.empty and request.operation!='compare':
                if request.sort_by not in result:raise ValueError('统计排序字段需为输出字段，例如 shipping_fee__sum。')
                result=result.sort_values(request.sort_by,ascending=not request.descending,na_position='last')
            total=len(result)
            result=result.head(request.limit)
        evidence['periods'][period]={'source_record_count':len(d),'matched_record_count':len(selected),'result_count':total,
            'returned_count':len(result),'truncated':len(result)<total,'missing_fields':missing_fields,
            'rows':json.loads(result.to_json(orient='records',date_format='iso',force_ascii=False))}
    if request.operation=='compare':
        keys=request.group_by or ['_all']
        if not request.group_by:
            for frame in comparison_frames.values(): frame['_all']='全部匹配记录'
        compared=comparison_frames['previous'].merge(comparison_frames['current'],on=keys,how='outer',suffixes=('__previous','__current'),indicator='presence')
        changes=[]
        for measure in request.measures:
            key=measure.field+'__'+measure.statistic
            # Missing groups/values remain unknown rather than fabricated zeros.
            if measure.field not in BUSINESS or measure.statistic in {'count','distinct_count','missing_count'}:
                previous=pd.to_numeric(compared[key+'__previous'],errors='coerce')
                current=pd.to_numeric(compared[key+'__current'],errors='coerce')
                compared[key+'__change']=current-previous
                compared[key+'__change_percent']=(current-previous)/previous.replace(0,np.nan)*100
                changes.append(key+'__change')
        if request.sort_by:
            if request.sort_by not in compared: raise ValueError('比较排序请使用 field__statistic__change 等输出字段。')
            compared=compared.sort_values(request.sort_by,ascending=not request.descending,na_position='last')
        elif changes:
            compared=compared.sort_values(changes[0],key=lambda values:values.abs(),ascending=False,na_position='last')
        total=len(compared)
        compared=compared.head(request.limit)
        evidence['comparison']={'result_count':total,'returned_count':len(compared),'truncated':len(compared)<total,
            'rows':json.loads(compared.to_json(orient='records',date_format='iso',force_ascii=False)),
            'note':'先对全部匹配源记录分组，再连接两期并计算变化；presence=left_only/right_only 表示仅一侧有记录，这类差额不强行以另一侧为零计算。__change 是金额或统计值变化，不是利润贡献。默认按第一项统计的绝对变化排序。'}
    return evidence


def public_evidence(value):
    """Show the analysis steps/counts, without turning the UI into an order export."""
    if isinstance(value,dict):
        if value.get('operation')=='sample':
            return {k:({p:{a:b for a,b in detail.items() if a!='rows'} for p,detail in v.items()} if k=='periods' else public_evidence(v)) for k,v in value.items()}
        return {k:public_evidence(v) for k,v in value.items() if k!='_source_frames'}
    if isinstance(value,list): return [public_evidence(v) for v in value]
    return value


TOOL = {'type':'function','function':{'name':'analyze_filtered_source_data',
    'description':'Analyze the FULL filtered pre-aggregation source records for either/both periods. Use compare with group_by and measures to rank differences across BOTH periods using ALL source rows; sort_by can be shipping_fee__sum__change. statistics computes exact counts/sums/distributions; sample retrieves at most 30 relevant original rows. Never assume samples are the entire dataset. Fields: '+', '.join(COLUMNS)+'. No SQL/Python. Additive sums only; unit cost and ratios cannot be summed. Output measures use field__statistic for sorting.',
    'parameters':AnalysisRequest.model_json_schema()}}
