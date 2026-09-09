# Product Profit 数据巡查与问题定位

本地开发版：定时检查 Product Profit、保存异常证据与历史，并根据业务问题查询和分解利润变化。延续 V2.0.1 的 Streamlit 界面和受控查询方式。开发、联调先在本机完成，测试通过后再部署公司服务器。

**[打开本机工具 →](http://127.0.0.1:8501)** · 需要先在自己的电脑启动下方程序；这不是 GitHub 托管的在线服务。

## 启动

需要 Python 3.10+（推荐 3.12）。Mac 可以双击本工具目录内的 `start.command`（仓库根目录同名快捷入口也可用），或在终端执行：

```bash
git clone https://github.com/pconlineyuxi/codex.git
cd codex/tools/product-profit-monitor
bash scripts/start.sh
```

第一次运行创建 `.venv` 并安装依赖；打开 <http://127.0.0.1:8501>。终端同时运行网页与巡查 worker，保持打开；按 Ctrl+C 停止。已有虚拟环境时不会自动升级依赖。需要更新时运行 `.venv/bin/python -m pip install -r requirements.txt`。

默认选择**演示数据**，无需数据库或 AI 密钥；合成样本与真实记录按模式隔离。页面先点击“运行一次演示巡查”，能看到成本缺失、运费偏高、有广告无销售三个例子。重复运行不会新增相同异常。业务问题页可以输入：

- 查昨天 SKU DEMO-COST 的产品成本缺失数据
- 查过去7天 SKU DEMO-SHIP 的运费占比异常
- 对 `DEMO-HEALTHY` 选择两个时间窗口，查看利润变化分解

解析草稿需要核对后执行。不支持的业务问题不能自动变成上游根因结论。

## 已实现

- 普通查询与 17 个可选规则；日期、平台、店铺、IR、SKU 等白名单筛选。
- 数据完整性与真实模式就绪门槛；空值不冒充零，截断不冒充检查成功。
- 按纽约时区每日调度、历史回看和未恢复窗口复查；独立 worker，不依赖网页访问。
- SQLite 运行记录、异常生命周期、去重、失败重试与通知发件队列。
- 新增、持续、恢复、复发跟踪；仅规则明确提供恶化阈值时判断恶化。
- 两个时间窗口的利润差异算术分解；成本缺失时拒绝给出完整利润归因。
- 飞书私聊适配器默认关闭，只允许已配置并确认的 Yuxi 接收人；演示和影子模式不发送。

## 真实数据联调

开发阶段可选择“真实数据测试（只读）”：填写一个店铺、最多 7 天，使用真实数据库查询；结果明确标记刷新完整性、币种和源日期时区未核实，不能用于自动巡查或异常恢复。

复制 `.env.example` 为 `.env` 并填写只读 PostgreSQL 连接。先阅读 [本地测试与接入说明](docs/local-testing.md)，核对时间、币种、业务规则和刷新完成契约。数据库连接配置不完整时会拒绝查询；不会自动退回演示数据。

“正式巡查数据”模式需要刷新任务在所有来源刷新提交后生成可信 `ready.json`，其覆盖范围必须包含查询窗口。不能用当前日期或最新订单时间伪装“刷新完成”。第一版只确认 Product Profit 结果视图，未接入上游源表；报告明确展示定位终点。

本地启动仅监听 `127.0.0.1`。公司服务器上线前需接入公司认证、HTTPS、数据备份和访问控制，不能直接将开发端口暴露到公网。

## 测试与 worker

以下命令在 `tools/product-profit-monitor/` 目录中执行。

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m bi_check_agent.worker --once
.venv/bin/python -m bi_check_agent.worker
```

日程在页面“巡查计划”维护。电脑休眠、网络中断或 worker 关闭时无法持续巡查。缺少数据刷新凭据、查询超限和失败会保留未完成状态，不把原有异常标记恢复。

[设计与验收基线](docs/design.md) · [实施计划](docs/superpowers/plans/2026-09-09-product-profit-monitor.md)
