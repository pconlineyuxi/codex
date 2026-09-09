from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bi_check_agent.parser_schema import (
    DIMENSION_ALIASES,
    MARKETPLACE_ALIASES,
    RULE_ALIASES,
    dedupe,
    default_parser_result,
    sanitize_parser_result,
)

CHINESE_MONTH_TO_NUMBER = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
MONTH_NAME_TO_NUMBER = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}


def today_est() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def month_range(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _safe_date(year: int, month: int, day: int) -> date:
    return date(year, month, day)


def _parse_numeric_date_range(text: str, today: date):
    pattern = r"(?:(20\d{2})[-/年])?\s*(1[0-2]|0?[1-9])[-/月]\s*(3[01]|[12]\d|0?[1-9])\s*(?:日)?\s*(?:到|至|~|－|-|—|through|to)\s*(?:(20\d{2})[-/年])?\s*(1[0-2]|0?[1-9])[-/月]\s*(3[01]|[12]\d|0?[1-9])"
    m = re.search(pattern, text, flags=re.I)
    if not m:
        return None
    y1, m1, d1, y2, m2, d2 = m.groups()
    start = _safe_date(int(y1 or today.year), int(m1), int(d1))
    inclusive_end = _safe_date(int(y2 or y1 or today.year), int(m2), int(d2))
    end = inclusive_end + timedelta(days=1)
    return start, end, m.group(0), f"{start.isoformat()} to {inclusive_end.isoformat()}", f"已将用户输入的日期范围 {m.group(0)} 按右开口径转换为 {start.isoformat()} <= date_order < {end.isoformat()}。"


def parse_time_range(description: str, today: date | None = None):
    today = today or today_est()
    text = description.strip().lower()
    numeric = _parse_numeric_date_range(text, today)
    if numeric:
        return numeric
    # Match whole-year requests only; leave month/day-qualified requests to the
    # more specific rules or the date-anchored AI parser.
    year_phrase = re.search(r"今年截至(?:目前|今天)|今年至今|year to date|\bytd\b|今年|去年|this year|last year", text)
    if year_phrase and not re.search(r"月|季度|半年|昨天|今天|昨日|今日|(?:\d+)\s*(?:天|日)|today|yesterday", text.replace(year_phrase.group(0), "")):
        phrase = year_phrase.group(0)
        year = today.year - (1 if phrase in {"去年", "last year"} else 0)
        start = date(year, 1, 1)
        end = today + timedelta(days=1) if phrase in {"今年截至目前", "今年截至今天", "今年至今", "year to date", "ytd"} else date(year + 1, 1, 1)
        return start, end, phrase, f"{year}年", "已按纽约当前日期解析年份，结束日期不包含；实际数据完整性需另行核对。"
    for phrase, offset in [('前天',2),('昨天',1),('昨日',1),('yesterday',1),('今天',0),('今日',0),('today',0)]:
        if phrase in text:
            start=today-timedelta(days=offset)
            end=start+timedelta(days=1)
            return start,end,phrase,str(start),'已按纽约时区自然日解析，结束日期不包含。'
    single=re.search(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)",text)
    if single:
        start=date(*map(int,single.groups()))
        return start,start+timedelta(days=1),single.group(0),str(start),'已按纽约时区单日解析。'
    m = re.search(r"(?:过去|最近|近|last|past)\s*(\d{1,3})\s*(?:天|日|days?)", text, flags=re.I)
    if m:
        days = max(1, min(int(m.group(1)), 180))
        start = today - timedelta(days=days - 1)
        end = today + timedelta(days=1)
        return start, end, m.group(0), f"最近{days}个自然日（含今天）", f"已按美东日期解析最近 {days} 个自然日；End Date 为明天 00:00，右侧不包含。"
    if any(x in text for x in ["过去一周", "最近一周", "近一周", "last 7 days", "past 7 days"]):
        start = today - timedelta(days=6)
        end = today + timedelta(days=1)
        return start, end, "过去一周/最近一周", "最近7个自然日（含今天）", "已按美东日期解析最近 7 个自然日；End Date 为明天 00:00，右侧不包含。"
    if any(x in text for x in ["上个月", "last month"]):
        first_this = date(today.year, today.month, 1)
        last_month_end = first_this
        start = date(first_this.year - 1, 12, 1) if first_this.month == 1 else date(first_this.year, first_this.month - 1, 1)
        return start, last_month_end, "上个月/last month", start.strftime("%Y-%m"), "已按美东自然月解析上个月，End Date 为不包含边界。"
    if any(x in text for x in ["本月", "this month"]):
        start = date(today.year, today.month, 1)
        end = month_range(today.year, today.month)[1]
        return start, end, "本月/this month", start.strftime("%Y-%m"), "已按美东自然月解析本月，End Date 为不包含边界。"
    if any(x in text for x in ["上周", "last week"]):
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        return start, this_monday, "上周/last week", f"week of {start.isoformat()}", "已按美东周一到周一解析上周。"
    m = re.search(r"(20\d{2})\s*年\s*(1[0-2]|0?[1-9]|十一|十二|十|[一二三四五六七八九])\s*月", description)
    if m:
        month = CHINESE_MONTH_TO_NUMBER.get(m.group(2), int(m.group(2)) if m.group(2).isdigit() else None)
        start, end = month_range(int(m.group(1)), month)
        return start, end, m.group(0), f"{start:%Y-%m}", "已按完整自然月解析，End Date 为下月 1 日。"
    m = re.search(r"今年\s*(1[0-2]|0?[1-9]|十一|十二|十|[一二三四五六七八九])\s*月", description)
    if m:
        month = CHINESE_MONTH_TO_NUMBER.get(m.group(1), int(m.group(1)) if m.group(1).isdigit() else None)
        start, end = month_range(today.year, month)
        return start, end, m.group(0), f"{start:%Y-%m}", "已按今年的完整自然月解析，End Date 为下月 1 日。"
    m = re.search(r"\b(20\d{2})[-/](1[0-2]|0?[1-9])\b", text)
    if m:
        start, end = month_range(int(m.group(1)), int(m.group(2)))
        return start, end, m.group(0), f"{start:%Y-%m}", "已按完整自然月解析，End Date 为下月 1 日。"
    for name, month in MONTH_NAME_TO_NUMBER.items():
        if re.search(rf"\b{name}\b", text):
            start, end = month_range(today.year, month)
            return start, end, name, f"{start:%Y-%m}", "已按当前年份的英文月份解析，End Date 为下月 1 日。"
    return None


def extract_aggregation_dimensions(text: str):
    low = text.lower()
    dims: list[str] = []
    explicit = any(x in low for x in ["按", "group by", "聚合", "汇总", "维度", "+"])
    if explicit:
        for dim, aliases in DIMENSION_ALIASES.items():
            if any(alias.lower() in low for alias in aliases):
                dims.append(dim)
    return dedupe(dims), [], []


def extract_anomaly_rules(text: str):
    low = text.lower()
    rules = []
    for rule, aliases in RULE_ALIASES.items():
        if any(alias.lower() in low for alias in aliases):
            rules.append(rule)
    compact = re.sub(r"\s+", "", low)
    if re.search(r"units?(?:=|为|是)?0.*(?:sales|销售|销售额|price_subtotal).*?(?:不为|不是|!=|<>|大于|>|非)0", compact, flags=re.I) or re.search(r"(?:销量|数量)(?:=|为|是)?0.*(?:销售|销售额).*?(?:不为|不是|!=|<>|大于|>|非)0", compact, flags=re.I):
        rules.append("zero_units_with_sales")
    if re.search(r"(?:sales|销售|销售额|price_subtotal)(?:=|为|是)?0.*units?.*?(?:不为|不是|!=|<>|大于|>|非)0", compact, flags=re.I) or re.search(r"(?:销售|销售额)(?:=|为|是)?0.*(?:销量|数量).*?(?:不为|不是|!=|<>|大于|>|非)0", compact, flags=re.I):
        rules.append("zero_sales_with_units")
    if "异常" in text and not rules:
        # Conservative default: do not run every rule silently; ask for confirmation.
        return [], ["识别到“异常”，但未能确定具体异常规则。请选择成本、销量、销售额、运费、促销、广告或利润率异常。"]
    return dedupe(rules), []


def infer_query_mode(text: str, rules: list[str]) -> str:
    anomaly_words = ["异常", "不对", "错误", "问题", "缺失", "为0", "大于", "小于", "超过", "negative", "missing", "ratio", "anomaly", "error"]
    if rules or any(w.lower() in text.lower() for w in anomaly_words):
        return "data_anomaly_query"
    return "data_query"


def extract_filters(text: str, result: dict) -> None:
    low = text.lower()
    for alias, canonical in MARKETPLACE_ALIASES.items():
        if alias in low:
            result["filters"]["marketplace"].append(canonical)
    patterns = {
        "main_ir": r"(?:main\s*ir|主\s*ir|主产品)\s*(?:是|为|=|:)\s*([A-Za-z0-9_#\-.]+)",
        "ir": r"(?<!main\s)(?:\bir\b|\bIR\b)\s*(?:是|为|=|:)\s*([A-Za-z0-9_#\-.]+)",
        "sku": r"\bsku\b\s*(?:是|为|=|:)\s*([A-Za-z0-9_#\-.]+)",
        "store": r"\bstore\b\s*(?:是|为|=|:)\s*([A-Za-z0-9_#\-.]+)",
        "order_id": r"(?:order\s*id|订单号)\s*(?:是|为|=|:)\s*([A-Za-z0-9_#\-.]+)",
    }
    for field, pattern in patterns.items():
        for m in re.finditer(pattern, text, flags=re.I):
            result["filters"][field].append(m.group(1))
    # Common compact forms: "IR ABC", "ABC 这个IR", "SKU ABC", "ABC 这个SKU" without equals.
    for field, token in [("sku", "sku"), ("ir", "ir")]:
        m = re.search(rf"\b{token}\b\s+([A-Za-z0-9_#\-.]{{4,}})", text, flags=re.I)
        if m:
            result["filters"][field].append(m.group(1))
        m = re.search(rf"([A-Za-z0-9_#\-.]{{4,}})\s*(?:这个|该|的)?\s*{token}\b", text, flags=re.I)
        if m:
            result["filters"][field].append(m.group(1))
    # If the user writes a lone product-looking code followed by 这个IR in Chinese with no boundary.
    m = re.search(r"([A-Za-z0-9][A-Za-z0-9_#\-.]{5,})\s*这个\s*IR", text, flags=re.I)
    if m:
        result["filters"]["ir"].append(m.group(1))


def fallback_parse(description: str) -> dict:
    result = default_parser_result()
    text = description or ""
    parsed_range = parse_time_range(text)
    if parsed_range:
        start, end, source, label, note = parsed_range
        result["start_date"] = start.isoformat()
        result["end_date"] = end.isoformat()
        result["time_range"].update({"start_date": start.isoformat(), "end_date": end.isoformat(), "source_text": source, "display_label": label})
        result["notes"].append(note)
        result["confidence"]["time_range"] = 0.98
    dims, dim_warnings, unresolved = extract_aggregation_dimensions(text)
    result["warnings"].extend(dim_warnings)
    result["unresolved_terms"].extend(unresolved)
    rules, warnings = extract_anomaly_rules(text)
    result["anomaly_rules"] = rules
    result["warnings"].extend(warnings)
    result["query_mode"] = infer_query_mode(text, rules)
    result["intent"] = result["query_mode"]
    extract_filters(text, result)
    for field, values in result["filters"].items():
        result[field] = dedupe(values)
        result["filters"][field] = result[field]
    if dims:
        result["aggregation_dimensions"] = dims
        result["aggregation_dimensions_source"] = "user_specified"
    elif result.get("sku"):
        result["aggregation_dimensions"] = ["sku"]
        result["aggregation_dimensions_source"] = "derived_from_sku_filter"
    elif result.get("ir"):
        result["aggregation_dimensions"] = ["ir"]
        result["aggregation_dimensions_source"] = "derived_from_ir_filter"
    elif result.get("main_ir"):
        result["aggregation_dimensions"] = ["main_ir"]
        result["aggregation_dimensions_source"] = "derived_from_main_ir_filter"
    else:
        result["aggregation_dimensions_source"] = "default_main_ir_when_empty"
    for required in ["start_date", "end_date"]:
        if not result.get(required):
            result["missing_required_fields"].append(required)
    if result["query_mode"] == "data_anomaly_query" and "异常" in text and not result["anomaly_rules"]:
        result["missing_required_fields"].append("anomaly_rules")
    if not any(result.get(k) for k in ["sku", "main_ir", "ir", "marketplace", "store", "order_id"]):
        result["notes"].append("没有识别到明确的 SKU / IR / Main IR / Marketplace / Store / Order ID；如需缩小范围，请补充定位 filter。")
    return sanitize_parser_result(result)
