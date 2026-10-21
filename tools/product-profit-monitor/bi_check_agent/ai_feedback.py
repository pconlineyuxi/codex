"""Session-local feedback from actual AI requests; no extra paid health calls."""
import streamlit as st

from bi_check_agent.ai_errors import MESSAGES

BILLING_URL = 'https://platform.openai.com/account/billing'
STATE_KEY = 'ai_last_failure'


def remember_ai_outcome(status, code=None):
    if status == 'succeeded':
        st.session_state.pop(STATE_KEY, None)
    elif status in {'failed', 'not_configured'}:
        st.session_state[STATE_KEY] = (
            'not_configured' if status == 'not_configured'
            else code if code in MESSAGES else 'service_error'
        )


def render_ai_notice(slot):
    code = st.session_state.get(STATE_KEY)
    if code not in MESSAGES:
        slot.empty()
        return
    with slot.container():
        if code == 'quota_exhausted':
            st.error('AI 暂不可用：API 额度不足或已耗尽。')
            st.markdown(
                '请由有账单权限的人为当前 API Key 所属组织补充额度。'
                f'[打开 OpenAI API 充值页面]({BILLING_URL})。'
                'ChatGPT 订阅与 API 额度分开计费。'
            )
        else:
            st.warning('上次 AI 调用未完成：' + MESSAGES[code])
        st.caption('AI 解析和数据问答可能受影响；仍可手动设置条件，执行查询、规则巡查和利润计算，已有结果与对话保留。')
        st.caption('以上为本会话最近一次 AI 调用反馈。处理后重新解析或提问，成功后提示会自动清除。')
