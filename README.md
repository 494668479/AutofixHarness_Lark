# Autofix Manager

Autofix Manager 是一个本地自动修复管理台。它把飞书群里的多维表格任务，转换成可排队、可追踪、可验证、可重试的本地 AI 修复流程。

它不是一个通用聊天机器人，也不是远程修复平台。它的核心定位是：

```text
飞书消息 / Base 记录 -> 任务过滤 -> git worktree -> AI CLI 修复 -> 工具层验证 -> 提交 -> 回写 Base -> 飞书回复
```

## 当前能力

- 启动本地网页管理台，运行后自动打开浏览器。
- 同一进程管理多个项目配置，多个项目可同时激活。
- 全局共享并发数，默认并发为 `2`。
- 支持任务超时，默认 `180` 秒。
- 支持 AI 多轮修复循环，默认 `3` 轮，硬上限 `10` 轮。
- 支持多种可编辑型 AI CLI：`codex`、`claude`、`cursor-agent`、`gemini`、`deepseek`、`qwen`、`opencode`、自定义 CLI。
- AI 配置组可保存 CLI 后端、CLI 路径、AI 大脑、模型名、API Key 环境变量名、CLI 私有配置目录、额外参数和自检状态。
- 项目配置可绑定不同 AI 配置组。
- 项目配置可设置自动修复时间段：全天自动修复、不自动修复、指定分钟级时间段。
- 支持按多维表格名称、状态值、修复版本过滤任务。
- 每条任务创建独立 git worktree 和独立修复分支。
- 支持上下文收集命令，把项目上下文注入首次 Prompt。
- 支持验证命令，把编译、测试、lint、diff 检查结果反馈给下一轮 Prompt。
- 支持飞书收到任务回显和任务完成结果回显。
- 任务列表和任务详情支持 SSE 自动刷新，SSE 不可用时退回轮询。
- 任务详情展示摘要、修复思路、AI 输入、AI 输出、上下文快照、验证输出、飞书回显和轮次历史。
- 任务完成后有短提示音。
- 失败、超时、队列卡住、Base 回写失败等状态都有对应的人工处理入口。

## 目录结构

```text
Autofix/
|-- README.md
|-- autoSetup.py
|-- requirements.txt
|-- Document/
|   |-- Autofix本地管理台使用说明.md
|   `-- Autofix Harness 编写复盘.md
|-- templates/
|   `-- *.html
`-- tools/
    |-- autofix_manager.py
    |-- autofix_store.py
    |-- autofix_harness.py
    |-- autofix_utils.py
    |-- autofix_views.py
    `-- ...
```

## 依赖

必需依赖：

- Python 3.9+
- Git
- `lark-cli`
- 至少一个可编辑型 AI CLI
- 目标项目本身必须是 Git 仓库

推荐依赖：

- `rg`，也就是 ripgrep。默认上下文收集命令会使用它搜索问题摘要。
- 项目自己的测试、编译或 lint 命令，例如 `npm test`、`npm run build`、`pytest`、`go test ./...`、`cargo test`。

当前 Python 源码只使用标准库和项目内模块，`requirements.txt` 暂无第三方 Python 包。

## 一键安装检查

可以先运行：

```bash
python3 Autofix/autoSetup.py --dry-run --ai-cli none
```

确认将要执行的检查和安装命令。

执行安装和检查：

```bash
python3 Autofix/autoSetup.py
```

指定希望确保存在的 AI CLI：

```bash
python3 Autofix/autoSetup.py --ai-cli codex
python3 Autofix/autoSetup.py --ai-cli claude
python3 Autofix/autoSetup.py --ai-cli gemini
python3 Autofix/autoSetup.py --ai-cli qwen
python3 Autofix/autoSetup.py --ai-cli opencode
```

说明：

- `auto` 模式会优先使用本机已有的 AI CLI；如果没有找到，会尝试安装 Codex CLI。
- `none` 模式只检查，不安装 AI CLI。
- `deepseek`、`cursor-agent` 等 CLI 如果无法自动安装，需要手工安装后在 AI 配置页填写路径或点击搜索。

## 启动

```bash
python3 Autofix/tools/autofix_manager.py
```

启动后会自动打开网页。

不自动打开浏览器：

```bash
python3 Autofix/tools/autofix_manager.py --no-open
```

指定端口：

```bash
python3 Autofix/tools/autofix_manager.py --port 8000
```

指定数据库文件：

```bash
python3 Autofix/tools/autofix_manager.py --db Autofix/.feishu_echo/autofix.sqlite3
```

默认数据库位置：

```text
Autofix/.feishu_echo/autofix.sqlite3
```

## 飞书配置

你需要准备一个飞书机器人或应用，并让 `lark-cli` 可以用 bot 身份完成这些操作：

- 读取目标群消息。
- 回复目标群消息。
- 解析 Base 链接或消息卡片。
- 读取多维表格记录。
- 更新多维表格记录状态。

常用检查命令：

```bash
lark-cli auth status --json --verify
lark-cli im +chat-search --as bot --json --query "群名" --chat-modes group --page-size 20
lark-cli im +chat-messages-list --as bot --chat-id "oc_xxx" --json --order desc --page-size 10 --no-reactions
```

多维表格建议至少包含：

- 问题描述字段，例如 `问题模块/描述`、`问题模块` 或 `描述`。
- 状态字段，默认字段名为 `状态`。
- 修复版本字段，默认字段名为 `修复版本`。

## AI 配置组

进入管理台后点击 `AI配置`。

每组 AI 配置代表一套“可编辑型 CLI + AI 大脑 + 模型/凭据配置”。

主要字段：

| 字段 | 作用 |
| --- | --- |
| 配置组名称 | 页面展示名，便于项目配置选择 |
| CLI 后端 | 实际执行代码编辑的 CLI 类型 |
| CLI 路径 | CLI 可执行文件路径，留空时自动搜索 |
| AI 大脑 | 标记背后的模型 Provider，例如 GPT、Claude、Gemini、DeepSeek、GLM |
| 模型名 | 留空表示使用 CLI 当前默认模型；填写后只影响 Autofix 子进程 |
| API Key 环境变量名 | 只保存变量名，不保存明文密钥 |
| CLI 私有配置目录 | 可选，只对子进程生效，不修改用户全局 CLI 配置 |
| 额外 CLI 参数 | 追加到本次 CLI 调用中 |
| 自定义命令模板 | 当 CLI 后端选择 `custom` 时使用 |
| 自检状态 | 验证这组 CLI 是否能在临时仓库里实际编辑文件 |

自定义 CLI 必须能进入指定 worktree、读取文件、修改文件并输出结果，否则不适合作为自动修复后端。

## 项目配置

进入管理台后点击 `项目配置`。

每个项目配置代表一条自动修复规则：

```text
飞书群 + 多维表格 + Git 仓库 + 基准分支 + worktree 根目录 + AI 配置组
```

主要字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| 激活 | 关闭 | 开启后后台监听该项目配置对应的飞书群 |
| AI 配置组 | 默认 Codex | 本项目使用哪组 AI CLI 和模型 |
| 群名 | 群名 | 页面展示和初始化查找使用 |
| 飞书 Cli 应用 id | cli_xxxxx | 当前飞书 CLI 使用的应用 id |
| 多维表格名称 | 空 | 只处理原始消息来源名匹配的任务 |
| 状态字段名称 | 状态 | Base 中表示任务状态的字段名 |
| 完成状态值 | AI修复完成 | 任务成功后写回 Base 的状态值 |
| 可处理状态值 | 待 AI 修复、重开 | Base 当前状态命中列表才进入修复 |
| 修复版本 | 空 | 必须和 Base 行中的修复版本一致 |
| 自动修复模式 | 全天自动修复 | 可设为不自动修复或指定时间段 |
| 上下文收集命令 | 默认三条命令 | 创建 worktree 后、首次 Prompt 前执行 |
| 验证命令 | git diff --check | AI 声明完成且已有 diff 后执行 |
| git 路径 | 必填 | 目标项目仓库根目录 |
| 基于的分支 | 必填 | 修复分支从该分支创建 |
| 分支前缀 | fix/base- | 自动创建修复分支的前缀 |
| 修改存放路径 | `<REPO_ROOT>/.worktrees` | 任务 worktree 所在根目录 |
| WorkRoot 名称 | 仓库名 | 任务列表和详情中用于区分项目的名称 |

## 自动修复时间段

项目配置支持三种模式：

- 全天自动修复：收到任务后直接进入队列。
- 不自动修复：收到任务后进入 `暂停`，只能人工立即开始或设为无需修复。
- 指定时间段：精确到分钟，支持当日结束或次日结束。

示例：

```text
当日 20:01 至次日 12:43
当日 20:01 至次日 20:30
```

不在时间段内收到的任务会先记录为 `暂停`。进入时间段后，后台会按创建顺序释放暂停任务，继续共享全局并发上限。

## Prompt 与 Harness

Prompt 页面可以分别配置：

- 首次 Prompt：第 1 轮使用，用于从零理解任务并开始修复。
- 二次 Prompt：第 2 轮及之后使用，用于根据上一轮回复、当前 diff 和验证输出继续收敛。

常用占位符：

| 占位符 | 说明 |
| --- | --- |
| `{{task_info}}` | 任务基础信息 |
| `{{source_message}}` | 飞书原始消息 |
| `{{base_fields}}` | Base 当前行字段 JSON |
| `{{context_snapshot}}` | 上下文收集命令输出 |
| `{{previous_ai_reply}}` | 上一轮 AI 回复 |
| `{{current_diff}}` | 当前 git diff 和 status |
| `{{verify_output}}` | 验证命令输出 |
| `{{first_prompt}}` | 首轮 Prompt 原文 |
| `{{worktree_path}}` | 当前任务 worktree 路径 |
| `{{record_id}}` | Base 记录 ID |
| `{{branch_name}}` | 当前任务分支名 |
| `{{workroot_name}}` | 当前项目 WorkRoot 名称 |
| `{{repair_version}}` | 当前任务修复版本 |
| `{{issue_summary}}` | 任务摘要 |

AI 回复必须包含：

```text
修复思路：
本轮处理：
验证情况：
风险说明：
修复状态：已完成
```

最后一行的 `修复状态` 只能是：

```text
修复状态：已完成
修复状态：未完成
修复状态：无法修复
```

Harness 判定规则：

- AI 声明已完成后，工具层先检查是否存在 git diff。
- 无 diff 会按无法修复处理。
- 有 diff 后运行项目配置的验证命令。
- 验证失败会把当前 diff 和验证输出注入二次 Prompt。
- 达到最大循环次数仍未通过，任务进入 `failed`。
- 超过全局超时时间，任务进入 `超时`。
- AI 明确无法修复，任务进入 `拒绝修复`。
- diff 存在且验证通过后，系统提交修改、回写 Base 状态并回复飞书。

## 任务状态

| 状态 | 含义 |
| --- | --- |
| `queued` | 已入队，等待执行 |
| `running` | 正在创建 worktree、调用 AI 或验证 |
| `succeeded` | 修复成功、已提交并已回写 Base |
| `failed` | 执行失败或达到最大循环次数仍未完成 |
| `超时` | 任务运行超过全局超时时间 |
| `拒绝修复` | 版本不匹配、AI 判断无法修复或不适合自动处理 |
| `状态不支持` | Base 当前状态不在可处理状态列表中 |
| `暂停` | 当前不在自动修复时间段内 |
| `无需修复` | 人工确认不处理 |
| `base_update_failed` | 代码已提交，但 Base 状态回写失败 |

## 任务操作

任务详情页支持：

- 删除任务记录。
- 删除任务记录并删分支。
- `queued` 任务重新入队。
- `failed` / `超时` 任务基于当前 worktree 继续重试。
- `failed` / `超时` 任务清空 worktree 后重新修复。
- `base_update_failed` 任务重试 Base 状态回写。
- `暂停` 任务立即开始。
- `暂停` 任务设为无需修复。

删除 worktree 时，系统会尽量删除当前任务对应的 worktree 和符合分支前缀的任务分支，并执行 `git worktree prune` 清理残留元数据。

## 本地数据与安全

本系统默认把运行数据保存在：

```text
Autofix/.feishu_echo/autofix.sqlite3
```

这个目录可能包含：

- 任务记录。
- 飞书消息内容。
- Base 字段快照。
- AI 输入和输出。
- worktree 路径。
- CLI 路径。
- 飞书回显结果和错误信息。

不要把 `.feishu_echo`、`.sqlite`、`.sqlite-wal`、`.sqlite-shm`、`.env`、真实 token、真实 API Key、真实群 ID、真实 Base ID 上传到公开仓库。

AI 配置中的 API Key 只保存环境变量名，不保存明文密钥。真正的密钥应通过系统环境变量或 CLI 自己的安全配置提供。

## 为什么是本地 CLI

自动修复代码天然依赖本地环境：

- 本地仓库和 Git 状态。
- 本地 worktree。
- 本地构建缓存和依赖。
- 用户已经登录的 AI CLI。
- 用户已经配置好的飞书 CLI。

如果做成远程服务，需要额外处理源码同步、凭据分发、远程执行隔离、网络稳定性、权限审计和构建环境一致性。当前版本选择本地 CLI，是为了减少这些额外复杂度，并尽量避免把代码和凭据上传到远端。

## 适合和不适合

适合：

- 有明确 Base 记录的重复 Bug 修复。
- 有明确状态流转和完成状态的 QA 修复流程。
- 有明确基准分支和独立 worktree 的项目。
- 能提供低成本验证命令的项目。
- 希望保留任务历史、AI 对话、diff、验证输出和飞书回显的团队。

不适合：

- 需求不清晰的大型功能开发。
- 需要产品决策的复杂业务改动。
- 大规模架构重构。
- 无法在本地构建或验证的项目。
- 需要自动合并、自动推送、自动发布的完整 CI/CD 流程。

## 开发检查

常用检查命令：

```bash
python3 -m py_compile Autofix/autoSetup.py Autofix/tools/*.py
git diff --check -- Autofix
python3 Autofix/autoSetup.py --dry-run --ai-cli none
```

## 许可说明

本项目使用 MIT License，详见仓库根目录的 `LICENSE` 文件。

MIT License 是一种宽松开源许可证。它允许任何人在保留版权声明和许可证文本的前提下，使用、复制、修改、合并、发布、分发、再授权和销售本软件副本。

需要注意：

- 软件按“现状”提供，不承诺适销性、特定用途适用性或不侵权。
- 作者和贡献者不对使用本软件产生的索赔、损害或其他责任负责。
- 许可证只覆盖本仓库代码本身，不自动覆盖你本地配置中的飞书数据、AI CLI 凭据、API Key、任务数据库或目标项目源码。
- 使用者仍需自行遵守飞书、GitHub、AI CLI、模型服务和目标项目的各自服务条款与许可证。
