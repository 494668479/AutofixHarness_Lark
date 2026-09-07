"""自动修复 Harness：Prompt 渲染、轮次判定、diff 和命令验证。

心理模型：
    1. 第 1 轮把任务信息和 context_snapshot 渲染进首次 Prompt。
    2. AI 回复后解析最后一行的“修复状态”。
    3. 若 AI 声明完成，工具层检查 git diff 并运行 verify_commands。
    4. 验证失败时，把 current_diff 和 verify_output 注入二次 Prompt 继续循环。

本模块不负责创建 worktree、提交代码或更新飞书 Base；这些由执行器负责。
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from autofix_defaults import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_REPAIR_FOLLOWUP_PROMPT_TEMPLATE,
    DEFAULT_REPAIR_PROMPT_TEMPLATE,
    MAX_COMMAND_OUTPUT_CHARS,
    MAX_DIFF_PROMPT_CHARS,
)
from autofix_models import TaskRequest, TaskRow
from autofix_utils import clean_subprocess_text, join_command_for_display, normalize_text_value, run_checked, split_command_line


def build_repair_prompt(
    task: TaskRow,
    request: TaskRequest,
    worktree_path: Path,
    prompt_template: str,
    *,
    context_snapshot: str = "",
) -> str:
    """构造第 1 轮修复 Prompt。

    参数:
        task: 数据库中的任务快照，提供分支、worktree、版本等执行信息。
        request: 飞书消息和 Base 行解析后的原始任务输入。
        worktree_path: 已创建好的任务工作区。
        prompt_template: 用户配置的首次 Prompt；为空时使用默认模板。
        context_snapshot: 上下文收集命令的输出，会填入 {{context_snapshot}}。

    返回:
        已替换占位符、可直接传给 AI CLI 的 Prompt 文本。
    """

    fields_json = json.dumps(request.fields, ensure_ascii=False, indent=2, sort_keys=True)
    task_info = textwrap.dedent(
        f"""
        - config_id: {task.config_id}
        - workroot_name: {task.workroot_name}
        - record_id: {task.record_id}
        - branch_name: {task.branch_name}
        - worktree_path: {worktree_path}
        - 修复版本: {task.repair_version}
        - Base 摘要: {request.issue_summary}
        """
    ).strip()
    values = {
        "task_info": task_info,
        "source_message": request.source_message,
        "base_fields": fields_json,
        "config_id": str(task.config_id),
        "workroot_name": task.workroot_name,
        "record_id": task.record_id,
        "branch_name": task.branch_name,
        "worktree_path": str(worktree_path),
        "repair_version": task.repair_version,
        "issue_summary": request.issue_summary,
        "context_snapshot": context_snapshot,
    }
    return render_prompt_template(
        prompt_template or DEFAULT_REPAIR_PROMPT_TEMPLATE,
        values,
        task_info=task_info,
        source_message=request.source_message,
        base_fields=fields_json,
        context_snapshot=context_snapshot,
    )


def build_followup_repair_prompt(
    task: TaskRow,
    request: TaskRequest,
    worktree_path: Path,
    prompt_template: str,
    *,
    previous_ai_reply: str,
    current_diff: str,
    verify_output: str,
    first_prompt: str,
) -> str:
    """构造第 2 轮及之后的收敛 Prompt。

    二次 Prompt 的重点不是重新理解任务，而是利用上一轮 AI 回复、当前 diff
    和工具层验证输出继续修复。调用方应在验证失败或 AI 表示未完成时使用它。
    """

    fields_json = json.dumps(request.fields, ensure_ascii=False, indent=2, sort_keys=True)
    task_info = textwrap.dedent(
        f"""
        - config_id: {task.config_id}
        - workroot_name: {task.workroot_name}
        - record_id: {task.record_id}
        - branch_name: {task.branch_name}
        - worktree_path: {worktree_path}
        - 修复版本: {task.repair_version}
        - Base 摘要: {request.issue_summary}
        """
    ).strip()
    values = {
        "task_info": task_info,
        "source_message": request.source_message,
        "base_fields": fields_json,
        "config_id": str(task.config_id),
        "workroot_name": task.workroot_name,
        "record_id": task.record_id,
        "branch_name": task.branch_name,
        "worktree_path": str(worktree_path),
        "repair_version": task.repair_version,
        "issue_summary": request.issue_summary,
        "previous_ai_reply": previous_ai_reply,
        "current_diff": current_diff,
        "verify_output": verify_output,
        "first_prompt": first_prompt,
    }
    return render_prompt_template(
        prompt_template or DEFAULT_REPAIR_FOLLOWUP_PROMPT_TEMPLATE,
        values,
        task_info=task_info,
        source_message=request.source_message,
        base_fields=fields_json,
        previous_ai_reply=previous_ai_reply,
        current_diff=current_diff,
        verify_output=verify_output,
        first_prompt=first_prompt,
    )


def render_prompt_template(template_text: str, values: dict[str, str], **fallbacks: str) -> str:
    """替换 Prompt 模板中的 `{{name}}` 占位符。

    如果模板显式使用了任意占位符，则只做替换并返回；如果完全没有使用占位符，
    会把 task_info、source_message、base_fields 等 fallback 自动追加到末尾，
    避免用户自定义 Prompt 时忘记带入任务上下文。
    """

    rendered = template_text.strip()
    used_placeholder = False
    all_values = dict(values)
    all_values.update(fallbacks)
    for key, value in all_values.items():
        placeholder = "{{" + key + "}}"
        if placeholder in rendered:
            rendered = rendered.replace(placeholder, value)
            used_placeholder = True
    if used_placeholder:
        return rendered.strip()

    fallback_lines = []
    if "task_info" in fallbacks:
        fallback_lines.append("自动填充任务信息：\n" + fallbacks["task_info"])
    if "source_message" in fallbacks:
        fallback_lines.append("原始信息：\n" + fallbacks["source_message"])
    if "base_fields" in fallbacks:
        fallback_lines.append("Base 行字段：\n" + fallbacks["base_fields"])
    if "context_snapshot" in fallbacks:
        fallback_lines.append("相关上下文：\n" + fallbacks["context_snapshot"])
    if "previous_ai_reply" in fallbacks:
        fallback_lines.append("上一轮 AI 回复：\n" + fallbacks["previous_ai_reply"])
    if "current_diff" in fallbacks:
        fallback_lines.append("当前 git diff / status：\n" + fallbacks["current_diff"])
    if "verify_output" in fallbacks:
        fallback_lines.append("工具层验证结果：\n" + fallbacks["verify_output"])
    if "first_prompt" in fallbacks:
        fallback_lines.append("首轮 Prompt：\n" + fallbacks["first_prompt"])
    if fallback_lines:
        return textwrap.dedent(
            f"""
            {rendered}

            {textwrap.dedent(chr(10).join(fallback_lines)).strip()}
            """
        ).strip()
    return rendered


def build_repair_prompt_round(
    task: TaskRow,
    request: TaskRequest,
    worktree_path: Path,
    prompt_template: str,
    *,
    round_index: int,
    first_prompt: str,
    first_ai_reply: str,
    previous_ai_reply: str,
    context_snapshot: str,
    verify_output: str,
) -> str:
    """按轮次选择首次 Prompt 或二次 Prompt。

    round_index 为 1 时调用 build_repair_prompt；从第 2 轮开始会先读取当前
    worktree diff，再调用 build_followup_repair_prompt 注入反馈信息。
    """

    current_diff = collect_worktree_diff(worktree_path)
    if round_index <= 1:
        return build_repair_prompt(task, request, worktree_path, prompt_template, context_snapshot=context_snapshot)
    return build_followup_repair_prompt(
        task,
        request,
        worktree_path,
        prompt_template,
        previous_ai_reply=previous_ai_reply or first_ai_reply,
        current_diff=current_diff,
        verify_output=verify_output,
        first_prompt=first_prompt,
    )


def collect_worktree_diff(worktree_path: Path, *, max_chars: int = MAX_DIFF_PROMPT_CHARS) -> str:
    """读取当前 worktree 的状态和未提交 diff。

    返回值会进入二次 Prompt，因此会截断到 max_chars；如果没有任何未提交改动，
    返回固定文案“当前没有未提交改动。”，执行器据此判断无 diff。
    """

    try:
        status_result = run_checked(["git", "status", "--short"], cwd=worktree_path)
        diff_result = run_checked(["git", "diff", "--no-ext-diff", "--no-color"], cwd=worktree_path)
        status_text = status_result.stdout.strip()
        diff_text = diff_result.stdout.strip()
        if not status_text and not diff_text:
            return "当前没有未提交改动。"
        sections = []
        if status_text:
            sections.append("git status --short:\n" + status_text)
        if diff_text:
            sections.append("git diff:\n" + diff_text)
        combined = "\n\n".join(sections).strip()
        if len(combined) > max_chars:
            return combined[:max_chars] + "\n\n[diff truncated]"
        return combined
    except Exception as exc:
        return f"无法读取当前 diff：{exc}"


def render_command_arg(arg: str, values: dict[str, str]) -> str:
    """在单个 argv 参数中替换命令占位符。

    占位符替换发生在 shlex 拆分之后，避免 issue_summary 里的空格或引号改变
    命令结构。
    """

    rendered = arg
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def render_command_args(command: str, values: dict[str, str]) -> list[str]:
    """把一行配置命令转换成安全的 argv 列表。

    示例:
        `rg -n -- "{{issue_summary}}" .` 会先按当前系统命令行规则拆成参数，再把
        `{{issue_summary}}` 替换成真实问题描述。
    """

    return [render_command_arg(arg, values) for arg in split_command_line(command)]


def split_command_lines(commands: str) -> list[str]:
    """把 textarea 中的多行命令解析成待执行命令列表。

    空行和以 `#` 开头的注释行会被忽略；每一行代表一个独立子进程。
    """

    return [line.strip() for line in commands.replace("\r", "\n").splitlines() if line.strip() and not line.strip().startswith("#")]


def run_task_commands(
    commands: str,
    *,
    cwd: Path,
    values: dict[str, str],
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    max_chars: int = MAX_COMMAND_OUTPUT_CHARS,
) -> tuple[bool, str]:
    """执行上下文收集命令或验证命令。

    命令统一在当前任务 worktree 下运行，不经过 shell，因此不支持管道、重定向
    等 shell 语法。返回 `(all_ok, output)`：只要任一命令非 0、超时或异常，
    all_ok 就是 False，output 会包含每条命令的 exit_code/stdout/stderr 摘要。
    """

    lines = split_command_lines(commands)
    if not lines:
        return True, "未配置命令。"
    outputs: list[str] = []
    all_ok = True
    for index, raw_command in enumerate(lines, start=1):
        if "{{issue_summary}}" in raw_command and not values.get("issue_summary", "").strip():
            continue
        section_lines = [f"$ {raw_command}"]
        try:
            args = render_command_args(raw_command, values)
            if not args:
                continue
            section_lines = [f"$ {join_command_for_display(args)}"]
            result = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=max(1, timeout_seconds),
            )
            section_lines.append(f"exit_code: {result.returncode}")
            if result.stdout.strip():
                section_lines.append("stdout:\n" + result.stdout.strip())
            if result.stderr.strip():
                section_lines.append("stderr:\n" + result.stderr.strip())
            if result.returncode != 0:
                all_ok = False
        except subprocess.TimeoutExpired as exc:
            all_ok = False
            section_lines.append(f"timeout: {timeout_seconds}s")
            stdout_text = clean_subprocess_text(exc.stdout)
            stderr_text = clean_subprocess_text(exc.stderr)
            if stdout_text:
                section_lines.append("stdout:\n" + stdout_text)
            if stderr_text:
                section_lines.append("stderr:\n" + stderr_text)
        except Exception as exc:
            all_ok = False
            section_lines.append(f"error: {exc}")
        section = "\n".join(section_lines)
        if len(section) > max_chars:
            section = section[:max_chars] + "\n[command output truncated]"
        outputs.append(f"命令 {index}/{len(lines)}\n{section}")
    combined = "\n\n".join(outputs).strip() or "未产生命令输出。"
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n[commands output truncated]"
    return all_ok, combined


def build_task_command_values(task: TaskRow, request: TaskRequest, worktree_path: Path) -> dict[str, str]:
    """生成上下文/验证命令可用的占位符字典。"""

    return {
        "config_id": str(task.config_id),
        "workroot_name": task.workroot_name,
        "record_id": task.record_id,
        "branch_name": task.branch_name,
        "worktree_path": str(worktree_path),
        "repair_version": task.repair_version,
        "issue_summary": request.issue_summary,
    }


def parse_repair_reply_status(reply_text: str) -> str:
    """从 AI 回复中解析本轮修复决策。

    只读取最后一条非空行中的严格格式：`修复状态：已完成/未完成/无法修复`。
    没有严格遵守格式时返回“未完成”，让 harness 继续下一轮而不是误判成功。
    """

    text = normalize_text_value(reply_text)
    if not text:
        return "未完成"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "未完成"
    matched = re.fullmatch(r"修复状态\s*[:：]\s*(已完成|未完成|无法修复)", lines[-1])
    if matched:
        return matched.group(1)
    return "未完成"


def extract_labeled_reply_section(reply_text: str, label: str) -> str:
    """从 AI 回复中提取 `标签：内容` 结构。

    支持单行值和多行值。提取会在遇到下一段已知输出标签时停止，适配默认
    Prompt 要求的“修复思路/本轮处理/验证情况/风险说明/修复状态”格式。
    """

    text = normalize_text_value(reply_text)
    if not text:
        return ""
    known_labels = ("修复思路", "本轮处理", "验证情况", "风险说明", "修复状态")
    start_pattern = re.compile(rf"^\s*{re.escape(label)}\s*[:：]\s*(.*)\s*$")
    stop_pattern = re.compile(rf"^\s*(?:{'|'.join(re.escape(item) for item in known_labels)})\s*[:：]")
    lines = text.splitlines()
    captured: list[str] = []
    capturing = False
    for line in lines:
        if not capturing:
            matched = start_pattern.match(line)
            if matched:
                captured.append(matched.group(1).strip())
                capturing = True
            continue
        if stop_pattern.match(line):
            break
        captured.append(line.rstrip())
    return "\n".join(line for line in captured if line.strip()).strip()


def parse_repair_reply_summary(reply_text: str) -> dict[str, str]:
    """解析 AI 回复中的结构化摘要字段。

    返回键固定为 repair_thinking_summary、round_action_summary、
    round_validation_summary、round_risk_summary。缺失字段返回空字符串。
    """

    return {
        "repair_thinking_summary": extract_labeled_reply_section(reply_text, "修复思路"),
        "round_action_summary": extract_labeled_reply_section(reply_text, "本轮处理"),
        "round_validation_summary": extract_labeled_reply_section(reply_text, "验证情况"),
        "round_risk_summary": extract_labeled_reply_section(reply_text, "风险说明"),
    }


def summarize_repair_rounds(rounds: list[dict[str, Any]]) -> str:
    """把轮次 JSON 转成人类可读的任务详情文本。"""

    if not rounds:
        return ""
    lines: list[str] = []
    for index, round_data in enumerate(rounds, start=1):
        decision = normalize_text_value(round_data.get("decision")) or "未完成"
        reply = normalize_text_value(round_data.get("ai_reply_text"))
        prompt = normalize_text_value(round_data.get("prompt_text"))
        verify_output = normalize_text_value(round_data.get("verify_output"))
        repair_thinking = normalize_text_value(round_data.get("repair_thinking_summary"))
        round_action = normalize_text_value(round_data.get("round_action_summary"))
        round_validation = normalize_text_value(round_data.get("round_validation_summary"))
        round_risk = normalize_text_value(round_data.get("round_risk_summary"))
        lines.append(f"第 {index} 轮")
        if repair_thinking:
            lines.append("修复思路:")
            lines.extend(f"  {line}" for line in repair_thinking.splitlines())
        if round_action:
            lines.append("本轮处理:")
            lines.extend(f"  {line}" for line in round_action.splitlines())
        if round_validation:
            lines.append("验证情况:")
            lines.extend(f"  {line}" for line in round_validation.splitlines())
        if round_risk:
            lines.append("风险说明:")
            lines.extend(f"  {line}" for line in round_risk.splitlines())
        if prompt:
            lines.append("Prompt:")
            lines.extend(f"  {line}" for line in prompt.splitlines())
        if reply:
            lines.append("回复:")
            lines.extend(f"  {line}" for line in reply.splitlines())
        if verify_output:
            verify_label = "验证通过" if round_data.get("verify_ok") else "验证失败"
            lines.append(f"{verify_label}:")
            lines.extend(f"  {line}" for line in verify_output.splitlines())
        lines.append(f"判定: {decision}")
        if index != len(rounds):
            lines.append("")
    return "\n".join(lines).strip()


def parse_repair_rounds_json(raw_value: str) -> list[dict[str, Any]]:
    """安全解析 round_history_json。

    损坏或非列表 JSON 会返回空列表，保证页面展示不会因为单条历史异常而崩溃。
    """

    try:
        payload = json.loads(raw_value)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    rounds: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            rounds.append(item)
    return rounds
