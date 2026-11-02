"""Render the user-facing rule list from the executable rule configuration."""
from pathlib import Path
from bi_check_agent.patrol_policy import CONFIG, POLICY, ROOT


def render():
    lines = [
        '# Product Profit 统一巡检规则', '',
        f"规则策略版本：{POLICY['version']}。本文件由配置生成，不单独维护第二份规则。", '',
        '## 工具、Skill 与定时任务的边界', '',
        '| 部分 | 职责 | 规则来源 |', '| --- | --- | --- |',
        '| 巡检工具（Python 模块及网页） | 查询、数值规则判断、业务适用性判断、历史跟踪与报告 | config/diagnostic_rules.yaml；core.py、patrol_policy.py 执行 |',
        '| 每日 Skill（product-profit-patrol） | 调用无界面命令、读取证据、解释需要处理的事项 | 读取工具生成的规则清单及 notification；不另写阈值或自行豁免 |',
        '| Codex 定时任务 | 每天纽约时间 09:00 触发 Skill | 仅保存触发与执行指令 |', '',
        '网页按用户选择的查询日期查物化视图；每日核查额外按订单创建日和商品扫描出库日查直接来源。查询范围不同，数值公式与业务适用性函数共用，不能把不同快照或日期范围的数量差当成恢复。', '',
        '## 全部数值规则', '',
        '销售额统一指扣除促销绝对值后的净销售额。基础产品成本与产品／硬件单件成本均不含运费、佣金和广告费。', '',
        '| 规则 ID | 名称 | 条件 | 粒度 | 每日处理 |', '| --- | --- | --- | --- | --- |']
    for key,r in CONFIG['queryable_rules'].items():
        tags = r.get('tags',[])
        role = ('出库后，高优先级复查' if key in POLICY['high_rules'] else '创建／出库日普通复查') if set(tags)&{'lifecycle_sales','lifecycle_cost'} else '辅助观察，计数变化不单独提醒'
        condition = r['condition_zh'].format(**CONFIG['thresholds'])
        lines.append(f"| `{key}` | {r['label']} | {condition} | {'来源记录' if r['level']=='line' else 'SKU／平台／店铺／日'} | {role} |")
    lines += ['', '## 固定范围与业务适用性', '',
        "固定排除 main_ir 为 SHIPPING FEE、SHIPMENT DISCOUNT、GIFT WRAPPER FEE、PROMOTION DISCOUNT 的记录，以及 NULL main_ir；查询日期结束日不包含。不能先筛掉无价格／无成本记录再声称没有缺失。", '',
        '销售按 sale_order.create_date 创建日检查，预期创建当天补齐；成本只按 order_outbound_history.scan_time 正数扫描的商品检查，预期出库后次日纽约时间 09:00 补齐。扫描时间已确认 UTC；创建时间按 UTC 解释，保留待确认假设。订单＋商品必须唯一对应订单行，不能把整单当作已全部出库。', '',
        '已取消：订单取消状态、Walmart 取消状态或 Amazon 已取消标记成立，且查询到交付数量为零、没有正数扫描、没有平台履约状态证据，金额规则记为 exempt（不适用）。若取消与交付／扫描／平台状态并存，记为无法确认，保留核查。买家申请取消不等于已取消。', '',
        f"长期未履约：仅对净销售额为零、数量为正、创建满 {POLICY['stale_unfulfilled_days']} 个纽约自然日，且扫描、交付数量、平台履约字段读取齐全并无履约证据的订单，记为 deferred（长期未履约待核对）。首次转入提醒一次，继续复查，仅状态变化再提醒；不声称已取消或金额已修复。有已交付数量、扫描或非空且非明确取消的平台履约状态时不适用；字段缺失不能当作零。", '',
        '允许零成本：仅配置中已确认的商品适用。基础成本必须为数值零，装拆硬件成本须完整、非负且未发生过量拆件；空值、负成本及异常拆件不豁免。没有 PO 本身不是全局排除条件，其他商品仍需核对 PO、商品采购价及实际成本来源。', '']
    for item in POLICY['zero_cost_exceptions']:
        lines.append('- '+ '／'.join(str(v) for v in item['match'].values())+'：'+item['reason'])
    lines += ['', '## 历史复查、提醒与订单号', '',
        f"从最近 {POLICY['history_days']} 个业务日期的已有报告导入疑点；open、unknown 和 deferred 超过窗口继续复查。clear、exempt 在窗口内继续复查，窗口外保留归档。缺少历史报告的日期明确列为覆盖缺口。", '',
        f"实际命中严重成本规则且已出库的疑点每 {POLICY['high_interval_days']} 天提醒；其他销售、成本及关联疑点首次发现、状态变化时提醒，持续 {POLICY['normal_interval_days']} 天后及此后每 {POLICY['normal_interval_days']} 天提醒。同一天重复运行不重复持续提醒。exempt 不持续提醒；deferred 只在进入或状态变化时提醒。", '',
        '通知列出平台、店铺、SKU、业务订单号、规则、状态、优先级、持续天数及核查原因。业务订单号取 sale_order.name，不输出数据库主键、客户或地址信息。旧报告仅有汇总时，当前查到的订单号标注“本次分组匹配”；本次未能验证时标“上次已知”。不能宣称恢复了旧订单身份。规则项可重叠，不能加总当订单数。', '',
        '通知以 notification.should_notify、items、health_changes 为准。经营信号、广告、负促销、负佣金以及原始待到齐计数变化不单独刷屏。只读复查基于预期时限，未接入补跑任务成功凭据；本次不再命中不等于正式恢复。保留刷新完整性、源币种和源日期口径限制。', '',
        '## 查看与更新', '',
        '查看当前规则（不连接数据库）：`.venv/bin/python -m bi_check_agent.codex_patrol --list-rules`。', '',
        '修改统一配置／判断代码后运行测试，再执行 `.venv/bin/python -m bi_check_agent.rulebook` 重新生成本文件；Skill 只引用它，不复制阈值。网页或 worker 如在运行，需要重启加载新版本。', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    path = ROOT/'docs/patrol-rules.md'
    path.write_text(render(),encoding='utf-8')
    print(path)
