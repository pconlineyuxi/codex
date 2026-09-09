from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from bi_check_agent.models import AnomalyQueryRequest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"

DIMENSION_LABELS = {
    "main_ir": "Main IR", "ir": "IR", "sku": "SKU", "marketplace": "Marketplace",
    "store": "Store", "day": "Day", "week": "Week", "month": "Month",
}
AGGREGATION_DIMENSION_COLUMNS = {
    "main_ir": "main_ir", "ir": "ir", "sku": "sku", "marketplace": "market_place",
    "store": "store", "day": "day", "week": "week", "month": "month",
}
ORDER_LINE_DETAIL_COLUMNS = [
    "date_order", "main_ir", "ir", "sku", "market_place", "store", "follower", "order_id",
    "gross_sales", "promo_cost", "sales", "units", "product_cost_unit", "hardware_cost",
    "removed_hardware", "unit_cost", "order_cost_total", "commission", "ad_spend", "shipping_fee",
]
ANOMALY_RULE_LABELS = {
    "zero_units_with_sales": "Units = 0 but Sales != 0",
    "zero_sales_with_units": "Sales = 0 but Units != 0",
    "negative_units": "Units < 0",
    "negative_sales": "Sales < 0",
    "negative_shipping_fee": "Shipping Fee < 0",
    "negative_promotion": "Promotion < 0 after normalization",
    "negative_commission": "Commission < 0",
    "negative_ad_spend": "Ad Spend < 0",
    "missing_product_cost": "Missing / non-positive product cost",
    "unit_cost_non_positive": "Unit cost <= 0",
    "removed_hardware_abnormal": "Removed hardware > product cost + hardware cost",
    "shipping_cost_ratio_high": "Shipping Fee / Sales high",
    "unit_shipping_fee_high": "Shipping Fee / Units high",
    "promotion_ratio_high": "Promotion / Sales high",
    "ad_spend_ratio_high": "Ad Spend / Sales high",
    "ad_spend_without_sales": "Ad Spend > 0 with Sales = 0",
    "profit_rate_with_ads_ship_high": "Profit rate with Ads & Ship > 1",
}


def load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _clean_list(values: list[Any]) -> list[Any]:
    return [v for v in values if v not in (None, "", [])]


def _bind_list(params: dict[str, Any], prefix: str, values: list[Any]) -> list[str]:
    keys = []
    for idx, value in enumerate(values):
        key = f"{prefix}_{idx}"
        params[key] = value
        keys.append(f":{key}")
    return keys


def build_where_clause(req: AnomalyQueryRequest) -> tuple[str, dict[str, Any], list[str], list[str]]:
    if any(req.unsupported_filters.values()):
        raise ValueError("包含不支持的筛选字段，请修正查询范围")
    mapping = load_yaml("filter_mapping.yaml")
    params: dict[str, Any] = {"start_date": f"{req.start_date.isoformat()} 00:00:00", "end_date": f"{req.end_date.isoformat()} 00:00:00"}
    rules_cfg = load_yaml("diagnostic_rules.yaml")
    max_days = int(os.getenv("MAX_QUERY_DAYS", str(rules_cfg.get("thresholds", {}).get("max_query_days", 366))))
    if (req.end_date - req.start_date).days > max_days:
        raise ValueError(f"查询时间跨度超过 {max_days} 天，请缩小 Time range")
    base_clauses = ["date_order >= TO_TIMESTAMP(:start_date, 'YYYY-MM-DD HH24:MI:SS')", "date_order < TO_TIMESTAMP(:end_date, 'YYYY-MM-DD HH24:MI:SS')"]
    order_clauses = ["type = 'order'"]
    ads_clauses = ["type = 'ad_daily'"]
    applied = ["date_order 左闭右开", "type = order + ad_daily", "order/ad_daily 使用相同业务 filter"]
    skipped: list[str] = []
    req_dict = req.model_dump()

    def build_filter_sql(filter_name: str, spec: dict[str, Any], values: list[Any]) -> str | None:
        field, operator = spec["sql_field"], spec["operator"]
        value_type = spec.get("type", "string_list")
        contains_null = any(str(v).upper() in {"<NULL>", "NULL"} for v in values)
        if contains_null and value_type != "string_list_with_null":
            raise ValueError(f"筛选 {filter_name} 不支持 NULL 标记，请明确取值")
        normal_values = [v for v in values if str(v).upper() not in {"<NULL>", "NULL"}]
        if operator not in {"in", "not_in"}:
            return None
        subclauses = []
        if normal_values:
            placeholders = _bind_list(params, filter_name, normal_values)
            sql_op = "IN" if operator == "in" else "NOT IN"
            subclauses.append(f"{field} {sql_op} ({', '.join(placeholders)})")
        if contains_null and value_type == "string_list_with_null":
            subclauses.append(f"{field} IS NULL" if operator == "in" else f"{field} IS NOT NULL")
        if not subclauses:
            return None
        return "(" + (" OR " if operator == "in" else " AND ").join(subclauses) + ")"

    for filter_name, spec in mapping.get("filters", {}).items():
        values = _clean_list(req_dict.get(filter_name, []))
        if not values:
            skipped.append(filter_name)
            continue
        filter_sql = build_filter_sql(filter_name, spec, values)
        if not filter_sql:
            skipped.append(filter_name)
            continue
        order_clauses.append(filter_sql)
        ads_clauses.append(filter_sql)
        applied.append(filter_name)

    for name, value in (req.unsupported_filters or {}).items():
        if value:
            skipped.append(f"{name}: unsupported field mapping")
    where_clause = "\n  AND ".join(base_clauses)
    where_clause += "\n  AND (\n    (" + "\n      AND ".join(order_clauses) + ")\n    OR\n    (" + "\n      AND ".join(ads_clauses) + ")\n  )"
    return where_clause, params, applied, skipped


def build_order_line_sql(req: AnomalyQueryRequest) -> tuple[str, dict[str, Any], list[str], list[str]]:
    where_clause, params, applied, skipped = build_where_clause(req)
    max_rows = int(os.getenv("MAX_RESULT_ROWS", str(load_yaml("diagnostic_rules.yaml").get("thresholds", {}).get("max_result_rows", 50000))))
    params["max_rows"] = max_rows + 1
    sql = f"""
SELECT type, date_order, main_ir, order_name AS order_id, ir, sku, follower,
    market_place, store,
    CASE WHEN type = 'order' THEN price_subtotal ELSE 0 END AS gross_sales,
    CASE WHEN type = 'order' THEN ABS(promotion) ELSE 0 END AS promo_cost,
    CASE WHEN type = 'order' THEN price_subtotal - ABS(promotion) ELSE 0 END AS sales,
    CASE WHEN type = 'order' THEN units ELSE 0 END AS units,
    CASE WHEN type = 'order' THEN product_cost_unit ELSE 0 END AS product_cost_unit,
    CASE WHEN type = 'order' THEN hardware_cost ELSE 0 END AS hardware_cost,
    CASE WHEN type = 'order' THEN removed_hardware ELSE 0 END AS removed_hardware,
    CASE WHEN type = 'order' THEN product_cost_unit + hardware_cost - removed_hardware ELSE 0 END AS unit_cost,
    CASE WHEN type = 'order' THEN (product_cost_unit + hardware_cost - removed_hardware) * units ELSE 0 END AS order_cost_total,
    CASE WHEN type = 'order' THEN commission ELSE 0 END AS commission,
    ad_spend,
    CASE WHEN type = 'order' THEN shipping_fee ELSE 0 END AS shipping_fee
FROM bi.ba_mv_profit_order_line_and_ad_info
WHERE {where_clause}
LIMIT :max_rows
"""
    return sql, params, applied, skipped


def _normalize_aggregation_dimensions(dimensions: list[str] | None) -> list[str]:
    cleaned = []
    for dim in dimensions or ["main_ir"]:
        normalized = str(dim).strip().lower().replace("market_place", "marketplace").replace("market place", "marketplace")
        if normalized not in AGGREGATION_DIMENSION_COLUMNS:
            raise ValueError(f"unsupported aggregation dimension: {dim}")
        if normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned or ["main_ir"]


def _prepare_date_dimensions(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    if "date_order" not in d.columns:
        return d
    dt = pd.to_datetime(d["date_order"], errors="coerce")
    try:
        if dt.dt.tz is not None:
            dt = dt.dt.tz_convert("America/New_York").dt.tz_localize(None)
    except Exception:
        pass
    d["day"] = dt.dt.date.astype("string")
    d["week"] = (dt - pd.to_timedelta(dt.dt.weekday, unit="D")).dt.date.astype("string")
    d["month"] = dt.dt.to_period("M").dt.to_timestamp().dt.date.astype("string")
    return d


def _ensure_numeric(d: pd.DataFrame) -> pd.DataFrame:
    for col in ["gross_sales", "promo_cost", "sales", "units", "product_cost_unit", "hardware_cost", "removed_hardware", "unit_cost", "order_cost_total", "commission", "ad_spend", "shipping_fee"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce").replace([float("inf"), float("-inf")], float("nan"))
    if "order_cost_total" not in d.columns:
        d["order_cost_total"] = (d.get("product_cost_unit", 0) + d.get("hardware_cost", 0) - d.get("removed_hardware", 0)) * d.get("units", 0)
    return d


def aggregate_product(df: pd.DataFrame, dimensions: list[str] | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    selected = _normalize_aggregation_dimensions(dimensions)
    group_cols = [AGGREGATION_DIMENSION_COLUMNS[d] for d in selected]
    d = _ensure_numeric(_prepare_date_dimensions(df))
    for col in group_cols:
        if col not in d.columns:
            d[col] = pd.NA
    g = d.groupby(group_cols, dropna=False).agg(
        Sales=("sales", lambda x: x.sum() if x.notna().all() else float("nan")), Units=("units", lambda x: x.sum() if x.notna().all() else float("nan")), gross_sales=("gross_sales", lambda x: x.sum() if x.notna().all() else float("nan")),
        **{"Promo cost": ("promo_cost", lambda x: x.sum() if x.notna().all() else float("nan"))}, order_cost_total=("order_cost_total", lambda x: x.sum() if x.notna().all() else float("nan")),
        commission_total=("commission", lambda x: x.sum() if x.notna().all() else float("nan")), **{"Ads cost": ("ad_spend", lambda x: x.sum() if x.notna().all() else float("nan"))},
        **{"Shipping Fee": ("shipping_fee", lambda x: x.sum() if x.notna().all() else float("nan"))},
    ).reset_index()
    g["Profit wo Ads"] = (g["Sales"] - g["order_cost_total"] - g["commission_total"]).round(2)
    g["Profit rate wo Ads"] = (g["Profit wo Ads"] / g["Sales"].replace({0: float("nan")})).round(4)
    g["Tacos"] = (g["Ads cost"] / g["Sales"].replace({0: float("nan")})).round(4)
    g["Profit with Ads"] = (g["Sales"] - g["order_cost_total"] - g["commission_total"] - g["Ads cost"]).round(2)
    g["Profit rate with Ads"] = (g["Profit with Ads"] / g["Sales"].replace({0: float("nan")})).round(4)
    g["Profit with Ads & Ship"] = (g["Profit with Ads"] - g["Shipping Fee"]).round(2)
    g["Profit rate with Ads & Ship"] = (g["Profit with Ads & Ship"] / g["Sales"].replace({0: float("nan")})).round(4)
    cols = group_cols + ["Sales", "Units", "Promo cost", "Shipping Fee", "Ads cost", "Tacos", "Profit wo Ads", "Profit with Ads", "Profit with Ads & Ship", "Profit rate with Ads & Ship"]
    return g[cols].sort_values(cols[-2] if cols[-2] in g.columns else group_cols[0], ascending=False)


def _business_guard_notes(row: pd.Series, rule: str, cfg: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    marketplace = str(row.get("market_place", "") or "")
    ir = str(row.get("ir", "") or "")
    sku = str(row.get("sku", "") or "")
    guards = cfg.get("rule_guards", {})
    if rule == "promotion_ratio_high":
        allowed = [x.lower() for x in guards.get("promotion_ratio_high", {}).get("skip_when_marketplace_not_in", [])]
        if marketplace.lower() not in allowed:
            notes.append("Promotion coverage mainly applies to Amazon; non-Amazon promotion anomaly is downgraded for review.")
    if rule in {"ad_spend_ratio_high", "ad_spend_without_sales"}:
        notes.append("Ad spend is SKU/date-level, not order-line-level; interpret at aggregate level.")
        if ir.lower() == "product_v3_3" or "v3_3" in sku.lower():
            notes.append("V3.3 ad spend can use product_v3_3 placeholder IR; do not treat this as IR mapping error.")
    return notes


def detect_anomalies(df: pd.DataFrame, selected_rules: list[str] | None = None) -> pd.DataFrame:
    cfg = load_yaml("diagnostic_rules.yaml")
    thresholds = cfg.get("thresholds", {})
    if df.empty or not selected_rules:
        return pd.DataFrame()
    rules = selected_rules
    d = _ensure_numeric(_prepare_date_dimensions(df.copy()))
    anomaly_rows = []
    order_rows = d[d.get("type", "order") == "order"].copy() if "type" in d.columns else d.copy()
    for _, row in order_rows.iterrows():
        hits, notes = [], []
        sales, units = row.get("sales", 0), row.get("units", 0)
        promo, shipping, commission = row.get("promo_cost", 0), row.get("shipping_fee", 0), row.get("commission", 0)
        product_cost, hardware, removed = row.get("product_cost_unit", 0), row.get("hardware_cost", 0), row.get("removed_hardware", 0)
        unit_cost = product_cost + hardware - removed
        checks = {
            "zero_units_with_sales": pd.notna(sales) and units == 0 and sales != 0,
            "zero_sales_with_units": pd.notna(units) and sales == 0 and units != 0,
            "negative_units": units < 0,
            "negative_sales": sales < 0,
            "negative_shipping_fee": shipping < 0,
            "negative_promotion": promo < 0,
            "negative_commission": commission < 0,
            "missing_product_cost": pd.isna(product_cost) or product_cost <= 0,
            "unit_cost_non_positive": unit_cost <= 0,
            "removed_hardware_abnormal": removed > (product_cost + hardware),
            "shipping_cost_ratio_high": sales > 0 and shipping / sales > thresholds.get("shipping_cost_ratio_high", 0.5),
            "unit_shipping_fee_high": units > 0 and shipping / units > thresholds.get("unit_shipping_fee_high", 80),
            "promotion_ratio_high": sales > 0 and promo / sales > thresholds.get("promotion_ratio_high", 0.5),
        }
        for rule in rules:
            if rule == "promotion_ratio_high" and str(row.get("market_place", "")).lower() not in {x.lower() for x in cfg.get("rule_guards", {}).get(rule, {}).get("skip_when_marketplace_not_in", [])}:
                continue
            if checks.get(rule, False):
                hits.append(rule)
                notes.extend(_business_guard_notes(row, rule, cfg))
        if hits:
            r = row.to_dict()
            r["anomaly_rules"] = ", ".join(hits)
            r["business_guard_notes"] = " | ".join(sorted(set(notes)))
            anomaly_rows.append(r)
    # Aggregate-only ad/profit rules by selected dimensions can still surface as aggregate samples.
    if any(r in rules for r in ["negative_ad_spend", "ad_spend_ratio_high", "ad_spend_without_sales", "profit_rate_with_ads_ship_high"]):
        agg = aggregate_product(d, ["marketplace", "store", "sku", "day"])
        for _, row in agg.iterrows():
            hits, notes = [], []
            if "negative_ad_spend" in rules and row.get("Ads cost", 0) < 0:
                hits.append("negative_ad_spend")
            if "ad_spend_ratio_high" in rules and row.get("Sales", 0) > 0 and row.get("Ads cost", 0) / row.get("Sales", 1) > thresholds.get("ad_spend_ratio_high", 0.1):
                hits.append("ad_spend_ratio_high")
            if "ad_spend_without_sales" in rules and row.get("Sales", 0) == 0 and row.get("Ads cost", 0) > 0:
                hits.append("ad_spend_without_sales")
            if "profit_rate_with_ads_ship_high" in rules and row.get("Profit rate with Ads & Ship", 0) > thresholds.get("profit_rate_with_ads_ship_high", 1.0):
                hits.append("profit_rate_with_ads_ship_high")
            if hits:
                r = row.to_dict()
                r["anomaly_rules"] = ", ".join(hits)
                r["business_guard_notes"] = "Ad-related rules are aggregate-level because ad spend is SKU/date-level."
                anomaly_rows.append(r)
    result = pd.DataFrame(anomaly_rows)
    if result.empty:
        return result
    preferred = ORDER_LINE_DETAIL_COLUMNS + ["day", "month", "Sales", "Units", "Ads cost", "Tacos", "Shipping Fee", "Profit with Ads & Ship", "Profit rate with Ads & Ship", "anomaly_rules", "business_guard_notes"]
    return result[[c for c in preferred if c in result.columns]]


def _fmt_list(values: list[Any]) -> str:
    return ", ".join(str(v) for v in values) if values else "未限制"


def generate_report(req: AnomalyQueryRequest, aggregate_df: pd.DataFrame, anomalies: pd.DataFrame, applied: list[str], skipped: list[str]) -> str:
    dimension_labels = [DIMENSION_LABELS.get(d, d) for d in req.aggregation_dimensions]
    rule_labels = [ANOMALY_RULE_LABELS.get(r, r) for r in req.anomaly_rules] if req.anomaly_rules else ["未选择异常规则：仅查询数据，不做异常判定"]
    lines = ["自然语言数据查询 / 异常检查报告", ""]
    lines.append(f"- Time range: {req.start_date} <= date_order < {req.end_date}（End Date 不包含）")
    lines.append(f"- Anomaly rules: {_fmt_list(rule_labels)}")
    lines.append(f"- Aggregation dimensions: {_fmt_list(dimension_labels)}")
    lines.append(f"- Marketplace: {_fmt_list(req.marketplace)}")
    lines.append(f"- Store: {_fmt_list(req.store)}")
    lines.append(f"- Main IR: {_fmt_list(req.main_ir)}")
    lines.append(f"- IR: {_fmt_list(req.ir)}")
    lines.append(f"- SKU: {_fmt_list(req.sku)}")
    lines.append(f"- 已应用 filter: {_fmt_list(applied)}")
    if skipped:
        lines.append(f"- 未应用 / 未限制 filter: {_fmt_list(skipped)}")
    lines.append("")
    if aggregate_df.empty:
        lines.append("当前 filter 下没有查询到可聚合数据。")
    else:
        lines.append(f"聚合结果行数：{len(aggregate_df)}。")
    if anomalies.empty:
        lines.append("未发现命中所选异常规则的数据。")
        lines.append("注意：未命中异常不代表数据绝对正确，只代表当前规则和 filter 下未发现可疑数据。")
    else:
        lines.append(f"命中异常样本数量：{len(anomalies)}。页面默认不展开订单行明细，仅展示异常摘要和可选样本。")
        counts = anomalies["anomaly_rules"].value_counts().head(8) if "anomaly_rules" in anomalies.columns else pd.Series(dtype=int)
        if not counts.empty:
            lines.append("主要异常分布：")
            for rule, count in counts.items():
                lines.append(f"- {rule}: {count}")
        if "business_guard_notes" in anomalies.columns and anomalies["business_guard_notes"].astype(str).str.len().sum() > 0:
            lines.append("")
            lines.append("业务口径提醒：部分命中项可能受 bundle 归因、平台 promotion 覆盖、广告 SKU/date 粒度或 V3.3 占位 IR 影响；请结合 guard notes 判断是否为真实异常。")
    return "\n".join(lines)
