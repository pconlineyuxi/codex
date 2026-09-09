from __future__ import annotations

import json
import os
import re
from typing import Any

from bi_check_agent.parser_rules import fallback_parse, parse_time_range
from bi_check_agent.parser_schema import (
    ALLOWED_AGGREGATION_DIMENSIONS,
    ANOMALY_RULES,
    FILTER_FIELDS,
    default_parser_result,
    sanitize_parser_result,
)


def _clean_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
    return {}


def _merge_rule_overrides(description: str, parsed: dict[str, Any]) -> dict[str, Any]:
    rule_result = fallback_parse(description)
    parsed_range = parse_time_range(description)
    if parsed_range:
        start, end, source_text, display_label, note = parsed_range
        parsed["start_date"] = start.isoformat()
        parsed["end_date"] = end.isoformat()
        parsed.setdefault("time_range", {}).update({"start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "America/New_York", "boundary": "left_closed_right_open", "source_text": source_text, "display_label": display_label})
        parsed.setdefault("notes", []).append(note)
    if rule_result.get("aggregation_dimensions"):
        parsed["aggregation_dimensions"] = rule_result["aggregation_dimensions"]
        parsed["aggregation_dimensions_source"] = "user_specified"
    if rule_result.get("anomaly_rules"):
        existing = list(parsed.get("anomaly_rules") or [])
        parsed["anomaly_rules"] = existing + [r for r in rule_result["anomaly_rules"] if r not in existing]
    for field in FILTER_FIELDS:
        values = list(parsed.get(field) or [])
        rule_values = list(rule_result.get(field) or [])
        merged = values + [v for v in rule_values if str(v).lower() not in {str(x).lower() for x in values}]
        parsed[field] = merged
        parsed.setdefault("filters", {})[field] = merged
    for key in ["warnings", "notes", "unresolved_terms", "missing_required_fields"]:
        parsed.setdefault(key, [])
        for item in rule_result.get(key, []):
            if item not in parsed[key]:
                parsed[key].append(item)
    if parsed.get("start_date") and parsed.get("end_date"):
        parsed["missing_required_fields"] = [f for f in parsed.get("missing_required_fields", []) if f not in {"start_date", "end_date"}]
    # Deterministic parser owns query mode and required anomaly behavior.
    if rule_result.get("query_mode"):
        parsed["query_mode"] = rule_result["query_mode"]
        parsed["intent"] = rule_result["query_mode"]
    if parsed.get("anomaly_rules") or parsed.get("query_mode") == "data_query":
        parsed["missing_required_fields"] = [f for f in parsed.get("missing_required_fields", []) if f != "anomaly_rules"]
    return parsed


def ai_parse(description: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        return fallback_parse(description)

    system_prompt = f"""
You are a strict parser for a controlled product-profit data anomaly query tool.
Convert the user's natural-language request into JSON only.
Do not write SQL. Do not calculate metrics. Do not invent filters or anomaly rules.

Hard rules:
- intent/query_mode is data_query for normal data lookup and data_anomaly_query only when the user explicitly asks for anomalies/problems or names an anomaly rule; data_domain is product_profit.
- Timezone is America/New_York.
- All date ranges are left-closed and right-open: date_order >= start_date AND date_order < end_date.
- Full natural month ranges must use first day of next month as end_date.
- Empty filter arrays mean no restriction / all values.
- Business filters apply to both type='order' and type='ad_daily'.
- Only choose anomaly_rules from: {ANOMALY_RULES}
- Only choose aggregation_dimensions from: {ALLOWED_AGGREGATION_DIMENSIONS}
- Only choose filter fields from: {FILTER_FIELDS}
- If the user asks for normal data without anomaly language, keep anomaly_rules empty and do not add missing_required_fields for anomaly_rules.
- If the user says only "异常" without a specific anomaly type, keep anomaly_rules empty and add missing_required_fields=["anomaly_rules"].

Return this JSON shape:
{{
  "parsed": true,
  "intent": "data_query|data_anomaly_query",
  "query_mode": "data_query|data_anomaly_query",
  "data_domain": "product_profit",
  "start_date": "YYYY-MM-DD"|null,
  "end_date": "YYYY-MM-DD"|null,
  "time_range": {{"start_date": "YYYY-MM-DD"|null, "end_date": "YYYY-MM-DD"|null, "timezone": "America/New_York", "boundary": "left_closed_right_open", "source_text": string|null, "display_label": string|null}},
  "anomaly_rules": [],
  "aggregation_dimensions": [],
  "aggregation_dimensions_source": "user_specified|default_main_ir_when_empty|not_specified",
  "filters": {{"marketplace": [], "store": [], "sku": [], "main_ir": [], "ir": [], "order_id": [], "follower": [], "brand": [], "category": [], "category_exclude": [], "ir_exclude": [], "attribute_use_for": [], "attribute_screen_size": [], "attribute_touch": [], "attribute_processor_series": []}},
  "output_level": "aggregate_with_anomaly_samples",
  "filter_policy": {{"empty_array_means_all": true, "apply_same_business_filters_to_order_and_ad_daily": true}},
  "missing_required_fields": [],
  "confidence": {{}},
  "warnings": [],
  "unresolved_terms": [],
  "notes": []
}}
""".strip()
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=20, max_retries=1)
        response = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": description}],
            temperature=0,
        )
        raw = _clean_json_dict(response.choices[0].message.content or "{}")
        merged = default_parser_result()
        merged.update(raw)
        merged["source"] = "openai_with_rule_validation_v20"
        return sanitize_parser_result(_merge_rule_overrides(description, merged))
    except Exception as exc:
        fallback = fallback_parse(description)
        fallback["source"] = "fallback_after_ai_error_v20"
        fallback.setdefault("warnings", []).append("AI 解析未完成，已使用本地规则解析；请核对草稿。")
        return sanitize_parser_result(fallback)


def parse_business_description(description: str) -> dict[str, Any]:
    if not description.strip():
        result = default_parser_result()
        result["missing_required_fields"] = ["description"]
        result["notes"].append("请输入自然语言查询描述。")
        return sanitize_parser_result(result)
    result=ai_parse(description)
    for field in ['has_product_cost','has_sale_price']:
        m=re.search(rf'{field}\s*(?:=|为|是)\s*(true|false|是|否)',description,re.I)
        if m: result[field]=[m.group(1).lower() in {'true','是'}]
    return result
