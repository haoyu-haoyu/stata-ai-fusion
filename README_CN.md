<div align="center">
  <img src="assets/icon.png" alt="Stata AI Fusion" width="120">
  <h1>Stata AI Fusion</h1>
  <p><strong>MCP Server + Skill 知识库 + VS Code 扩展，三合一 Stata AI 集成方案</strong></p>
  <p>让 AI 直接执行 Stata 代码、生成高质量统计分析、并在 VS Code 中提供完整的 IDE 体验。</p>

  [![PyPI](https://img.shields.io/pypi/v/stata-ai-fusion)](https://pypi.org/project/stata-ai-fusion/)
  [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
  [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
  [![VS Code Marketplace](https://img.shields.io/visual-studio-marketplace/v/statafusion.stata-ai-fusion)](https://marketplace.visualstudio.com/items?itemName=statafusion.stata-ai-fusion)

  <p>
    <a href="#快速开始">快速开始</a> &bull;
    <a href="#功能特性">功能特性</a> &bull;
    <a href="#mcp-工具参考">MCP 工具</a> &bull;
    <a href="#skill-知识库">Skill 知识库</a> &bull;
    <a href="#vs-code-扩展">VS Code 扩展</a> &bull;
    <a href="README.md">English</a>
  </p>
</div>

---

## 为什么需要 Stata AI Fusion？

Stata 是经济学、政治学、流行病学和生物统计学中使用最广泛的统计软件之一。然而，当 R 和 Python 用户已经享受了多年的 AI 辅助编程时，Stata 用户一直被排除在这场 AI 革命之外。

**stata-ai-fusion** 填补了这个空白。它让 AI 助手（Claude、Cursor、GitHub Copilot 等）能够启动真实的 Stata 会话、执行命令、查看数据、提取估计结果并捕获图形 -- 所有这些都通过开放的 [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) 实现。

项目以三个互补组件的形式发布，覆盖所有使用场景：

| 组件 | 功能 | 适用人群 |
|------|------|---------|
| **MCP Server** | 11 个工具让 MCP 兼容的 AI 直接操作 Stata | Claude Desktop、Claude Code、Cursor 用户 |
| **Skill 知识库** | 5,653 行 Stata 专业知识供 AI 参考 | Claude.ai Project / Skill 用户 |
| **VS Code 扩展** | 语法高亮、代码片段、终端执行 | 在 VS Code 或 Cursor 中编写 `.do` 文件的用户 |

---

## 架构

<p align="center">
  <img src="assets/stata-ai-fusion-flow.gif" alt="Architecture" width="800">
</p>

数据流程：

1. **AI 助手** 通过 MCP 发送工具调用（如 `run_command`）。
2. **MCP Server** 将请求分发给 **Session Manager**，后者维护一个或多个持久化的交互式 Stata 进程。
3. **Stata** 执行命令；服务器捕获输出、清除 SMCL 标记、检测错误，并自动导出新生成的图形。
4. 清理后的结果（文本 + 可选的 base64 图片）返回给 AI，AI 解读结果并回复用户。

---

## 快速开始

### Claude Code（推荐）

```bash
# 一行命令注册 MCP 服务器
claude mcp add stata-ai-fusion -- uvx --from stata-ai-fusion stata-ai-fusion

# 验证
claude mcp list
```

然后试试：

```
> 帮我用 Stata 加载 auto 数据集，做一个 price 对 mpg 和 weight 的回归分析
```

### Claude Desktop

编辑配置文件：

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "stata": {
      "command": "uvx",
      "args": ["--from", "stata-ai-fusion", "stata-ai-fusion"]
    }
  }
}
```

重启 Claude Desktop，Stata 工具将出现在工具列表中。

### Cursor / VS Code (MCP)

在项目根目录创建 `.cursor/mcp.json` 或 `.vscode/mcp.json`：

```json
{
  "servers": {
    "stata": {
      "command": "uvx",
      "args": ["--from", "stata-ai-fusion", "stata-ai-fusion"]
    }
  }
}
```

### Claude.ai（仅 Skill 模式）

此模式仅提供代码生成指导，不具备实时 Stata 执行能力。

1. 从 [Releases](https://github.com/haoyu-haoyu/stata-ai-fusion/releases) 页面下载 `stata-ai-fusion-skill.zip`。
2. 前往 **Claude.ai > Project > Project Knowledge > Upload**。
3. 上传 zip 文件。

AI 将在编写 Stata 代码时参考这个 5,653 行的知识库。

### VS Code 扩展

```bash
# 方法 1: 在 VS Code 扩展面板搜索 "Stata AI Fusion"

# 方法 2: 从 GitHub Release 下载
code --install-extension stata-ai-fusion-0.2.2.vsix

# 方法 3: Cursor
cursor --install-extension stata-ai-fusion-0.2.2.vsix
```

---

## 功能特性

### MCP Server -- 11 个 AI 驱动的分析工具

服务器暴露 11 个 MCP 工具，任何 MCP 兼容的 AI 助手都可以调用。

#### 对话示例

```
用户: "分析 auto 数据集中汽车价格的影响因素"

AI 调用: run_command("sysuse auto, clear")
AI 调用: inspect_data()                          -> 74 obs, 12 variables
AI 调用: run_command("regress price mpg weight foreign, robust")
AI 调用: get_results("e", "N r2 F")              -> N=74, R²=0.52, F=29.1
AI 调用: run_command("scatter price mpg || lfit price mpg")
AI 调用: export_graph(format="png")               -> [图片]

AI: "回归结果显示，mpg 每增加 1 单位，价格下降 $49.5..."
```

### Skill 知识库 -- 5,653 行 Stata 专业知识

知识库采用 **渐进式披露** 架构：

- **SKILL.md**（486 行）作为入口路由文档。
- **14 个参考文件** 覆盖不同领域；AI 按需加载。
- AI 不会一次性读取所有 5,653 行 -- 仅获取当前任务所需的内容。

### VS Code 扩展 -- 完整的 Stata IDE

| 功能 | 快捷键 | 说明 |
|------|--------|------|
| 运行选中代码 | `Cmd+Shift+Enter` | 在终端中执行选中的 Stata 代码 |
| 运行文件 | `Cmd+Shift+D` | 执行整个 `.do` 文件 |
| 语法高亮 | -- | 25 个语法规则，覆盖命令、函数、宏 |
| 代码片段 | `Tab` | 30 个代码片段（`reg`、`merge`、`foreach`、`esttab`...） |
| 图形预览 | -- | 在 VS Code 内查看 Stata 图形 |
| 自动 MCP 配置 | -- | 为 Cursor/VS Code 自动生成 `.vscode/mcp.json` |

---

## MCP 工具参考

| 工具 | 说明 | 示例 |
|------|------|------|
| `run_command` | 交互式执行短命令 | `run_command(code="regress price mpg weight, robust")` |
| `run_do_file` | 批处理模式运行 `.do` 文件（适合长脚本） | `run_do_file(path="/path/to/analysis.do")` |
| `inspect_data` | 描述当前内存中的数据集 | 返回观测数、变量名、类型、标签 |
| `codebook` | 生成指定变量的 codebook | `codebook(variables="price mpg foreign")` |
| `get_results` | 提取存储的结果（r/e/c 类） | `get_results(result_class="e", keys="N r2")` |
| `export_graph` | 导出当前图形为 PNG/SVG/PDF | 返回 base64 编码的图片数据 |
| `search_log` | 搜索 Stata 会话日志 | `search_log(query="error", regex=true)` |
| `install_package` | 安装 SSC 或用户编写的包 | `install_package(package="reghdfe")` |
| `cancel_command` | 发送中断信号（SIGINT）取消运行中的命令 | `cancel_command(session_id="default")` |
| `list_sessions` | 列出所有活跃的 Stata 会话 | 返回会话 ID、类型、存活状态 |
| `close_session` | 关闭指定的 Stata 会话 | `close_session(session_id="default")` |

---

## Skill 知识库

| 参考文件 | 行数 | 覆盖内容 |
|----------|-----:|---------|
| `syntax-core.md` | 564 | 命令语法、数据类型、运算符、宏 |
| `data-management.md` | 481 | merge、reshape、append、collapse、encode |
| `econometrics.md` | 412 | OLS、IV、面板数据、GMM、分位数回归 |
| `causal-inference.md` | 433 | DiD、RDD、合成控制法、IPW、事件研究 |
| `survival-analysis.md` | 332 | stset、stcox、streg、竞争风险、KM 曲线 |
| `clinical-data.md` | 497 | MIMIC-IV、ICD-9/10、KDIGO、Sepsis-3、住院时长 |
| `graphics.md` | 463 | twoway、图形选项、配色方案、导出 |
| `tables-export.md` | 348 | esttab、putdocx、collect、LaTeX/Word 输出 |
| `error-codes.md` | 349 | 常见 Stata 错误及修复方法 |
| `defensive-coding.md` | 389 | assert、capture、confirm、isid、临时文件 |
| `mata.md` | 532 | Mata 编程、矩阵运算、优化 |
| `packages/reghdfe.md` | 127 | 高维固定效应回归 |
| `packages/coefplot.md` | 133 | 系数图与事件研究图 |
| `packages/gtools.md` | 107 | 快速数据操作（gcollapse、gegen） |
| **合计** | **5,653** | |

---

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `STATA_PATH` | 自动检测 | Stata 可执行文件的完整路径 |
| `MCP_STATA_LOGLEVEL` | `INFO` | 日志级别（`DEBUG` / `INFO` / `WARNING`） |
| `MCP_STATA_TEMP` | 系统临时目录 | 会话临时文件的存放目录 |

---

## Stata 自动发现

服务器通过三级策略自动检测 Stata 安装：

1. **环境变量** -- `STATA_PATH` 优先级最高。
2. **标准路径** --
   - macOS: `/Applications/Stata*/`、`/Applications/StataNow/`
   - Linux: `/usr/local/stata*/`、`/usr/local/bin/`
   - Windows: `C:\Program Files\Stata*\`
3. **系统 PATH** -- `which stata-mp` / `which stata-se` / `which stata`

支持的版本：**MP**、**SE**、**IC**、**BE**（Stata 17、18、19 及 StataNow）。

如果自动检测失败，可手动设置环境变量：

```bash
export STATA_PATH="/Applications/Stata/StataMP.app/Contents/MacOS/stata-mp"
```

---

## 多会话支持

服务器支持多个并发 Stata 会话，数据完全隔离：

- 每个会话维护自己的数据集、变量和估计结果。
- 会话在工具调用之间持久化 -- 无需每次命令后重新加载数据。
- 默认会话自动创建；可创建命名会话用于并行工作流。
- 空闲会话 1 小时后自动清理（可配置）。
- 服务器关闭时所有会话优雅清理。

```
AI 调用: run_command(code="sysuse auto, clear", session_id="session_A")
AI 调用: run_command(code="sysuse nlsw88, clear", session_id="session_B")
# session_A 有 74 条观测 (auto)，session_B 有 2,246 条观测 (nlsw88)
```

---

## 开发

```bash
# 克隆并设置
git clone https://github.com/haoyu-haoyu/stata-ai-fusion.git
cd stata-ai-fusion
uv sync

# 运行单元测试（不需要 Stata）
uv run pytest tests/test_discovery.py -v

# 运行集成测试（需要 Stata）
uv run pytest tests/test_integration.py -v

# 构建 Python 包
uv build

# 构建 VS Code 扩展
cd vscode-extension && npm install && npm run build
```

---

## 测试

| 测试套件 | 数量 | 需要 Stata |
|----------|-----:|:----------:|
| `test_discovery.py` | 39 | 否 |
| `test_integration.py` | 46 | 是 |
| **合计** | **85** | |

全部 85 个测试在 Stata MP 19 (macOS arm64) 上通过。

---

## 项目结构

```
stata-ai-fusion/
├── src/stata_ai_fusion/
│   ├── __main__.py          # CLI 入口
│   ├── server.py            # MCP 服务器 + 资源注册
│   ├── stata_discovery.py   # Stata 自动发现
│   ├── stata_session.py     # 交互式 & 批处理会话管理器
│   ├── graph_cache.py       # 图形捕获与 base64 编码
│   ├── result_extractor.py  # r()/e()/c() 结果提取
│   └── tools/               # 11 个 MCP 工具实现
├── skill/
│   ├── SKILL.md             # 主 Skill 路由文档（486 行）
│   └── references/          # 14 个参考文档（5,167 行）
├── vscode-extension/
│   ├── src/                 # TypeScript 扩展源码（5 个文件）
│   ├── syntaxes/            # TextMate 语法规则
│   └── snippets/            # 30 个代码片段
├── tests/                   # 85 个测试（39 单元 + 46 集成）
├── assets/                  # 图标、架构图
└── pyproject.toml
```

---

## 贡献

欢迎贡献！以下是一些参与方式：

- **Bug 报告**：创建 issue，描述问题、Stata 版本和操作系统。
- **新 Skill 参考文档**：在 `skill/references/` 中添加覆盖某个 Stata 主题的 `.md` 文件。
- **新 MCP 工具**：在 `src/stata_ai_fusion/tools/` 中实现工具并注册。
- **VS Code 改进**：扩展语法规则或添加代码片段。

提交 PR 前请运行 `uv run pytest tests/ -v`。

---

## 许可证

MIT -- 详见 [LICENSE](LICENSE)。

## 致谢

- [Stata](https://www.stata.com/) by StataCorp
- [Model Context Protocol](https://modelcontextprotocol.io/) by Anthropic

---

<p align="center">
  <a href="https://pypi.org/project/stata-ai-fusion/">PyPI</a> &bull;
  <a href="https://marketplace.visualstudio.com/items?itemName=statafusion.stata-ai-fusion">VS Code Marketplace</a> &bull;
  <a href="https://github.com/haoyu-haoyu/stata-ai-fusion/releases">Releases</a> &bull;
  <a href="README.md">English</a>
</p>
