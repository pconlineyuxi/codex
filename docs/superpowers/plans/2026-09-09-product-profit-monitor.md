# Product Profit Monitor Implementation Plan

> **For agentic workers:** Use subagent-driven-development for bounded implementation and review, with local integration. Steps track completion below.

**Goal:** A locally runnable Product Profit query/diagnostic interface and durable scheduled monitor, pushed to pconlineyuxi/codex, ready for real-data configuration before server deployment.

**Architecture:** Preserve the existing Streamlit/Pydantic parser, build one shared query/check service, and add SQLite run/incident/outbox persistence plus a standalone worker. Explicit demo mode is isolated from live mode; no live credentials or notification targets are bundled.

**Tech Stack:** Python 3.12, Streamlit, pandas, SQLAlchemy/PostgreSQL, SQLite, pytest, requests.

## Task 1: Source queries and evidence
Files: bi_check_agent/core.py, db.py, service.py; tests/test_service.py.
- [x] Write tests for no implicit mock, null preservation, safe query limit, partition failure, multi-store isolation, and exact profit decomposition.
- [x] Run `.venv/bin/python -m pytest tests/test_service.py -q` and verify failures before fixes.
- [x] Keep original parser/model interfaces, add `query(req, mode)` returning source rows and evidence; fetch raw source rows with parameterized filters and bounded max+1, fail on incomplete partitions rather than silently truncate. Production requires a configured read-only connection and acknowledged currency/timezone contract.
- [x] Use explicit demo fixtures filtered by request date/object, never production notification.
- [x] Preserve original missing values; rule guards actually skip inapplicable cases. Aggregate ads by marketplace/store/SKU/day.
- [x] Verify contribution sum equals profit difference using gross sales, negative promo/cost/commission/ads/shipping deltas; zero division stays undefined.

## Task 2: Durable monitor
Files: bi_check_agent/monitor.py, notifications.py, worker.py; tests/test_monitor.py.
- [x] Test first detection, repeat, recovery only on successful complete same-scope run, recurrence, failure and ambiguous delivery.
- [x] Implement `MonitorStore(path)` with `save_plan(dict)`, `plans()`, `runs()`, `incidents()`, `incident(id)`, `outbox()`, `run(plan, window_start, window_end, evaluate)`; callback returns `{'findings': [dict], 'evidence': dict}` or raises. Findings contain stable `key`, `rule`, `object`, `value`, `evidence`, optional `worsening_delta`.
- [x] Persist plan version/scope, leased runs, incident history, unique event outbox and delivery state in SQLite transactions. Never recover outside successfully evaluated window. Demo/live modes form separate identity scopes.
- [x] Standalone worker loads enabled persisted plans, computes America/New_York local due windows including configured lookback and open incidents, invokes service callback. Configurable schedule, bounded retries, isolated plan errors.
- [x] Feishu sender requires environment credentials, verified Yuxi receiver and explicit enabled flag; no sends in demo or shadow mode. Stable UUID prevents repeated sends; timeout uncertain is recorded for review.

## Task 3: Local interface and run entrypoints
Files: app.py, bi_check_agent/ui.py, start.command, scripts/start.sh, .env.example, README.md.
- [x] Add UI for overview, query draft, two-window decomposition, incident history and plan editor. All database errors are actionable and secrets suppressed.
- [x] UI includes sample queries and clear demo banner; settings do not embed secrets. Links select incident by ID. Local launcher binds 127.0.0.1.
- [x] Validate with Streamlit AppTest and browser screenshot; launch locally and check HTTP health.

## Task 4: Release and verification
Files: docs/local-testing.md, .github/workflows/tests.yml, requirements.txt.
- [x] Run complete pytest suite and compileall; test new/unchanged/recovery demo path; check worker help and startup.
- [x] Pin installed dependency versions, add CI test command and README local launch link, document remaining production contracts and real-data validation.
- [ ] Review spec coverage and code independently. Scan tracked files for secrets, local user files, databases and credentials. Commit only this isolated repository and push without force.
- [ ] Verify remote commit and local runnable page. Record actual validated behavior and deployment dependencies.
