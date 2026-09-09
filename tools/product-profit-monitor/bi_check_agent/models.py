from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


ALLOWED_AGGREGATION_DIMENSIONS = {"main_ir", "ir", "sku", "marketplace", "store", "day", "week", "month"}
ALLOWED_ANOMALY_RULES = {
    "zero_units_with_sales",
    "zero_sales_with_units",
    "negative_units",
    "negative_sales",
    "negative_shipping_fee",
    "negative_promotion",
    "negative_commission",
    "negative_ad_spend",
    "missing_product_cost",
    "unit_cost_non_positive",
    "removed_hardware_abnormal",
    "shipping_cost_ratio_high",
    "unit_shipping_fee_high",
    "promotion_ratio_high",
    "ad_spend_ratio_high",
    "ad_spend_without_sales",
    "profit_rate_with_ads_ship_high",
}


class AnomalyQueryRequest(BaseModel):
    data_domain: str = "product_profit"
    start_date: date
    end_date: date
    anomaly_rules: list[str] = Field(default_factory=list)
    aggregation_dimensions: list[str] = Field(default_factory=lambda: ["main_ir"])
    output_level: str = "aggregate_with_anomaly_samples"

    has_product_cost: list[bool] = Field(default_factory=list)
    has_sale_price: list[bool] = Field(default_factory=list)
    category_exclude: list[str] = Field(default_factory=list)
    ir_exclude: list[str] = Field(default_factory=list)
    store: list[str] = Field(default_factory=list)
    marketplace: list[str] = Field(default_factory=list)
    main_ir: list[str] = Field(default_factory=list)
    category: list[str] = Field(default_factory=list)
    follower: list[str] = Field(default_factory=list)
    brand: list[str] = Field(default_factory=list)
    attribute_use_for: list[str] = Field(default_factory=list)
    attribute_screen_size: list[str] = Field(default_factory=list)
    attribute_touch: list[str] = Field(default_factory=list)
    attribute_processor_series: list[str] = Field(default_factory=list)
    sku: list[str] = Field(default_factory=list)
    ir: list[str] = Field(default_factory=list)
    order_id: list[str] = Field(default_factory=list)

    unsupported_filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("aggregation_dimensions")
    @classmethod
    def validate_aggregation_dimensions(cls, values: list[str]) -> list[str]:
        cleaned = []
        if not values:
            raise ValueError("汇总维度不能为空，请至少选择一个汇总维度")
        for value in values:
            normalized = str(value).strip().lower().replace("market_place", "marketplace").replace("market place", "marketplace")
            if normalized not in ALLOWED_AGGREGATION_DIMENSIONS:
                raise ValueError(f"unsupported aggregation dimension: {value}")
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned or ["main_ir"]

    @field_validator("anomaly_rules")
    @classmethod
    def validate_anomaly_rules(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values or []:
            normalized = str(value).strip().lower()
            if normalized not in ALLOWED_ANOMALY_RULES:
                raise ValueError(f"unsupported anomaly rule: {value}")
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned

    @model_validator(mode="after")
    def validate_dates(self) -> "AnomalyQueryRequest":
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be later than start_date")
        return self


# Backward-compatible alias for older imports/tests.
CheckRequest = AnomalyQueryRequest
