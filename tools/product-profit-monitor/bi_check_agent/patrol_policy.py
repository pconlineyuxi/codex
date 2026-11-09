"""Shared business applicability; configuration is the only policy authority.

An exemption is not a repaired amount. A deferred unfulfilled order is not a
cancelled order. Missing evidence must never create either decision.
"""
from datetime import datetime, timezone
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT/'config/diagnostic_rules.yaml').read_text())
POLICY = CONFIG['patrol_policy']
ZONE = ZoneInfo('America/New_York')
REASONS = {
    'verified_no_po_zero_cost':'已核实无适用 PO 且基础成本为零，按业务口径排除',
    'allowed_zero_cost':'已确认业务允许零成本',
    'verified_gift_receipt':'已核实本次零成本商品为无 PO 赠品入库来源，不视为金额修复',
    'confirmed_cancelled':'已取消且没有履约证据，不适用金额补齐检查',
    'stale_unfulfilled':'长期未履约待核对，仅状态变化再提醒',
    'cancellation_with_fulfillment':'取消标记与履约证据冲突，需核对实际履约状态',
}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def present(value):
    return value is not None and not pd.isna(value) and str(value).strip() != ''


def zero_cost_reason(rule, row):
    if rule not in {'missing_product_cost','unit_cost_non_positive'}:
        return None
    base, hardware, removed = [number(row.get(k)) for k in ('product_cost_unit','hardware_cost','removed_hardware')]
    if base != 0 or hardware is None or removed is None or hardware < 0 or removed < 0 or removed > base+hardware:
        return None
    if rule == 'unit_cost_non_positive' and base+hardware-removed != 0:
        return None
    evidence = row.get('cost_review')
    if isinstance(evidence, dict) and evidence.get('status') == 'reviewed':
        if evidence.get('gift_verified') is True:
            return 'verified_gift_receipt'
        if (POLICY['allow_verified_no_po_zero_cost'] and evidence.get('code') == 'verified_no_po_zero_cost'
                and evidence.get('po_line_count') == 0):
            return 'verified_no_po_zero_cost'
    if any(all(str(row.get(k,'')) == str(v) for k,v in item['match'].items()) for item in POLICY['zero_cost_exceptions']):
        return 'allowed_zero_cost'
    return None


def allowed_zero_cost(rule, row):
    return zero_cost_reason(rule, row) is not None


def decision(rule, row, now=None):
    """Return an explicit applicability state, or None to evaluate normally."""
    reason = zero_cost_reason(rule, row)
    if reason:
        return {'state':'exempt','reason':reason}
    tags = CONFIG['queryable_rules'].get(rule,{}).get('tags',[])
    if not set(tags) & {'lifecycle_sales','lifecycle_cost'}:
        return None
    scan, delivered = number(row.get('positive_scan_records')), number(row.get('qty_delivered'))
    # An unknown/non-empty platform status prevents claiming no fulfillment.
    platform_evidence = any(present(row.get(k)) and str(row[k]).strip().lower() not in {'cancel','canceled','cancelled'}
                            for k in ('amz_fulfullment_order_status','cwzy_ss_order_status'))
    fulfilled = (scan is not None and scan > 0) or (delivered is not None and delivered > 0) or platform_evidence
    cancelled = (str(row.get('order_state','')).lower() in {'cancel','cancelled','canceled'}
                 or str(row.get('walmart_status','')).lower() in {'cancelled','canceled'}
                 or str(row.get('is_amazon_canceled','')).lower() == 'true')
    if cancelled and fulfilled:
        return {'state':'unknown','reason':'cancellation_with_fulfillment'}
    complete = all(k in row for k in ('positive_scan_records','qty_delivered','amz_fulfullment_order_status','cwzy_ss_order_status'))
    unfulfilled = complete and scan == 0 and delivered == 0 and not platform_evidence
    if cancelled and unfulfilled:
        return {'state':'exempt','reason':'confirmed_cancelled'}
    if rule != 'zero_sales_with_units' or number(row.get('sales')) != 0 or not (number(row.get('units')) or 0) > 0:
        return None
    if not unfulfilled or not present(row.get('created_at')):
        return None
    created = pd.Timestamp(row['created_at'])
    created = created.tz_localize('UTC') if created.tzinfo is None else created.tz_convert('UTC')
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('检查时间必须带时区')
    age = (current.astimezone(ZONE).date()-created.tz_convert(ZONE).date()).days
    if age >= POLICY['stale_unfulfilled_days']:
        return {'state':'deferred','reason':'stale_unfulfilled'}
    return None


def order_names(rows):
    return sorted({str(r['order_number']) for r in rows if present(r.get('order_number'))})


def ensure_policy_current():
    current = yaml.safe_load((ROOT/'config/diagnostic_rules.yaml').read_text())
    if current.get('patrol_policy') != POLICY:
        raise RuntimeError('巡检业务策略已更新，请重启工具后再查询，避免旧例外与新规则混用。')
