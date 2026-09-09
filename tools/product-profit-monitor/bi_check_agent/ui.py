from bi_check_agent.service import summarize_findings, without_order_samples, available_business_options
"""Local Product Profit workbench; one shared engine for queries and monitoring."""
from datetime import date, datetime, timedelta
import json
import os
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from bi_check_agent.ai_parser import parse_business_description
from bi_check_agent.models import AnomalyQueryRequest
from bi_check_agent.monitor import MonitorStore
from bi_check_agent.service import RULE_LABELS, query, decompose, evaluate_plan, records


def _now():
    return datetime.now(ZoneInfo('America/New_York')).date()


def _csv(label, frame, filename):
    # Prevent a string cell from becoming an Excel formula on export.
    out=frame.copy()
    for c in out:
        out[c]=out[c].map(lambda x: "'"+x if isinstance(x,str) and x.startswith(('=','+','-','@')) else x)
    st.download_button(label,out.to_csv(index=False).encode('utf-8-sig'),filename,'text/csv')


def _err(exc):
    if isinstance(exc,(RuntimeError,ValueError)):
        st.error(str(exc))
    else:
        st.error('本次操作未完成，请检查本地服务配置。未将失败判定为检查通过。')


def manual_scan(store, mode):
    st.markdown('**手动巡查**')
    saved=[p for p in store.plans() if p.get('mode')==('live' if mode=='live_test' else mode)]
    choices={p['id']:p for p in saved}
    selected=st.selectbox('手动巡查计划',['临时检查']+list(choices),format_func=lambda x:choices[x]['name'] if x in choices else x)
    plan=choices.get(selected)
    rules=st.multiselect('手动巡查规则',list(RULE_LABELS),default=(plan or {}).get('rules',['missing_product_cost','shipping_cost_ratio_high']),format_func=RULE_LABELS.get)
    dims=st.multiselect('手动巡查汇总维度',['marketplace','store','sku','main_ir','ir','day','week','month'],default=(plan or {}).get('filters',{}).get('aggregation_dimensions') or ['marketplace','store','sku'])
    left,right=st.columns(2)
    start=left.date_input('巡查开始日期',_now()-timedelta(days=(plan or {}).get('lookback_days',1)))
    end=right.date_input('巡查结束日期（不包含）',_now())
    st.caption('复用所选计划的业务筛选；临时检查覆盖全部店铺。手动触发不启用后台计划或发送通知。')
    if not rules or not dims: st.warning('请至少选择一条规则和一个汇总维度。')
    if st.button('立即巡查',type='primary',disabled=not rules or not dims):
        try:
            filters={**(plan or {}).get('filters',{}),'aggregation_dimensions':dims}
            request=AnomalyQueryRequest(start_date=start,end_date=end,anomaly_rules=rules,**filters)
            with st.spinner('执行手动巡查…'):
                result=query(request,mode)
                summary=summarize_findings(result['anomalies'],dims)
                # Manual runs are isolated from formal recovery and notification state.
                from pathlib import Path
                from uuid import uuid4
                record={'id':str(uuid4()),'plan_id':(plan or {}).get('id'),'evidence':result['evidence'],'summary':records(summary)}
                folder=Path(os.getenv('PROFIT_STATE_DB','.runtime/monitor.sqlite3')).parent/'manual-scans'
                folder.mkdir(parents=True,exist_ok=True)
                (folder/(record['id']+'.json')).write_text(json.dumps(record,ensure_ascii=False,default=str))
                st.session_state['manual_scan_result']=(mode,record)
            st.success('手动巡查完成，已保存独立检查记录；未更新正式异常恢复状态或发送通知。')
        except Exception as exc:_err(exc)
    cached=st.session_state.get('manual_scan_result')
    if cached and cached[0]==mode:
        record=cached[1]
        st.caption(f"检查范围 {record['evidence']['start']} — {record['evidence']['end']} · {record['evidence']['row_count']} 条来源记录")
        frame=pd.DataFrame(record['summary'])
        if frame.empty: st.info('所选规则未命中；数据完整性仍以查询凭据为准。')
        else: st.dataframe(frame,width='stretch',hide_index=True)
        st.download_button('下载手动巡查记录',json.dumps(record,ensure_ascii=False,indent=2,default=str),'manual-scan.json','application/json')


def overview(store, mode):
    st.subheader('巡查概览')
    st.caption('先确认检查是否完成，再看异常。没有检查记录不等于没有问题。')
    runs=[r for r in store.runs() if r.get('mode')==mode]
    incidents=[i for i in store.incidents() if i.get('mode')==mode]
    cols=st.columns(4)
    cols[0].metric('待核查异常',sum(i['status']=='open' for i in incidents))
    cols[1].metric('已恢复',sum(i['status']=='recovered' for i in incidents))
    cols[2].metric('失败 / 未完成',sum(r['status']!='succeeded' for r in runs))
    cols[3].metric('检查窗口',len(runs))
    if not runs:
        st.info('还没有巡查记录。先运行一次演示巡查，查看异常证据与后续复查。')
    elif runs:
        last=next((r for r in runs if r['status']=='succeeded'),None)
        if last: st.success(f"最近完成窗口：{last['window_start']} 至 {last['window_end']}（结束日期不包含）")
    if mode=='demo':
        st.markdown('**体验路径**：运行一次巡查 → 打开异常详情 → 重复运行验证去重。查询页可试用 `DEMO-COST` 和 `DEMO-SHIP`。')
        if st.button('运行一次演示巡查',type='primary'):
            plan=next((p for p in store.plans() if p['id']=='demo-daily'),None)
            if plan is None:
                plan=store.save_plan({'id':'demo-daily','name':'Product Profit 每日演示','mode':'demo','shadow':True,'enabled':True,
                     'hour':9,'minute':0,'lookback_days':1,'rules':['missing_product_cost','shipping_cost_ratio_high','ad_spend_without_sales'],'filters':{}})
            with st.spinner('检查演示数据并保存证据…'):
                result=store.run(plan,str(_now()-timedelta(days=1)),str(_now()),evaluate_plan)
            if result['status']=='succeeded': st.rerun()
            else: st.error(result.get('error','检查未完成'))
    if runs:
        st.markdown('**运行记录**')
        data=[{k:r.get(k) for k in ['window_start','window_end','status','attempts','error']} for r in runs[:100]]
        st.dataframe(pd.DataFrame(data),width='stretch',hide_index=True)
    if incidents:
        st.markdown('**最近异常**')
        for i in incidents[:8]:
            with st.container(border=True):
                st.markdown(f"**{RULE_LABELS.get(i['rule'],i['rule'])}** · {'待核查' if i['status']=='open' else '已恢复'}")
                st.write(i['object'])
                st.caption(f"数据窗口 {i['window_start']} — {i['window_end']} · 实测值 {i['value']:.4g}")
                st.link_button('查看证据与历史',f"?incident={i['id']}&mode={mode}")


def queries(mode):
    st.subheader('业务问题定位')
    st.write('描述问题，核对范围，再用同一套规则查看证据。')
    text=st.text_area('业务问题',placeholder='查昨天 SKU DEMO-COST 的产品成本缺失数据',key='question')
    if st.button('解析为查询草稿'):
        try:
            parsed=parse_business_description(text)
            st.session_state['parsed_query']=parsed
        except Exception as exc:_err(exc)
    parsed=st.session_state.get('parsed_query',{})
    if parsed:
        st.caption('解析结果仅作为草稿；以下实际执行范围需要你核对。')
        with st.expander('查看解析结果与未识别条件',expanded=bool(parsed.get('missing_required_fields') or parsed.get('unresolved_terms'))): st.json(parsed)
    def parsed_date(name,fallback):
        try:return date.fromisoformat(parsed.get(name,''))
        except (ValueError,TypeError):return fallback
    c1,c2=st.columns(2)
    start=c1.date_input('开始日期',value=parsed_date('start_date',_now()-timedelta(days=1 if mode=='live_test' else 7)))
    end=c2.date_input('结束日期（不包含）',value=parsed_date('end_date',_now()))
    # Use form keys tied to parser output so a new draft fills all filter fields.
    import hashlib
    draft=hashlib.sha256(json.dumps(parsed,sort_keys=True,default=str).encode()).hexdigest()[:8]
    c1,c2,c3=st.columns(3)
    filters={}
    fields=[('sku','SKU'),('ir','IR'),('main_ir','Main IR'),('marketplace','平台'),('store','店铺'),('order_id','订单号')]
    window_key=f'{start.isoformat()}:{end.isoformat()}'
    loaded=st.session_state.get('business_options',{})
    if loaded.get('window')!=window_key: loaded={}
    if mode!='demo':
        if st.button('加载当前日期范围内的平台和店铺选项'):
            try:
                with st.spinner('读取平台和店铺名称…'):
                    loaded={'window':window_key,'pairs':available_business_options(start,end)}
                    st.session_state['business_options']=loaded
                if not loaded['pairs']: st.info('当前日期范围未找到平台和店铺选项。')
            except Exception as exc: _err(exc)
        st.caption('平台、店铺均可多选；留空不限制。修改日期后请重新加载选项，选择平台后店铺选项会相应更新。')
    for idx,(key,label) in enumerate(fields):
        if key in {'marketplace','store'} and mode!='demo':
            pairs=loaded.get('pairs',[])
            if key=='marketplace':
                options=sorted({r['market_place'] for r in pairs if r.get('market_place')})
            else:
                platforms=filters.get('marketplace',[])
                options=sorted({r['store'] for r in pairs if r.get('store') and (not platforms or r.get('market_place') in platforms)})
            if not loaded: options=list(dict.fromkeys(options+parsed.get(key,[])))
            defaults=[v for v in parsed.get(key,[]) if v in options]
            option_key=hashlib.sha256(json.dumps(options,ensure_ascii=False).encode()).hexdigest()[:8]
            value=[c1,c2,c3][idx%3].multiselect(label+'（留空为全部）',options,default=defaults,key=draft+window_key+key+option_key)
            if value: filters[key]=value
            continue
        value=[x.strip() for x in [c1,c2,c3][idx%3].text_input(label,', '.join(parsed.get(key,[])),key=draft+key).split(',') if x.strip()]
        if value:filters[key]=value
    with st.expander('更多已支持的业务筛选'):
        for key,label in [('follower','负责人'),('category','品类'),('brand','品牌'),('category_exclude','排除品类'),('ir_exclude','排除 IR'),('attribute_use_for','用途'),('attribute_screen_size','屏幕尺寸'),('attribute_touch','触屏'),('attribute_processor_series','处理器系列')]:
            vals=[x.strip() for x in st.text_input(label,', '.join(parsed.get(key,[])),key=draft+key).split(',') if x.strip()]
            if vals:filters[key]=vals
    with st.expander('销售价格与成本标记'):
        for key,label in [('has_product_cost','有产品成本标记'),('has_sale_price','有销售价格标记')]:
            default=parsed.get(key,[])
            # Parser values are validated booleans, never infer absence as false.
            selected=st.multiselect(label,[True,False],default=[v for v in default if isinstance(v,bool)],
                format_func=lambda x:'是' if x else '否',key=draft+key)
            if selected:filters[key]=selected
    dims=st.multiselect('汇总维度',['main_ir','ir','sku','marketplace','store','day','week','month'],default=parsed.get('aggregation_dimensions') or ['marketplace','store','sku'])
    if not dims: st.warning('汇总维度不能为空，请至少选择一个汇总维度。')
    rules=st.multiselect('检查规则（留空只查询）',list(RULE_LABELS),default=parsed.get('anomaly_rules',[]),format_func=RULE_LABELS.get)
    unresolved=parsed.get('unresolved_terms',[]) or parsed.get('unsupported_filters',{})
    accepted=st.checkbox('我已核对未识别内容，并确认以下范围完整表达本次问题',value=False) if unresolved else True
    if st.button('执行查询与检查',type='primary',disabled=not accepted or not dims):
        try:
            request=AnomalyQueryRequest(start_date=start,end_date=end,aggregation_dimensions=dims,anomaly_rules=rules,**filters)
            with st.spinner('读取数据并核对规则…'):result=query(request,mode)
            st.session_state['result']=(mode,result)
        except Exception as exc:_err(exc)
    cached=st.session_state.get('result')
    if cached and cached[0]==mode:
        result=cached[1]
        st.divider()
        st.markdown('**查询结果**')
        st.caption(f"实际范围 {result['evidence']['start']} — {result['evidence']['end']} · {result['evidence']['row_count']} 条来源记录 · {result['evidence']['currency']}")
        st.dataframe(result['aggregate'],width='stretch',hide_index=True)
        _csv('下载聚合结果',result['aggregate'],'profit-summary.csv')
        if result['evidence'].get('unknown_rule_inputs'):
            st.warning('部分规则所需字段缺失，本次不能确认这些规则通过。请查看查询凭据中的 unknown_rule_inputs。')
        if result['anomalies'].empty:st.info('未命中所选规则；未选规则或无数据时不代表数据正确。')
        else:
            st.markdown('**疑似异常与经营信号**')
            summary=summarize_findings(result['anomalies'],result['evidence']['query']['aggregation_dimensions'])
            st.dataframe(summary,width='stretch',hide_index=True)
            _csv('下载异常汇总',summary,'profit-findings-summary.csv')
        st.info(result['evidence']['localization_limit'])
        with st.expander('查询凭据'):
            st.json(result['evidence'])
    st.divider()
    st.markdown('**两个时期的利润差异分解**')
    st.caption('复用上面的业务范围。演示时选择 DEMO-HEALTHY 或 DEMO-SHIP；DEMO-COST 会因成本缺失阻止完整归因。')
    c1,c2=st.columns(2)
    prev_start=c1.date_input('对比期开始',_now()-timedelta(days=14))
    prev_end=c2.date_input('对比期结束（不包含）',_now()-timedelta(days=7))
    if st.button('比较利润变化',disabled=not accepted or not dims):
        try:
            with st.spinner('按同一口径比较两个时期…'):
                a=query(AnomalyQueryRequest(start_date=prev_start,end_date=prev_end,aggregation_dimensions=dims,**filters),mode)
                b=query(AnomalyQueryRequest(start_date=start,end_date=end,aggregation_dimensions=dims,**filters),mode)
                if a['evidence']['currency']!=b['evidence']['currency']:raise ValueError('两个时期币种不一致，不能直接比较。')
                if mode=='live' and a['evidence']['snapshot']!=b['evidence']['snapshot']:raise ValueError('两个时期读取时刷新状态改变，请重新比较。')
                report=decompose(a['rows'],b['rows'])
            cols=st.columns(3)
            for col,label,key in zip(cols,['对比期利润','当前期利润','利润变化'],['previous_profit','current_profit','difference']):col.metric(label,f"{report[key]:,.2f}")
            frame=pd.DataFrame(report['components'])
            st.dataframe(frame,width='stretch',hide_index=True)
            st.bar_chart(frame.set_index('item')['contribution'],color='#0f766e')
            st.caption(f"舍入差：{report['reconciliation_error']}；正值增加利润，负值减少利润。")
            st.info(report['conclusion'])
        except Exception as exc:_err(exc)


def history(store,mode):
    st.subheader('异常证据与历史')
    items=[i for i in store.incidents() if i.get('mode')==mode]
    if not items:
        st.info('当前模式暂无异常记录。请先运行巡查。');return
    ids=[i['id'] for i in items];by={i['id']:i for i in items}
    desired=st.query_params.get('incident')
    selected=st.selectbox('选择异常',ids,index=ids.index(desired) if desired in ids else 0,
        format_func=lambda x:f"{RULE_LABELS.get(by[x]['rule'],by[x]['rule'])} · {by[x]['object']} · {by[x]['window_start']}")
    detail=without_order_samples(store.incident(selected))
    st.write('状态：'+('待核查' if detail['status']=='open' else '已恢复'))
    st.json(detail.get('evidence',{}))
    st.markdown('**检查历史**');st.json(detail.get('history',[]))
    st.download_button('下载异常凭据',json.dumps(detail,ensure_ascii=False,indent=2,default=str),'incident.json','application/json')


def plans(store,mode):
    st.subheader('巡查计划')
    st.caption('每日在纽约时区的指定时间后执行；本地电脑休眠或后台停止时不会运行。真实模式默认影子运行。')
    plans=[p for p in store.plans() if p.get('mode')==mode]
    choice=st.selectbox('编辑计划',['新建计划']+[p['id'] for p in plans])
    current=next((p for p in plans if p['id']==choice),{})
    name=st.text_input('计划名称',current.get('name','Product Profit 每日巡查'),key=choice+'name')
    c1,c2,c3=st.columns(3)
    hour=c1.number_input('执行小时',0,23,current.get('hour',9),key=choice+'h')
    minute=c2.number_input('执行分钟',0,59,current.get('minute',0),key=choice+'m')
    days=c3.number_input('回看完整业务日',1,30,current.get('lookback_days',1),key=choice+'d')
    rules=st.multiselect('启用规则',list(RULE_LABELS),default=current.get('rules',['missing_product_cost','shipping_cost_ratio_high','ad_spend_without_sales']),format_func=RULE_LABELS.get,key=choice+'rules')
    filters=st.text_area('业务范围 JSON',json.dumps(current.get('filters',{}),ensure_ascii=False,indent=2),key=choice+'filters',help='例：{"marketplace":["Amazon"],"store":["Store A"]}；空对象检查全部范围。')
    worsening_text=st.text_area('恶化增量 JSON（空对象表示不启用恶化通知）',json.dumps(current.get('worsening_deltas',{}),ensure_ascii=False),key=choice+'worsening',help='例如 {"shipping_cost_ratio_high":0.1}：运费占比比上次通知增加 10 个百分点才升级。')
    enabled=st.checkbox('启用后台巡查',value=current.get('enabled',True),key=choice+'enabled')
    shadow=st.checkbox('影子运行：只保存结果，不发送飞书',value=current.get('shadow',True),disabled=mode=='demo',key=choice+'shadow')
    confirmed=st.checkbox('已用真实异常与正常样本核对这些规则',value=current.get('rules_confirmed',False),disabled=mode=='demo',key=choice+'confirm')
    if st.button('保存计划',type='primary'):
        try:
            f=json.loads(filters)
            if not isinstance(f,dict):raise ValueError('业务范围必须为 JSON 对象')
            allowed=set(AnomalyQueryRequest.model_fields)-{'data_domain','start_date','end_date','anomaly_rules','output_level','unsupported_filters'}
            if set(f)-allowed:raise ValueError('业务范围中包含不支持的字段')
            AnomalyQueryRequest(start_date=_now()-timedelta(days=1),end_date=_now(),**f)
            if not rules:raise ValueError('请至少启用一条规则')
            worsening=json.loads(worsening_text)
            import math
            if not isinstance(worsening,dict) or set(worsening)-set(rules) or any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in worsening.values()):
                raise ValueError('恶化增量需对应已选规则，且为有限正数。')
            payload={**current,'name':name,'mode':mode,'enabled':enabled,'shadow':True if mode=='demo' else shadow,
                     'hour':hour,'minute':minute,'lookback_days':days,'worsening_deltas':worsening,'rules':rules,'filters':f,'rules_confirmed':confirmed}
            if not current:payload.pop('id',None)
            store.save_plan(payload);st.success('计划已保存；后台进程运行时会按计划执行。')
        except Exception as exc:_err(exc)
    if current:
        if st.button('立即检查昨天的数据'):
            result=store.run(current,str(_now()-timedelta(days=1)),str(_now()),evaluate_plan)
            if result['status']=='succeeded':st.success('检查完成，请到异常历史查看证据。')
            else:st.error(result.get('error','检查未完成'))
    with st.expander('通知队列与配置要求'):
        st.write('飞书只发送给已确认的 Yuxi 接收人。应用凭证通过 .env 配置；演示和影子运行不发送。')
        items=[x for x in store.outbox() if x.get('mode')==mode or x.get('plan',{}).get('mode')==mode]
        st.json(items[:20])


def main():
    load_dotenv()
    st.set_page_config(page_title='Product Profit · 数据巡查',page_icon='🔎',layout='wide')
    st.markdown('''<style>
    .stApp {background:#f6f8fb} h1,h2,h3 {color:#102c41}
    [data-testid="stSidebar"] {background:#eaf0f5}
    [data-testid="stMetric"] {background:white;border:1px solid #dce5ec;padding:18px;border-radius:12px}
    .block-container {padding-top:2.2rem;max-width:1350px}
    </style>''',unsafe_allow_html=True)
    with st.sidebar:
        st.markdown('### PRODUCT PROFIT')
        st.caption('数据巡查与问题定位')
        mode=st.selectbox('数据模式',['demo','live_test','live'],index={'demo':0,'live_test':1,'live':2}.get(st.query_params.get('mode','demo'),0),format_func=lambda x:{'demo':'演示数据','live_test':'真实数据测试（只读）','live':'正式巡查数据（只读）'}[x])
        sections=['巡查概览','业务问题定位','异常历史','巡查计划']
        page=st.radio('工作区',sections,index=2 if st.query_params.get('incident') else (1 if mode=='live_test' else 0))
        st.divider();st.caption('纽约时间 · 本地开发版')
        st.caption('演示与真实数据分别记录。所有异常都需要证据，不自动修复数据。')
        st.link_button('GitHub 项目','https://github.com/pconlineyuxi/codex')
    st.title('Product Profit 数据工作台')
    if mode=='demo':st.warning('演示模式 · 以下均为合成样本，不代表公司实际数据，不会发送飞书。')
    elif mode=='live_test':st.warning('真实数据测试 · 店铺可多选或留空查询全部，最多一年（366 天，含闰年）；刷新完整性、币种和源日期时区待核对。不用于定时巡查、异常恢复或飞书通知。')
    else:st.info('正式巡查数据 · 只读查询；需要数据库配置与可信刷新凭据。')
    store=MonitorStore(os.getenv('PROFIT_STATE_DB','.runtime/monitor.sqlite3'))
    monitor_mode='live' if mode=='live_test' else mode
    if mode=='live_test' and page!='业务问题定位':
        st.info('这里展示真实巡查记录和计划。手动测试查询可直接使用；正式巡查执行前仍需确认数据刷新、币种和时区。')
    if page=='巡查概览':
        overview(store,monitor_mode)
        manual_scan(store,mode)
    elif page=='业务问题定位':queries(mode)
    elif page=='异常历史':history(store,monitor_mode)
    else:plans(store,monitor_mode)
