# Codex 工具集合

这里集中维护独立的小工具。每个工具放在 `tools/<工具名>/` 中，拥有自己的代码、依赖、配置、测试和使用说明；根目录用于查找和进入工具。

## 工具导航

| 工具 | 用途 | 当前阶段 | 入口 |
|---|---|---|---|
| [Product Profit 数据巡查与问题定位](tools/product-profit-monitor/) | 定时检查利润数据、跟踪异常，并按业务问题查询和分解利润变化 | 本地开发与联调；默认演示数据 | [使用说明](tools/product-profit-monitor/README.md) · [打开本机工具](http://127.0.0.1:8501) |

“打开本机工具”需要先在自己的电脑启动程序，不是 GitHub 托管的在线服务。

## 启动 Product Profit

Mac 可以双击根目录的 `start.command`，或执行：

```bash
git clone https://github.com/pconlineyuxi/codex.git
cd codex
bash tools/product-profit-monitor/scripts/start.sh
```

启动后访问 <http://127.0.0.1:8501>。运行要求、演示方式和真实数据接入条件见工具自己的 README。

## 仓库结构

```text
codex/
├── README.md
├── start.command                 # Product Profit 启动快捷入口
├── tools/
│   └── product-profit-monitor/   # 工具代码、依赖、测试和文档
└── .github/workflows/            # 各工具的自动检查
```

新增工具时，在 `tools/` 下新建独立目录并更新上方导航表。依赖和运行数据归各工具管理；凭证、虚拟环境和本地数据库不提交到仓库。各工具的自动检查在 `.github/workflows/` 中按目录配置。
