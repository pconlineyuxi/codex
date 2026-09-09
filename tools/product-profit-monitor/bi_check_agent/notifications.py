"""Explicitly gated Feishu private notifications. No credentials in stored errors."""
import json
import os
from collections import Counter
from urllib.parse import urlparse
import requests


class DeliveryUncertain(Exception):
    """The message may have reached Feishu; require manual reconciliation."""


class FeishuSender:
    def __init__(self):
        self.app_id = os.getenv('FEISHU_APP_ID', '')
        self.secret = os.getenv('FEISHU_APP_SECRET', '')
        self.receiver = os.getenv('FEISHU_RECEIVER_OPEN_ID', '')
        self.base_url = os.getenv('MONITOR_PUBLIC_URL', '')
        self.enabled = (os.getenv('FEISHU_ENABLED', '').lower() in ('1', 'true')
                        and os.getenv('FEISHU_RECEIVER_CONFIRMED') == 'Yuxi'
                        and bool(self.app_id and self.secret and self.receiver)
                        and urlparse(self.base_url).scheme == 'https'
                        and bool(urlparse(self.base_url).netloc))

    def send(self, item):
        if not self.enabled or item.get('mode') != 'live' or item.get('shadow', True):
            raise ValueError('Delivery is disabled')
        auth = requests.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                             json={'app_id': self.app_id, 'app_secret': self.secret}, timeout=15)
        auth.raise_for_status()
        token = auth.json()
        if token.get('code') != 0 or not token.get('tenant_access_token'):
            raise RuntimeError('Feishu authentication rejected')
        lines = [f"Product Profit 巡查：{item.get('plan_name', '')}"]
        counts = Counter(e['type'] for e in item['events'])
        lines.append('；'.join(f'{kind}: {count}' for kind, count in sorted(counts.items())))
        evidence = item.get('evidence', {})
        cutoff = evidence.get('data_as_of') or evidence.get('watermark') or (evidence.get('snapshot') or {}).get('refreshed_at')
        if cutoff:
            lines.append('数据截至：' + str(cutoff)[:200])
        for event in item['events'][:10]:
            lines.append(f"{event['type']} | {event.get('window_start', '')} ~ {event.get('window_end', '')}")
            if 'value' in event:
                lines.append(f"规则：{event.get('rule', '')}；对象：{str(event.get('object', ''))[:160]}；实测值：{event['value']}")
            if event.get('incident_id'):
                lines.append(self.base_url.rstrip('/') + '/?incident=' + event['incident_id'] + '&mode=live')
        if len(item['events']) > 10:
            lines.append('其余事件请在工具内查看：' + self.base_url)
        if not any(e.get('incident_id') for e in item['events']):
            lines.append(self.base_url)
        try:
            response = requests.post('https://open.feishu.cn/open-apis/im/v1/messages',
                params={'receive_id_type': 'open_id'},
                headers={'Authorization': 'Bearer ' + token['tenant_access_token']},
                json={'receive_id': self.receiver, 'msg_type': 'text',
                      'content': json.dumps({'text': '\n'.join(lines)}, ensure_ascii=False),
                      'uuid': item['uuid']}, timeout=15)
            if response.status_code >= 500:
                raise DeliveryUncertain('Feishu response requires reconciliation')
            response.raise_for_status()
            body = response.json()
        except (requests.Timeout, requests.ConnectionError, ValueError) as exc:
            raise DeliveryUncertain('Message delivery requires reconciliation') from exc
        if body.get('code') != 0:
            raise RuntimeError('Feishu rejected message')
        if not body.get('data', {}).get('message_id'):
            raise DeliveryUncertain('Missing delivery receipt')
