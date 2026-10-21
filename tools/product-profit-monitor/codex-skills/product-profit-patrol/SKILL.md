---
name: product-profit-patrol
description: 在 Codex 中执行 Product Profit 每日只读核查，复用现有 17 条规则，生成平台、店铺、SKU 维度的异常摘要与证据。用于该数据域的日常巡查或指定业务日复查。
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
- 区分疑似数据问题与经营信号／符号待核查。命中并不等于已确认错误，负促销等符号问题尤其不能直接判错。
- `partial_assessment` 表示有规则依赖字段缺失；给出无法判断的规则与字段。`empty_unverified` 表示没有记录，不等于数据正常。`failed` 表示检查未完成，不以旧结果冒充本次结果。
- 行级规则计数是来源记录，广告和利润率规则计数是 SKU/平台/店铺/日分组；跨规则可能重复，不把命中数相加当订单数。不提供订单行明细或未经币种统一的总损失估算。
- 日常运行先给出业务日期、执行状态、主要变化和需要 BA 核查的对象，附本地报告链接。结合完整分组定位，不根据展示的前 30 组推断总体。
- 每次都保存报告。第一次运行、异常分布变化、新的检查失败、从失败恢复、规则变化或需要用户处理时反馈；`changed_since_previous_run=false` 且没有新的可行动事项时保持安静。签名仅比较规则命中分布和状态，不证明金额等所有业务指标均未变化。
- 不自动发送飞书、不更改业务数据、不修改原工具计划和正式异常状态。数据库不可达或查询失败时报告问题；不改凭据、不扩大权限、不伪造就绪凭据。后续若用户批准正式模式，可使用 verified，但必须通过原有数据就绪检查。
