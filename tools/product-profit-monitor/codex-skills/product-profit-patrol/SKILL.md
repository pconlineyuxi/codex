---
name: product-profit-patrol
description: 在 Codex 中执行 Product Profit 每日只读核查，复用现有 17 条规则，区分订单数据尚待补齐的观察项与经营信号，生成平台、店铺、SKU 维度摘要和证据。用于日常巡查或指定业务日复查。
---

# Product Profit 每日核查

面向 BA，复用本地工具的查询和规则引擎，无需启动 Streamlit 或 worker。用户确认的日常范围是纽约时间前一个完整业务日、全部平台和店铺、全部 17 条规则；保留四类费用和折扣 main_ir 的固定排除及 NULL 排除口径。

## 执行

项目目录为 `/Users/yuxi/Documents/codex project/product-profit-monitor/tools/product-profit-monitor`。从该目录执行：

```sh
.venv/bin/python -m bi_check_agent.codex_patrol --mode review --rules all
```

默认日期在每次执行时按 America/New_York 计算；用户指定历史日期时使用 `--date YYYY-MM-DD`。真实凭证由项目 `.env` 加载，不读取或展示密钥。执行与结果保存在本机，不使用演示数据代替失败的真实查询。

模块输出 JSON，包含执行状态和报告绝对路径。读取 `artifacts.markdown`；需要完整异常分组时再读 `artifacts.json`。报告默认在 `.runtime/codex-patrol/review/`，每次独立归档。`latest.json` 是最新尝试，`last-successful.json` 是最近一次查询成功；后者不表示所有规则可判断或数据已验证通过。

## 判断与反馈

- `review` 为日常只读核查，刷新完整性、源币种及源日期时区尚未确认。明确保留这一限制，不将结果表述为正式数据质量通过或异常已恢复。
- 已确认业务时序：sale_order.create_date 当天创建的订单正常应在当天补充销售额；实际出库订单在当晚至次日凌晨写入成本。出库依据为 order_outbound_history.scan_time，用户已确认是 UTC。创建时间也按 UTC 转纽约日期，目前有实时数据支持这一解释，但尚未单独获得用户确认，报告保留假设。has_product_cost / has_sale_price 只是金额大于零的派生标记，不能作为批次完成凭据。
- 优先读取 `lifecycle_review`：销售检查指定日期创建的有效订单；成本检查指定日期有正数扫描出库且订单+商品能唯一匹配到订单行的记录，包含更早创建但本日出库的订单。销售按创建当日应补齐，成本在次日 09:00 复核。金额读取物化结果视图的直接订单来源 bi.ba_profit_order_line_info，按订单行 ID 关联；与原辅助观察不是同一快照，数量变化不能解释成原异常已恢复。相同订单+商品匹配多行、无匹配行、来源重复、缺失输入均单独标注，不按整单强行分配成本。只输出平台/店铺/SKU 汇总，不提供订单行明细。
- `lifecycle_review.basis=expected_deadline` 只是预期补齐时限，`job_completion_verified=false`，命中只能称为“超过预期时点仍命中的疑点”，不能确认写入失败。当前覆盖指定创建日/出库日，未自动回查更早未解决批次；不可声称已完成全部历史订单检查。两条成本规则的重叠计数读取 overlapping_cost_rule_lines，不相加当作独立订单。
- 原订单日期观察中标为 `assessment=maturity_unverified` 的规则仍不单独用于提醒；创建/出库日复核中的疑点和关联缺口可反馈。不能先过滤掉所有无价格/无成本记录，再声称没有缺失。其余依赖广告到齐及完整成本的比率规则仍保留限制。
- 区分疑似数据问题与经营信号／符号待核查。命中并不等于已确认错误，负促销等符号问题尤其不能直接判错。
- `partial_assessment` 表示有规则依赖字段缺失、生命周期来源关联不明确或该复核失败；给出具体缺口。`maturity_unverified` 表示辅助观察无法核实应到时点；`observations` 表示另有可描述的信号，仍不是确认错误。`empty_unverified` 表示没有记录，不等于数据正常。`failed` 表示检查未完成，不以旧结果冒充本次结果。顶层 query_succeeded 只表示主查询成功，仍须检查 lifecycle_review.status。
- 行级规则计数是来源记录，广告和利润率规则计数是 SKU/平台/店铺/日分组；跨规则可能重复，不把命中数相加当订单数。不提供订单行明细或未经币种统一的总损失估算。
- 日常运行先给出业务日期、执行状态、主要变化和需要 BA 核查的对象，附本地报告链接。结合完整分组定位，不根据展示的前 30 组推断总体。
- 广告观察优先读取 `advertising_observation`：分别说清平台数、店铺数、SKU/日分组数，以及“本次范围内无匹配订单”和“有订单但净销售为零”的分组数。不可把分组数说成平台数，不把同日账面销售为零等同于广告无转化或数据错误；广告归因数据、回补周期和判断窗口尚未确认。
- 每次都保存报告。第一次运行、可反馈信号分布变化、新的检查失败、从失败恢复、规则变化或需要用户处理时反馈；`changed_since_previous_run=false` 且没有新的可行动事项时保持安静。签名排除辅助观察中应到时点未核实的计数变化，但包含创建/出库日复核中的疑点和关联缺口；不证明全部金额或业务指标均未变化。
- 不自动发送飞书、不更改业务数据、不修改原工具计划和正式异常状态。数据库不可达或查询失败时报告问题；不改凭据、不扩大权限、不伪造就绪凭据。后续若用户批准正式模式，可使用 verified，但必须通过原有数据就绪检查。
