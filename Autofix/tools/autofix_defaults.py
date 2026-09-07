"""Autofix 的默认值和内置 Prompt。

这个模块只放“配置默认值”和“可展示给用户修改的默认文案”，不放运行逻辑。
其他模块从这里读取常量，避免默认值散落在数据库、页面和执行器里。

调用示例：
    from autofix_defaults import DEFAULT_VERIFY_COMMANDS
"""

from __future__ import annotations

import textwrap
from pathlib import Path


APP_NAME = "Autofix Manager"
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / ".feishu_echo" / "autofix.sqlite3"
DEFAULT_PORT = 8000
APP_SCHEMA_VERSION = 7
DEFAULT_MAX_CONCURRENT = 2
DEFAULT_CODEX_BIN = ""
DEFAULT_DONE_STATUS = "AI修复完成"
DEFAULT_STATUS_FIELD = "状态"
DEFAULT_REPAIR_VERSION_FIELD = "修复版本"
DEFAULT_BRANCH_PREFIX = "fix/base-"
DEFAULT_PROCESSABLE_STATUSES = ["待 AI 修复", "重开"]
DEFAULT_WORKTREE_SUFFIX = ".worktrees"
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_AUTO_REPAIR_MODE = "all_day"
DEFAULT_AUTO_REPAIR_START_TIME = "00:00"
DEFAULT_AUTO_REPAIR_END_DAY_OFFSET = 0
DEFAULT_AUTO_REPAIR_END_TIME = "23:59"
DEFAULT_REPAIR_MAX_LOOPS = 3
MAX_REPAIR_MAX_LOOPS = 10
MAX_DIFF_PROMPT_CHARS = 60000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
MAX_COMMAND_OUTPUT_CHARS = 20000
MAX_CONTEXT_SNAPSHOT_CHARS = 60000
MAX_VERIFY_OUTPUT_CHARS = 60000
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
AI_CONFIG_CLI_TYPES = [
    ("codex", "Codex CLI"),
    ("claude", "Claude Code"),
    ("cursor-agent", "Cursor Agent"),
    ("gemini", "Gemini CLI"),
    ("deepseek", "DeepSeek CLI"),
    ("qwen", "Qwen CLI"),
    ("opencode", "OpenCode"),
    ("custom", "自定义 CLI"),
]
AI_BRAIN_TYPES = [
    ("gpt", "GPT"),
    ("claude", "Claude"),
    ("gemini", "Gemini"),
    ("deepseek", "DeepSeek"),
    ("glm", "GLM"),
    ("qwen", "Qwen"),
    ("custom-openai-compatible", "自定义 OpenAI-compatible"),
    ("custom", "自定义"),
]
COMMON_CLI_PATHS: dict[str, list[str]] = {}
DEFAULT_CONTEXT_COLLECT_COMMANDS = textwrap.dedent(
    """
    git status --short
    git log --oneline -20
    rg -n -- "{{issue_summary}}" .
    """
).strip()
DEFAULT_VERIFY_COMMANDS = textwrap.dedent(
    """
    git diff --check
    """
).strip()
DEFAULT_REPAIR_PROMPT_TEMPLATE = textwrap.dedent(
    """
    你正在执行结构化 Autofix Harness 的第 1 轮自动修复。请基于任务信息、原始消息、Base 字段和工具层收集的上下文，修复当前仓库里的这个 bug。

    硬性约束：

    - 不要创建分支，不要创建 worktree
    - 只允许修改 {{worktree_path}} 目录下的文件，不要修改原始仓库 checkout
    - 尽量保持改动最小
    - 如需新增或修改测试，只做能验证这次修复的最小集合
    - 完成后把需要的文件修改好，不要提交
    - 修改完成后必须进行必要的语法检查，尽量确保编译不会出错
    - 不要为了让任务结束而过早输出“修复状态：已完成”
    - 无论是否完成、是否修改、是否拒绝修复，都必须输出“修复思路”

    当前自动修复 Harness 会在你回复后执行以下检查：

    - 如果你输出“修复状态：已完成”，系统会先检查当前 worktree 是否存在 git diff
    - 如果没有任何 diff，系统会按无法修复处理
    - 如果存在 diff，系统会继续执行项目配置中的验证命令
    - 如果验证失败，系统会把当前 diff 和验证输出发送给下一轮二次 Prompt
    - 如果你没有输出“修复思路”，系统不会接受本轮成功，会继续下一轮要求补齐
    - 只有实际改动存在且你认为验证可以通过时，才输出“修复状态：已完成”

    上下文阅读顺序：

    1. 先阅读任务信息，确认 record_id、worktree_path、修复版本和 Base 摘要。
    2. 再阅读 Base 字段和飞书原始消息，确认用户真正描述的问题。
    3. 最后阅读相关上下文。上下文是工具层检索线索，不代表全部事实；必要时请继续查阅仓库文件。

    修复决策规则：

    1. 运行时异常 / 逻辑缺陷 / 局部显示缺陷：如果属于空指针、类型边界错误、状态判断遗漏、静态资源/样式调整等局部问题，请直接在当前 worktree 中进行最小化修复。
    2. 缺乏结构性功能 / 架构设计变动：如果评估发现该 Bug 实际上需要新增跨模块接口、重构数据模型或引入全新的结构性功能，请停止修改代码，在输出中明确说明该 Bug 无法直接修复及其原因，并附上推荐的实现计划。
    3. 配置与环境问题：如果问题取决于外部环境、无法在当前代码库中单独定位，请说明原因并提供排查建议。
    4. 存在较大的逻辑分支 / 需求不明确：如果分析后发现存在多种可能的修复路径且涉及业务逻辑决策（例如：方案 A 会改变既有行为，方案 B 会增加接口复杂度，或预期行为在文档/上下文中不明确），严禁私自进行假设或强行修复。请停止修改代码，在输出中列出所有可行的逻辑分支、各自的优缺点及影响范围，并要求人类进一步确认。

    任务信息如下：
    {{task_info}}

    原始信息如下：
    {{source_message}}

    Base 字段如下：
    {{base_fields}}

    相关上下文如下：
    {{context_snapshot}}

    执行要求：

    1. 先定位最可能相关的文件和代码路径。
    2. 修改前先判断是否属于可直接修复的问题。
    3. 如果可直接修复，进行最小代码修改。
    4. 修改后运行你认为必要且低成本的语法检查或局部验证。
    5. 如果你没有完成修改，或者还需要继续定位，请输出“修复状态：未完成”。

    输出格式要求：

    - 必须包含“修复思路：”，用简明技术语言说明判断依据、定位方向、采用或放弃的方案，方便用户判断是否应用本次修改。
    - 必须包含“本轮处理：”，说明本轮实际做了什么；如果没有修改，说明没有修改的原因。
    - 必须包含“验证情况：”，说明已运行或建议运行的语法检查、编译、测试。
    - 必须包含“风险说明：”，说明可能影响范围；无明显风险则写“未发现明显风险”。
    - 最后一行必须严格输出以下三种之一，不要添加其它文字：
      修复状态：已完成
      修复状态：未完成
      修复状态：无法修复

    推荐输出骨架：

    修复思路：...
    本轮处理：...
    验证情况：...
    风险说明：...
    修复状态：未完成
    """
).strip()
DEFAULT_REPAIR_FOLLOWUP_PROMPT_TEMPLATE = textwrap.dedent(
    """
    你正在执行结构化 Autofix Harness 的第 2 轮或后续轮次。上一轮没有让 Harness 确认成功，请基于上一轮回复、当前 diff/status 和工具层验证结果继续收敛修复。

    硬性约束：

    - 只允许修改 {{worktree_path}} 目录下的文件
    - 继续保持最小改动，只处理本任务相关问题
    - 优先修复下方工具层验证结果暴露的具体问题
    - 如果上一轮已经留下部分改动，请在这些改动基础上检查、补齐或收敛，不要无关重写
    - 不要回退上一轮中已经正确且必要的改动
    - 如果发现该问题需要人类确认、外部环境、跨模块架构调整或无法可靠完成，请停止继续改代码，并明确说明原因
    - 修改完成后必须进行必要的语法检查，尽量确保编译不会出错
    - 不要为了让任务结束而过早输出“修复状态：已完成”
    - 无论是否完成、是否修改、是否拒绝修复，都必须输出“修复思路”

    当前自动修复 Harness 会在你回复后再次执行：

    - git diff 检查：如果没有任何 diff，会按无法修复处理
    - 项目配置中的验证命令：如果失败，会把新的 diff 和验证输出继续反馈给下一轮
    - 如果你没有输出“修复思路”，系统不会接受本轮成功，会继续下一轮要求补齐
    - 只有 diff 存在且你认为验证可以通过时，才输出“修复状态：已完成”

    任务信息如下：
    {{task_info}}

    原始信息如下：
    {{source_message}}

    Base 字段如下：
    {{base_fields}}

    上一轮 AI 回复如下：
    {{previous_ai_reply}}

    当前 git diff / status 如下：
    {{current_diff}}

    上一轮工具层验证结果如下：
    {{verify_output}}

    本轮目标：

    1. 先根据当前 diff/status 判断上一轮留下的改动是否方向正确。
    2. 再根据工具层验证输出定位失败原因。
    3. 如果验证失败是语法错误，请优先修复语法。
    4. 如果验证失败是测试失败，请按失败用例做最小修复。
    5. 如果验证失败是无 diff、依赖缺失、环境不可用、命令超时或需求不明确，请在修复思路中说明原因，不要盲目扩大修改。
    6. 如果仍未完成或还需要继续定位，请输出“修复状态：未完成”。

    输出格式要求：

    - 必须包含“修复思路：”，说明本轮如何利用上一轮回复、当前 diff 和验证结果继续收敛。
    - 必须包含“本轮处理：”，说明本轮实际做了什么；如果没有修改，说明没有修改的原因。
    - 必须包含“验证情况：”，说明已运行或建议运行的语法检查、编译、测试。
    - 必须包含“风险说明：”，说明可能影响范围；无明显风险则写“未发现明显风险”。
    - 最后一行必须严格输出以下三种之一，不要添加其它文字：
      修复状态：已完成
      修复状态：未完成
      修复状态：无法修复

    推荐输出骨架：

    修复思路：...
    本轮处理：...
    验证情况：...
    风险说明：...
    修复状态：未完成
    """
).strip()
