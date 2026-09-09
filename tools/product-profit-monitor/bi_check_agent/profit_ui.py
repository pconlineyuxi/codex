"""Independent comparison workflow with persistent results and scoped chat."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import os

import pandas as pd
import streamlit as st

from bi_check_agent.models import AnomalyQueryRequest
from bi_check_agent import profit_analysis as analysis
from bi_check_agent.service import amount_change_percent


def render(mode, business_filters, csv, error):
    st.subheader('利润变化分析')
    st.write('比较同一业务范围的两个时期，看看利润变了多少、哪些收入或费用贡献最大，再就结果继续提问。')
    today = datetime.now(ZoneInfo('America/New_York')).date()
    # The independent form may be initialized from the most recent executed query.
    settings = st.session_state.get('profit_settings_'+mode,{})
    seed = settings.get('current',st.session_state.get('profit_seed_'+mode))
    if seed is None:
        last = st.session_state.get('result')
        seed = last[1]['evidence']['query'] if last and last[0]==mode else {}
        st.session_state['profit_seed_'+mode] = seed
    from datetime import date
    c1,c2=st.columns(2)
    start=c1.date_input('本期开始日期',date.fromisoformat(seed['start_date']) if seed.get('start_date') else today-timedelta(days=7),key='profit_start')
    inclusive_end=c2.date_input('本期结束日期（包含当天）',date.fromisoformat(seed['end_date'])-timedelta(days=1) if seed.get('end_date') else today-timedelta(days=1),key='profit_end')
    end=inclusive_end+timedelta(days=1)
    period=st.radio('对比方式',['上一等长周期','自定义对比期'],index=1 if settings.get('period')=='自定义对比期' else 0,horizontal=True,key='profit_period')
    if end<=start:
        st.warning('本期结束日期不能早于开始日期。')
        return
    previous_start,previous_end=analysis.previous_equal_period(start,end)
    if period=='自定义对比期':
        c1,c2=st.columns(2)
        previous_start=c1.date_input('对比期开始日期',date.fromisoformat(settings['previous']['start_date']) if settings.get('previous') else previous_start,key='profit_previous_start')
        previous_end=c2.date_input('对比期结束日期（包含当天）',date.fromisoformat(settings['previous']['end_date'])-timedelta(days=1) if settings.get('previous') else previous_end-timedelta(days=1),key='profit_previous_end')+timedelta(days=1)
    days=(end-start).days
    previous_days=(previous_end-previous_start).days
    st.info(f'本期：{start} 至 {inclusive_end}（{days} 天）　｜　对比期：{previous_start} 至 {previous_end-timedelta(days=1)}（{previous_days} 天）')
    if days != previous_days: st.warning('两个时期天数不同，总额变化同时受周期长度影响；不能直接理解为经营效率变化。')
    if max(start,previous_start)<min(end,previous_end): st.warning('两个时期存在日期重叠，请确认这是你希望比较的范围。')
    filters=business_filters(mode,start,end,seed,namespace='profit_'+mode,windows=[(previous_start,previous_end),(start,end)])
    dims=st.multiselect('分析汇总维度',['marketplace','store','sku','main_ir','ir'],default=[d for d in seed.get('aggregation_dimensions',['marketplace','store','sku']) if d in {'marketplace','store','sku','main_ir','ir'}] or ['marketplace','store','sku'],key='profit_dims')
    scope_text='；'.join(f'{k}：{", ".join(map(str,v))}' for k,v in filters.items()) or '全部平台、店铺和产品'
    st.caption('本次业务范围：'+scope_text+'。两个时期使用相同筛选；整体差异与各期汇总数据分开展示。')
    if not dims: st.warning('请至少选择一个分析汇总维度。')
    current=previous=None
    fingerprint=None
    try:
        current=AnomalyQueryRequest(start_date=start,end_date=end,aggregation_dimensions=dims,**filters)
        previous=AnomalyQueryRequest(start_date=previous_start,end_date=previous_end,aggregation_dimensions=dims,**filters)
        fingerprint=analysis.scope_key(current,previous,mode)
        st.session_state['profit_settings_'+mode]={'current':current.model_dump(mode='json'),'previous':previous.model_dump(mode='json'),'period':period}
    except ValueError:
        st.warning('请核对日期范围与汇总维度后再分析。')
    run=st.button('开始利润分析',type='primary',disabled=fingerprint is None)
    progress_slot=st.empty()
    result_slot=st.empty()
    if run:
        st.session_state['profit_last_error']=False
        progress=progress_slot.progress(0,text='准备读取对比期数据…')
        try:
            snapshot=analysis.compare(current,previous,mode,progress=lambda value,label:progress.progress(min(value,1.0),text=label))
            st.session_state['profit_snapshot']=snapshot
            st.session_state['profit_chat']=[]
            progress.progress(1.0,text='分析完成，结果与对话已绑定到本次数据。')
        except Exception as exc:
            st.session_state['profit_last_error']=True
            progress.empty()
            error(exc)
    with result_slot.container():
        snapshot=st.session_state.get('profit_snapshot')
        if not snapshot or snapshot['mode']!=mode:
            st.info('完成分析后，这里会显示利润差额、费用贡献和数据问答。')
            return
        stale=fingerprint!=snapshot['scope_key'] or st.session_state.get('profit_last_error',False)
        if stale: st.warning('条件已变化或重新分析未完成。下方保留上次成功结果；请重新分析后继续提问。')
        st.divider()
        st.markdown('**分析结果**')
        st.caption(f"结果对应：本期 {snapshot['current_request']['start_date']} 至 {snapshot['current_request']['end_date']}（结束日不含），对比期 {snapshot['previous_request']['start_date']} 至 {snapshot['previous_request']['end_date']}（结束日不含）。金额口径：{snapshot['currency']}")
        report=snapshot['report']
        cols=st.columns(3)
        for col,label,key in zip(cols,['对比期利润','本期利润','利润变化'],['previous_profit','current_profit','difference']):
            col.metric(label,f'{report[key]:,.2f}')
        biggest=max(report['components'],key=lambda row:abs(row['contribution']))
        direction='增加' if report['difference']>=0 else '减少'
        st.write(f"本期利润比对比期{direction} {abs(report['difference']):,.2f}；按绝对金额，影响最大的是{biggest['item']}，对利润变化的贡献为 {biggest['contribution']:+,.2f}。")
        frame=pd.DataFrame(report['components']).rename(columns={'item':'项目','previous':'对比期金额','current':'本期金额','contribution':'对利润变化的贡献'})
        frame['金额变化百分比']=[amount_change_percent(c['previous'],c['current']) for c in report['components']]
        frame=frame.drop(columns=['change_percent'],errors='ignore')
        frame['金额变化百分比']=frame['金额变化百分比'].map(lambda x:'—' if pd.isna(x) else f'{x:+.2f}%')
        st.bar_chart(frame.set_index('项目')['对利润变化的贡献'],color='#0f766e')
        st.dataframe(frame,width='stretch',hide_index=True)
        st.caption('金额变化百分比 =（本期金额－对比期金额）÷对比期金额；对比期为 0 时显示 —。负基数时请结合金额判断，不用百分比符号判断改善或恶化。')
        st.caption('正值增加利润，负值减少利润。销售额行是扣促销前金额；利润已扣促销、产品与改装成本、佣金、广告和运费。')
        st.caption(report['conclusion'])
        if snapshot['current_evidence'].get('testing_note'): st.info(snapshot['current_evidence']['testing_note'])
        with st.expander('查看两个时期的汇总数据'):
            for p,label in [('previous','对比期'),('current','本期')]:
                st.markdown('**'+label+'**')
                table=pd.DataFrame(snapshot[p+'_aggregate'])
                st.dataframe(table,width='stretch',hide_index=True)
                csv('下载'+label+'汇总数据',table,'profit-'+p+'.csv')
        with st.expander('本次计算与范围凭据'):
            st.json({k:snapshot[k] for k in ['id','current_request','previous_request','current_evidence','previous_evidence']})
        st.markdown('**就这份数据继续提问**')
        st.caption(f"模型：{os.getenv('OPENAI_MODEL','gpt-5.4')}。发送问题时使用本次汇总数据、费用贡献和最近对话；不发送订单行。")
        st.caption('例如：利润下降主要来自哪项费用？广告费用的变化能否解释利润下降？这份数据还不足以证明什么？')
        history=st.session_state.setdefault('profit_chat',[])
        if history and st.button('清空本次对话'):
            st.session_state['profit_chat']=[]
            history=[]
        for item in history:
            with st.chat_message(item['role']):
                st.markdown(item['content'])
                if item.get('coverage') and not item['coverage']['complete']:
                    st.caption(f"本回答包含 {item['coverage']['included_aggregate_rows']}/{item['coverage']['total_aggregate_rows']} 条汇总记录，整体费用贡献覆盖全范围。")
                if item.get('evidence'):
                    with st.expander('查看回答所用数据与证据编号'): st.json(item['evidence'])
        question=st.chat_input('针对本次利润对比，输入你的问题…',disabled=stale,key='profit_question')
        if question:
            with st.chat_message('user'): st.markdown(question)
            try:
                with st.chat_message('assistant'):
                    with st.spinner('结合本次分析数据回答…'):
                        reply=analysis.answer(snapshot,question,history)
                    st.markdown(reply['content'])
                    if not reply['coverage']['complete']:
                        st.caption(f"本回答包含 {reply['coverage']['included_aggregate_rows']}/{reply['coverage']['total_aggregate_rows']} 条汇总记录；不是全量对象排名。")
                    with st.expander('查看回答所用数据与证据编号'):st.json(reply['evidence'])
                history.extend([{'role':'user','content':question},{'role':'assistant',**reply}])
            except Exception as exc:error(exc)
