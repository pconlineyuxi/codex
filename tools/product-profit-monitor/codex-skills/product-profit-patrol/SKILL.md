---
name: product-profit-patrol
description: 在 Codex 中调用 Product Profit 只读巡检工具，依据统一规则和通知结果解读每日与历史疑点；Skill 不另行维护或推断业务规则。
---

# Product Profit 每日巡检执行 Skill

## 职责与唯一规则来源

本 Skill 是执行和解读流程；巡检工具负责数据库查询、规则判断、适用性判断、历史跟踪、提醒去重与报告归档；Codex 定时任务仅负责触发。

项目目录：`/Users/yuxi/Documents/codex project/product-profit-monitor/tools/product-profit-monitor`。

每次先读取项目 `docs/patrol-rules.md`。它由 `config/diagnostic_rules.yaml` 生成；规则公式在 `core.py`，业务适用性在 `patrol_policy.py`，状态和提醒在 `patrol_tracking.py`。查看当前配置可执行 `.venv/bin/python -m bi_check_agent.codex_patrol --list-rules`，此命令不连接数据库。不要在 Skill、定时提示或回答中建立第二套阈值，也不能自行把“没有内部扫描”解释成已取消。

## 执行

从项目目录执行：

```sh
.venv/bin/python -m bi_check_agent.codex_patrol --mode review --rules all
```

日期由模块按 America/New_York 计算前一个完整自然日；用户指定历史日期时加 `--date YYYY-MM-DD`。凭证由本地配置加载，不读取或输出凭证。真实查询失败不能用演示数据替代。不启动网页或 worker。

每次自动归档。读取终端输出的报告路径，先读取 `notification.should_notify`、`notification.items`、`notification.health_changes`，再读 `history_review`、`lifecycle_review` 和 Markdown；仅按需读取 JSON 指定字段，避免整份 evidence 或私有跟踪文件输出。`latest.json` 是最近尝试，`last-successful.json` 仅表示最近主查询成功。

## 反馈

- 为 true 时反馈本次新增、状态变化、到期提醒和检查健康变化；为 false 且无新的可行动事项时保持安静。不因原始规则计数、广告或符号观察变化重复提醒。
- 按平台／店铺／SKU 组织，**尽量给出 `order_numbers` 业务订单号**，同时说明规则、状态、优先级、持续天数和 BA 需核对的事实。多规则对应同一订单时合并说明，不相加为独立订单数。缺少订单号如实写未取得，不猜测。
- `order_reference_basis=exact` 是稳定订单行对应的业务订单号；`current_group` 只代表本次在旧日期／分组中查到的订单；`last_known` 是上次已知、本次未核实。保留这些区别。
- `exempt` 是业务不适用，不是已修复；`deferred` 是长期未履约待核对，不是已取消；`clear` 是本次不再命中，不是正式恢复；`unknown` 是证据不足。以工具输出为准，不能只看金额、年龄或是否有 PO 自行改判。
- 成本为零时先读取工具的 `lifecycle_review.cost_provenance` 和提醒项中的 `cost_review`，说明适用 PO、商品采购价、入库单及证据状态。工具已查明的事实应直接解释，不再笼统要求 BA 自查；仅把尚缺的业务或任务凭据交给 BA。`actual_ir` 是实际出库商品，可能是组合 SKU 内配件。
- 多销售记录的关联以工具的 `association_basis` 为准：已通过 SN 或完整数量核实的多行不再视为关联缺口；无法核实时保留疑点。不要由销售记录条数自行推断异常。
- 工具按统一规则执行已确认零成本例外、取消判定和长期未履约判定。按本次 cost_review 说明无适用 PO 的零成本判定。仅在工具已核实赠品时说明赠品来源。成本为空、为零、为负应分别解释。只有内部没有扫描而已有交付数量或平台履约证据时，不能断言无需交付。
- 历史已记录疑点与当天创建／出库批次分别说明。缺少历史报告、复查失败、来源缺失／重复、多行匹配和固定筛选变化保留覆盖或关联缺口。原分组复查不能冒充已恢复旧订单身份。
- 必须检查顶层状态、`lifecycle_review.status` 和 `history_review.status`；主查询失败时仍检查历史复查结果。缺少 notification 或历史复查失败时报告缺口，不假定没有历史问题。
- 预期补齐时限不等于任务成功证据。直接来源和物化结果不一定是同一快照，不用计数差推算恢复。保留刷新完整性、源币种、源日期解释及规则适用限制。空数据不等于通过。
- 广告只按 `advertising_observation` 解释平台数、店铺数、SKU／日分组数与同日订单匹配；同日账面销售不是广告归因销售。
- 附当次报告链接。仅输出业务订单号及核查必要字段，不输出数据库主键、订单行明细、客户或地址信息。私有 `tracking-state.json` 不展示、不删除、不重置。

## 操作边界

数据库只读；不修改业务数据、原网页工具计划或正式异常状态，不向飞书或其他人发送消息。网络沙箱受限时仅通过现有授权执行同一只读命令；仍失败则报告阻塞，不改凭据、不扩权限、不绕过权限。调整规则必须在项目统一配置／代码修改、验证并更新生成文档，不能只改 Skill 文案假装工具已生效。
