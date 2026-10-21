from __future__ import annotations

from typing import Any
from bi_check_agent.rule_catalog import RULE_IDS, RULES

ALLOWED_AGGREGATION_DIMENSIONS = ["main_ir", "ir", "sku", "marketplace", "store", "day", "week", "month"]
ANOMALY_RULES = list(RULE_IDS)
FILTER_FIELDS = [
    "marketplace", "store", "sku", "main_ir", "ir", "order_id", "follower", "brand",
    "category", "category_exclude", "ir_exclude", "attribute_use_for", "attribute_screen_size",
    "attribute_touch", "attribute_processor_series",
]
DIMENSION_ALIASES = {
    "marketplace": ["marketplace", "market place", "market_place", "平台", "市场"],
    "store": ["store", "店铺", "店"],
    "month": ["month", "月份", "按月", "月度", "monthly"],
    "week": ["week", "周", "按周", "weekly"],
    "day": ["day", "date", "daily", "日期", "天", "按日", "每天"],
    "main_ir": ["main ir", "main_ir", "主ir", "主 ir", "主产品", "主产品维度"],
    "ir": ["ir"],
    "sku": ["sku"],
}
MARKETPLACE_ALIASES = {
    "walmart mx": "Walmart MX", "walmart mexico": "Walmart MX", "wm mx": "Walmart MX",
    "amazon us": "Amazon US", "amazon ca": "Amazon CA", "amazon": "Amazon",
    "newegg": "Newegg", "neweggbusiness": "Neweggbusiness", "ebay": "eBay", "bestbuy": "BestBuy",
    "kohls": "Kohl's", "kohl's": "Kohl's", "temu": "Temu", "target": "Target", "tiktok": "TikTok", "macys": "Macys",
}
RULE_ALIASES = {key: list(rule.aliases) for key, rule in RULES.items()}


def default_parser_result() -> dict[str, Any]:
    return {
        "parsed": False,
        "intent": "data_query",
        "query_mode": "data_query",
        "data_domain": "product_profit",
        "start_date": None,
        "end_date": None,
        "time_range": {"start_date": None, "end_date": None, "timezone": "America/New_York", "boundary": "left_closed_right_open", "source_text": None, "display_label": None},
        "anomaly_rules": [],
        "aggregation_dimensions": [],
        "aggregation_dimensions_source": "not_specified",
        "filters": {field: [] for field in FILTER_FIELDS},
        **{field: [] for field in FILTER_FIELDS},
        "output_level": "aggregate_with_anomaly_samples",
        "filter_policy": {"empty_array_means_all": True, "apply_same_business_filters_to_order_and_ad_daily": True},
        "missing_required_fields": [],
        "confidence": {},
        "warnings": [],
        "unresolved_terms": [],
        "notes": [],
        "source": "fallback",
        "ai_status": "not_used",
        "ai_error_code": None,
    }


def dedupe(values: list[Any]) -> list[str]:
    result, seen = [], set()
    for value in values or []:
        if value is None:
            continue
        cleaned = str(value).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            result.append(cleaned)
            seen.add(key)
    return result


def normalize_dimension(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("market_place", "marketplace").replace("market place", "marketplace")
    return text if text in ALLOWED_AGGREGATION_DIMENSIONS else None


def sanitize_parser_result(raw: dict[str, Any] | None) -> dict[str, Any]:
    result = default_parser_result()
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in result and key not in {"filters", "time_range", "filter_policy"}:
                result[key] = value
        if isinstance(raw.get("time_range"), dict):
            result["time_range"].update(raw["time_range"])
        if isinstance(raw.get("filter_policy"), dict):
            result["filter_policy"].update(raw["filter_policy"])
        if isinstance(raw.get("filters"), dict):
            for field in FILTER_FIELDS:
                result["filters"][field] = dedupe(raw["filters"].get(field, []))
        for field in FILTER_FIELDS:
            direct = raw.get(field, []) if isinstance(raw.get(field, []), list) else []
            merged = dedupe(result["filters"].get(field, []) + direct)
            result["filters"][field] = merged
            result[field] = merged

    dims = []
    for value in result.get("aggregation_dimensions") or []:
        dim = normalize_dimension(value)
        if dim and dim not in dims:
            dims.append(dim)
        elif value:
            result["warnings"].append(f"不支持的聚合维度已忽略：{value}")
            result["unresolved_terms"].append(str(value))
    result["aggregation_dimensions"] = dims

    rules = []
    for value in result.get("anomaly_rules") or []:
        rule = str(value).strip().lower()
        if rule in ANOMALY_RULES and rule not in rules:
            rules.append(rule)
        elif value:
            result["warnings"].append(f"不支持的异常规则已忽略：{value}")
            result["unresolved_terms"].append(str(value))
    result["anomaly_rules"] = rules

    for key in ["start_date", "end_date"]:
        if result["time_range"].get(key) and not result.get(key):
            result[key] = result["time_range"][key]
        if result.get(key):
            result["time_range"][key] = result[key]
    for field in FILTER_FIELDS:
        result[field] = dedupe(result.get(field, []))
        result["filters"][field] = result[field]
    if result.get("start_date") and result.get("end_date"):
        result["parsed"] = True
    for key in ["warnings", "notes", "unresolved_terms", "missing_required_fields"]:
        result[key] = dedupe(result.get(key, []))
    return result
