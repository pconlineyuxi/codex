"""Validated internal results; callers retain JSON-friendly dictionary access.

DataFrames stay in memory and are never part of the public snapshot export.
All query and snapshot producers validate here, rather than inventing shapes.
"""
from datetime import date
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from bi_check_agent.models import AnomalyQueryRequest

Mode = Literal['demo', 'live', 'live_test']


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', arbitrary_types_allowed=True, hide_input_in_errors=True)

    def as_dict(self):
        # Python mode preserves the original DataFrames without serializing rows.
        return self.model_dump(by_alias=True)


class QueryEvidence(Contract):
    mode: Mode
    complete: StrictBool
    query_complete: StrictBool
    start: str
    end: str
    row_count: int = Field(ge=0)
    source: str
    snapshot: dict[str, Any] | None
    checked_at: str
    currency: str
    freshness_verified: StrictBool
    testing_note: str | None
    rule_version: str
    policy_version: str = ''
    applied_filters: list[str]
    unrestricted_filters: list[str]
    query: dict[str, Any]
    unknown_rule_inputs: list[dict[str, Any]]
    localization_limit: str
    diagnostics: dict[str, Any] | None = None

    @model_validator(mode='after')
    def consistent_scope(self):
        request = AnomalyQueryRequest.model_validate(self.query)
        if (str(request.start_date), str(request.end_date)) != (self.start, self.end):
            raise ValueError('Query evidence dates do not match the request')
        if not self.query_complete or (self.mode == 'live_test' and self.complete):
            raise ValueError('Incomplete/test query cannot be certified as a complete scan')
        if self.mode == 'live' and (not self.complete or not self.freshness_verified or not self.snapshot):
            raise ValueError('Live scan requires verified refresh evidence')
        return self


class QueryResult(Contract):
    rows: pd.DataFrame
    aggregate: pd.DataFrame
    anomalies: pd.DataFrame
    evidence: QueryEvidence

    @model_validator(mode='after')
    def source_count(self):
        if len(self.rows) != self.evidence.row_count:
            raise ValueError('Evidence count does not match filtered source records')
        return self


class AnalysisSnapshot(Contract):
    id: str
    kind: Literal['scan', 'comparison']
    mode: Mode
    currency: str
    scope_key: str
    current_request: dict[str, Any]
    current_evidence: QueryEvidence
    current_days: int = Field(gt=0)
    current_aggregate: list[dict[str, Any]] = Field(default_factory=list)
    previous_request: dict[str, Any] | None = None
    previous_evidence: QueryEvidence | None = None
    previous_days: int = Field(default=0, ge=0)
    previous_aggregate: list[dict[str, Any]] = Field(default_factory=list)
    report: dict[str, Any] | None = None
    rule_findings: list[dict[str, Any]] = Field(default_factory=list)
    source_frames: dict[str, pd.DataFrame] = Field(alias='_source_frames')

    @model_validator(mode='after')
    def matching_periods(self):
        periods = ('current', 'previous') if self.kind == 'comparison' else ('current',)
        if set(self.source_frames) != set(periods):
            raise ValueError('Analysis periods do not match source frames')
        if self.kind == 'comparison' and self.report is None:
            raise ValueError('Comparison requires a calculated report')
        if self.kind == 'scan' and (self.previous_request or self.previous_evidence or self.previous_days or self.report):
            raise ValueError('Single-period scan cannot contain a comparison')
        for period in periods:
            evidence = getattr(self, period + '_evidence')
            request = getattr(self, period + '_request')
            if evidence is None or request is None:
                raise ValueError('Missing period request or evidence')
            if evidence.query != request or evidence.mode != self.mode or evidence.currency != self.currency:
                raise ValueError('Snapshot request, mode or currency does not match evidence')
            days = (date.fromisoformat(evidence.end) - date.fromisoformat(evidence.start)).days
            if getattr(self, period + '_days') != days or len(self.source_frames[period]) != evidence.row_count:
                raise ValueError('Snapshot duration or source count does not match evidence')
        return self

    def public_dict(self):
        return self.model_dump(by_alias=True, exclude={'source_frames'})


def analysis_status(snapshot, mode, *, record_id=None, scope_matches=True, failed=False):
    """One readiness rule for query, manual-scan and comparison chat widgets."""
    if not snapshot or snapshot.get('mode') != mode or (record_id is not None and snapshot.get('id') != record_id):
        return 'missing'
    frames = snapshot.get('_source_frames')
    if not isinstance(frames, dict) or set(frames) not in ({'current'}, {'current', 'previous'}):
        return 'missing'
    if not all(isinstance(frame, pd.DataFrame) for frame in frames.values()):
        return 'missing'
    return 'stale' if failed or not scope_matches else 'ready'
