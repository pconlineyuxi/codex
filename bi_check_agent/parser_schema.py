from __future__ import annotations

from typing import Any

ALLOWED_AGGREGATION_DIMENSIONS = ["main_ir", "ir", "sku", "marketplace", "store", "day", "week", "month"]
ANOMALY_RULES = [
    "zero_units_with_sales", "zero_sales_with_units", "negative_units", "negative_sales",
    "negative_shipping_fee", "negative_promotion", "negative_commission", "negative_ad_spend",
    "missing_product_cost", "unit_cost_non_positive", "removed_hardware_abnormal",
    "shipping_cost_ratio_high", "unit_shipping_fee_high", "promotion_ratio_high",
    "ad_spend_ratio_high", "ad_spend_without_sales", "profit_rate_with_ads_ship_high",
]
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
RULE_ALIASES = {
    "zero_units_with_sales": ["units = 0", "unit = 0", "销量为0", "销量是0", "units为0但sales不为0", "有销售没数量"],
    "zero_sales_with_units": ["sales = 0", "销售额为0", "销售为0但units不为0", "有数量没销售", "price_subtotal = 0"],
    "negative_units": ["units < 0", "negative units", "销量为负", "负销量"],
    "negative_sales": ["sales < 0", "negative sales", "销售额为负", "负销售"],
    "negative_shipping_fee": ["shipping fee < 0", "negative shipping", "运费为负", "负运费"],
    "negative_promotion": ["promotion < 0", "promo < 0", "促销为负", "负促销"],
    "negative_commission": ["commission < 0", "佣金为负", "负佣金"],
    "negative_ad_spend": ["ad_spend < 0", "ads < 0", "广告为负", "负广告"],
    "missing_product_cost": ["missing product cost", "成本缺失", "没有成本", "product cost missing", "product_cost_unit为空"],
    "unit_cost_non_positive": ["unit cost <= 0", "单件成本小于等于0", "成本小于等于0", "unit cost异常"],
    "removed_hardware_abnormal": ["removed hardware", "移除硬件异常", "拆除硬件异常"],
    "shipping_cost_ratio_high": ["shipping ratio", "shipping fee / sales", "运费占比", "运费比例", "shipping超过", "运费超过"],
    "unit_shipping_fee_high": ["unit shipping", "shipping fee / units", "单件运费", "每件运费"],
    "promotion_ratio_high": ["promotion ratio", "promo ratio", "促销占比", "促销比例"],
    "ad_spend_ratio_high": ["ad spend ratio", "ad_spend / sales", "tacos", "广告占比", "广告比例", "广告花费异常"],
    "ad_spend_without_sales": ["ad spend without sales", "有广告没销售", "销售为0且广告大于0"],
    "profit_rate_with_ads_ship_high": ["profit rate > 1", "利润率大于1", "profit rate with ads ship", "利润率异常"],
}


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
