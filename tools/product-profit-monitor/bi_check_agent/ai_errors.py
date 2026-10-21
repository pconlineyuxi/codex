"""Safe, shared failure categories for parsing and evidence-grounded chat."""

MESSAGES = {
    'quota_exhausted': '模型 API 额度不足或已耗尽。请在当前 API Key 所属组织补充额度，或更换有可用额度的 API 配置。',
    'authentication_failed': '模型 API 身份验证失败，请检查本地 API Key 配置。',
    'model_unavailable': '当前 API 配置无法访问所选模型，请核对模型名称和项目权限。',
    'rate_limited': '模型 API 请求频率受限，请稍后重试。',
    'timeout': '模型 API 请求超时，请稍后重试。',
    'connection_failed': '无法连接模型 API，请检查网络和本地接口配置。',
    'invalid_response': '模型没有返回完整且可验证的结果，请重试。',
    'service_error': '模型 API 暂时无法完成请求，请稍后重试。',
    'not_configured': '尚未配置模型 API Key，请先完成本地模型配置。',
}


def failure_detail(exc):
    """Never expose provider response bodies, request details or credentials."""
    status = getattr(exc, 'status_code', None)
    code = getattr(exc, 'code', None)
    if code in {'credit_balance_exhausted', 'insufficient_quota', 'billing_hard_limit_reached', 'billing_not_active'}:
        category = 'quota_exhausted'
    elif status == 401:
        category = 'authentication_failed'
    elif status in {403, 404}:
        category = 'model_unavailable'
    elif status == 429:
        category = 'rate_limited'
    elif type(exc).__name__ == 'APITimeoutError':
        category = 'timeout'
    elif type(exc).__name__ == 'APIConnectionError':
        category = 'connection_failed'
    elif isinstance(exc, (ValueError, TypeError, KeyError, IndexError, AttributeError)):
        category = 'invalid_response'
    else:
        category = 'service_error'
    return category, MESSAGES[category]


class AIServiceError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in MESSAGES else 'service_error'
        super().__init__(MESSAGES[self.code] + ' 本次 AI 问答未完成；已计算的结果和已有对话均保留。')
