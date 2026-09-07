"""Autofix 的通用工具函数。

这里放无状态、可复用的小能力：命令执行、CLI 配置解析、路径校验、git 辅助、
飞书 Base 状态更新、时间段判断和文本归一化。调用方应把业务状态保存在
store/executor 中，而不是藏在这些工具函数里。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import ast
from pathlib import Path
from typing import Any

from autofix_defaults import (
    AI_BRAIN_TYPES,
    AI_CONFIG_CLI_TYPES,
    COMMON_CLI_PATHS,
    DEFAULT_AUTO_REPAIR_END_DAY_OFFSET,
    DEFAULT_AUTO_REPAIR_END_TIME,
    DEFAULT_AUTO_REPAIR_MODE,
    DEFAULT_AUTO_REPAIR_START_TIME,
    DEFAULT_CODEX_BIN,
    DEFAULT_WORKTREE_SUFFIX,
)
from autofix_models import AIConfig, ProjectConfig


def run_checked(args: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """执行一个必须成功的子进程命令。

    命令以 argv 列表传入，不经过 shell。非 0 退出码会抛 CalledProcessError，
    stdout/stderr 会被捕获，方便上层写入任务错误或日志。
    """

    return subprocess.run(
        args,
        check=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_json(args: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> dict[str, Any]:
    """执行返回 JSON 的 CLI 命令，并校验 `ok` 字段。

    主要用于 lark-cli。返回值是解析后的 JSON 对象；解析失败或 ok=false 会抛
    RuntimeError，错误信息会尽量压缩成人能读懂的摘要。
    """

    result = run_checked(args, cwd=cwd, timeout=timeout)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid json from {' '.join(args)}: {exc}") from exc
    if not payload.get("ok", False):
        raise RuntimeError(summarize_lark_cli_error(json.dumps(payload, ensure_ascii=False)))
    return payload


def clean_subprocess_text(value: Any) -> str:
    """把 subprocess 异常里的 stdout/stderr 归一成可读 UTF-8 文本。

    Python 的 TimeoutExpired 即使在 text=True 时也可能携带 bytes；直接
    str(bytes) 会显示成 `b'\\xe4\\xbd...'` 这类转义。本函数统一解码并清理。
    """

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    text = str(value).strip()
    if len(text) >= 3 and text[:2] in {"b'", 'b"'} and text[-1] == text[1]:
        try:
            literal_value = ast.literal_eval(text)
            if isinstance(literal_value, bytes):
                return literal_value.decode("utf-8", errors="replace").strip()
        except Exception:
            pass
    if "\\x" in text:
        try:
            decoded = text.encode("utf-8").decode("unicode_escape")
            return decoded.encode("latin1").decode("utf-8", errors="replace").strip()
        except Exception:
            pass
    return text


def cli_type_label(cli_type: str) -> str:
    """把 CLI 类型枚举转换成页面展示名。"""

    labels = dict(AI_CONFIG_CLI_TYPES)
    return labels.get(cli_type, cli_type or "custom")


def ai_provider_label(provider: str) -> str:
    """把 AI Provider 枚举转换成页面展示名。"""

    labels = dict(AI_BRAIN_TYPES)
    return labels.get(provider, provider or "custom")


def default_ai_provider(cli_type: str) -> str:
    """根据 CLI 后端推断默认 AI 大脑类型。"""

    return {
        "claude": "claude",
        "gemini": "gemini",
        "deepseek": "deepseek",
        "qwen": "qwen",
        "codex": "gpt",
        "cursor-agent": "gpt",
    }.get(cli_type, "custom")


def default_config_home_env(cli_type: str) -> str:
    """返回隔离 CLI 私有配置目录时应设置的环境变量名。"""

    return {
        "codex": "CODEX_HOME",
    }.get(cli_type, "")


def default_ai_config_name(cli_type: str, model_name: str) -> str:
    """生成 AI 配置组默认名称。"""

    label = cli_type_label(cli_type)
    return f"{label} / {model_name}" if model_name else label


def split_command_line(command: str) -> list[str]:
    """按当前操作系统规则把命令行拆成 argv。

    macOS/Linux 使用 POSIX shell 规则；Windows 使用 CommandLineToArgvW，
    这样带空格的 `C:\\Program Files\\...` 和 Windows 风格引号能正确处理。
    """

    if os.name != "nt":
        return shlex.split(command)
    import ctypes

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(command, ctypes.byref(argc))
    if not argv:
        raise ValueError("Windows 命令行解析失败")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def join_command_for_display(args: list[str]) -> str:
    """把 argv 拼成一行仅用于展示的命令文本。"""

    if os.name != "nt":
        return " ".join(shlex.quote(part) for part in args)
    return subprocess.list2cmdline(args)


def discover_cli_path(cli_type: str) -> str:
    """在 PATH 和常见安装路径中寻找指定 CLI。

    返回可执行文件路径；找不到时返回空字符串，让页面允许用户手工填写。
    """

    command = cli_type.strip()
    path = shutil.which(command)
    if path:
        return path
    for candidate in COMMON_CLI_PATHS.get(cli_type, []):
        candidate_path = Path(candidate)
        if candidate_path.exists() and os.access(candidate_path, os.X_OK):
            return str(candidate_path)
    return ""


def resolve_cli_executable(ai_config: AIConfig) -> str:
    """解析 AIConfig 最终要执行的 CLI 路径。

    优先使用配置中的 cli_path；为空时自动搜索。路径不存在或不可执行会抛错，
    执行器据此把任务标为失败。
    """

    raw_path = ai_config.cli_path.strip() or discover_cli_path(ai_config.cli_type)
    if not raw_path:
        raise RuntimeError(f"未找到 CLI：{cli_type_label(ai_config.cli_type)}")
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        if not path.exists():
            raise RuntimeError(f"CLI 路径不存在：{path}")
        if not os.access(path, os.X_OK):
            raise RuntimeError(f"CLI 不可执行：{path}")
        return str(path)
    found = shutil.which(raw_path)
    if not found:
        raise RuntimeError(f"CLI 命令不可用：{raw_path}")
    return found


def build_ai_cli_env(ai_config: AIConfig) -> dict[str, str]:
    """构造只作用于 Autofix 子进程的环境变量。

    不修改用户全局 CLI 配置；如果配置了 API Key 环境变量名，只检查该变量
    是否存在，不保存或打印明文密钥。
    """

    env = os.environ.copy()
    if ai_config.api_key_env and ai_config.api_key_env not in env:
        raise RuntimeError(f"缺少 API Key 环境变量：{ai_config.api_key_env}")
    if ai_config.config_home:
        env_name = ai_config.config_home_env or default_config_home_env(ai_config.cli_type)
        if env_name:
            env[env_name] = str(Path(ai_config.config_home).expanduser())
    return env


def split_extra_args(extra_args: str) -> list[str]:
    """把 AI 配置中的额外 CLI 参数解析成 argv 列表。"""

    if not extra_args.strip():
        return []
    try:
        return split_command_line(extra_args)
    except ValueError as exc:
        raise RuntimeError(f"额外参数解析失败：{exc}") from exc


def format_custom_command_template(
    template: str,
    *,
    cli: str,
    model: str,
    worktree_path: Path,
    prompt: str,
    prompt_file: Path,
    output_file: Path,
) -> list[str]:
    """把自定义 CLI 命令模板渲染成 argv。

    模板可用 `{cli}`、`{model}`、`{worktree_path}`、`{prompt}`、
    `{prompt_file}`、`{output_file}`。返回值直接传给 subprocess。
    """

    if not template.strip():
        raise RuntimeError("自定义 CLI 需要填写命令模板")
    tokens = {
        "cli": "__AUTOFIX_CLI__",
        "model": "__AUTOFIX_MODEL__",
        "worktree_path": "__AUTOFIX_WORKTREE_PATH__",
        "prompt": "__AUTOFIX_PROMPT__",
        "prompt_file": "__AUTOFIX_PROMPT_FILE__",
        "output_file": "__AUTOFIX_OUTPUT_FILE__",
    }
    values = {
        "__AUTOFIX_CLI__": cli,
        "__AUTOFIX_MODEL__": model,
        "__AUTOFIX_WORKTREE_PATH__": str(worktree_path),
        "__AUTOFIX_PROMPT__": prompt,
        "__AUTOFIX_PROMPT_FILE__": str(prompt_file),
        "__AUTOFIX_OUTPUT_FILE__": str(output_file),
    }
    try:
        rendered = template.format(**tokens)
    except KeyError as exc:
        raise RuntimeError(f"自定义命令模板包含未知占位符：{exc}") from exc
    try:
        args = split_command_line(rendered)
    except ValueError as exc:
        raise RuntimeError(f"自定义命令模板解析失败：{exc}") from exc
    rendered_args = []
    for arg in args:
        for token, value in values.items():
            arg = arg.replace(token, value)
        rendered_args.append(arg)
    return rendered_args


def build_ai_cli_command(
    ai_config: AIConfig,
    *,
    prompt: str,
    worktree_path: Path,
    prompt_file: Path,
    output_file: Path,
) -> list[str]:
    """根据 AIConfig 生成一次自动修复的 CLI 命令。

    不同 CLI 的参数形式不同，这里把它们统一成 argv。model_name 为空时不传
    `--model`，让 CLI 使用自己的当前默认模型。
    """

    cli = resolve_cli_executable(ai_config)
    model = ai_config.model_name.strip()
    extra = split_extra_args(ai_config.extra_args)
    cli_type = ai_config.cli_type

    if cli_type == "custom":
        return format_custom_command_template(
            ai_config.command_template,
            cli=cli,
            model=model,
            worktree_path=worktree_path,
            prompt=prompt,
            prompt_file=prompt_file,
            output_file=output_file,
        )

    if cli_type == "codex":
        cmd = [
            cli,
            "exec",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(worktree_path),
            "--skip-git-repo-check",
            "--output-last-message",
            str(output_file),
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(extra)
        cmd.append(prompt)
        return cmd

    if cli_type == "claude":
        cmd = [cli, "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(extra)
        cmd.extend(["-p", prompt])
        return cmd

    if cli_type == "cursor-agent":
        cmd = [cli, "--force", "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(extra)
        cmd.extend(["-p", prompt])
        return cmd

    if cli_type == "opencode":
        cmd = [cli, "run"]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(extra)
        cmd.append(prompt)
        return cmd

    cmd = [cli]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(extra)
    cmd.extend(["-p", prompt])
    return cmd


def command_for_display(cmd: list[str]) -> str:
    """生成安全展示用命令行，最后的 Prompt 参数会被替换成 `<prompt>`。"""

    if not cmd:
        return ""
    return join_command_for_display([part if index != len(cmd) - 1 else "<prompt>" for index, part in enumerate(cmd)])


def read_cli_output(output_file: Path, stdout: str) -> str:
    """读取 AI CLI 的回复文本。

    优先读取 output_file；如果 CLI 没有写文件，则退回 stdout。多行会压缩成
    单行，便于任务摘要和飞书回显展示。
    """

    try:
        file_text = output_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        file_text = ""
    text = file_text or stdout.strip()
    if not text:
        return ""
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def self_check_ai_config(ai_config: AIConfig) -> tuple[str, str]:
    """自检一组 AI 配置是否具备“可编辑型 CLI”能力。

    函数会创建临时 git 仓库，让 CLI 修改一个小文件；只有文件内容真的变成
    `passed` 才认为通过。返回 `(状态, 说明)`，不抛给页面。
    """

    temp_root = Path(tempfile.mkdtemp(prefix="autofix-ai-check-"))
    prompt_path = temp_root / "prompt.md"
    output_path = temp_root / "last-message.md"
    target_path = temp_root / "autofix_self_check_result.txt"
    try:
        run_checked(["git", "init"], cwd=temp_root, timeout=30)
        target_path.write_text("pending\n", encoding="utf-8")
        prompt = textwrap.dedent(
            f"""
            你正在 Autofix 的临时自检目录中工作，只允许修改当前目录。
            请把文件 {target_path.name} 的内容改为 exactly: passed
            完成后用一句中文说明已完成。
            """
        ).strip()
        prompt_path.write_text(prompt, encoding="utf-8")
        env = build_ai_cli_env(ai_config)
        cmd = build_ai_cli_command(
            ai_config,
            prompt=prompt,
            worktree_path=temp_root,
            prompt_file=prompt_path,
            output_file=output_path,
        )
        result = subprocess.run(
            cmd,
            check=True,
            cwd=temp_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = read_cli_output(output_path, result.stdout or "")
        try:
            file_value = target_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            file_value = ""
        if file_value == "passed":
            return "passed", output or "CLI 可编辑临时文件"
        return "failed", f"CLI 未完成临时文件编辑，当前内容：{file_value or '空'}"
    except subprocess.TimeoutExpired:
        return "failed", "自检超时"
    except subprocess.CalledProcessError as exc:
        detail = clean_subprocess_text(exc.stderr or exc.stdout)
        return "failed", detail or f"自检命令退出码：{exc.returncode}"
    except Exception as exc:
        return "failed", str(exc)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def summarize_lark_cli_error(raw_message: str) -> str:
    """把 lark-cli 的 JSON 错误压缩成一行摘要。"""

    raw_message = raw_message.strip()
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        return raw_message

    error = payload.get("error")
    if not isinstance(error, dict):
        return json.dumps(payload, ensure_ascii=False)

    parts: list[str] = []
    for key in ("type", "subtype", "code", "message", "log_id", "hint"):
        value = error.get(key)
        if value:
            parts.append(f"{key}={value}")
    missing_scopes = error.get("missing_scopes")
    if missing_scopes:
        parts.append(f"missing_scopes={missing_scopes}")
    return "; ".join(parts) or json.dumps(error, ensure_ascii=False)


def sanitize_slug(value: str) -> str:
    """把任意文本转换成适合分支名/文件名片段的 slug。"""

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return slug or "task"


def normalize_path(path_value: str, *, base: Path | None = None) -> Path:
    """解析用户输入路径，支持 `~` 和相对 base 的路径。"""

    path = Path(path_value).expanduser()
    if not path.is_absolute() and base is not None:
        path = (base / path).resolve()
    return path.resolve()


def ensure_under(parent: Path, child: Path) -> None:
    """确保 child 位于 parent 目录内，否则抛 ValueError。"""

    try:
        child.relative_to(parent)
    except ValueError as exc:
        raise ValueError(f"{child} must be inside {parent}") from exc


def repo_root_name(repo_root: Path) -> str:
    """从仓库路径推断默认 WorkRoot 名称。"""

    return repo_root.name or "repo"


def current_git_root(start: Path) -> Path | None:
    """从指定目录向上查找当前 git 仓库根目录。"""

    try:
        result = run_checked(["git", "rev-parse", "--show-toplevel"], cwd=start)
    except Exception:
        return None
    root = result.stdout.strip()
    if not root:
        return None
    return Path(root).resolve()


def current_git_branch(repo_root: Path) -> str:
    """读取仓库当前分支名，失败时退回 master。"""

    try:
        result = run_checked(["git", "branch", "--show-current"], cwd=repo_root)
        branch = result.stdout.strip()
        if branch:
            return branch
    except Exception:
        pass

    try:
        result = run_checked(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo_root)
        branch = result.stdout.strip()
        if branch:
            return branch
    except Exception:
        pass
    return "master"


def resolve_codex_bin() -> str:
    """解析默认 Codex CLI 路径。

    顺序为 CODEX_BIN 环境变量、PATH 中的 codex，最后返回空字符串。
    空字符串表示后续运行时继续按 AI 配置的 CLI 类型自动搜索。
    """

    configured = os.environ.get("CODEX_BIN")
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            return str(path)
    from_path = shutil.which("codex")
    if from_path:
        return from_path
    return DEFAULT_CODEX_BIN


def log_task_status(task_id: int, record_id: str, status: str, message: str) -> None:
    """按统一格式输出任务状态日志到 stderr。"""

    print(f"[repair-task:{task_id}][record_id={record_id}] {status} {message}", file=sys.stderr, flush=True)


def log_task_multiline(task_id: int, record_id: str, label: str, text: str) -> None:
    """逐行输出 AI 回复或长文本日志，保持每行都带任务标识。"""

    lines = text.splitlines() or [""]
    for line in lines:
        print(f"[repair-task:{task_id}][record_id={record_id}] {label} {line}", file=sys.stderr, flush=True)


def build_commit_message(record_id: str, issue_summary: str) -> str:
    """根据 Base 记录 ID 和问题摘要生成 72 字符以内的提交信息。"""

    head = f"Base {record_id}"
    summary = " ".join(issue_summary.split())
    message = f"{head}: {summary}" if summary else head
    return message[:72]


class BaseStatusUpdateError(RuntimeError):
    """代码已提交但更新飞书 Base 状态失败。"""

    def __init__(self, commit_sha: str, message: str) -> None:
        """保存已生成的提交 SHA，供上层后续重试 Base 回写。"""
        super().__init__(f"code committed as {commit_sha}, but failed to update Base status: {message}")
        self.commit_sha = commit_sha


class NoCodeChangesError(RuntimeError):
    """AI CLI 结束后没有产生任何代码改动。"""

    pass


class CannotRepairError(RuntimeError):
    """当前任务被判定为无法自动修复。"""

    def __init__(self, ai_reply: str) -> None:
        """把 AI 的拒绝原因同时作为异常消息和可展示回复保存。"""
        message = ai_reply.strip() or "AI 未产生代码修改，按无法修复处理"
        super().__init__(message)
        self.ai_reply = message


def read_auth_ids() -> tuple[str, str]:
    """读取当前 lark-cli 登录态中的 app_id 和 bot_open_id。"""

    try:
        result = run_checked(["lark-cli", "auth", "status", "--json"])
        payload = json.loads(result.stdout)
    except Exception:
        return "", ""

    app_id = payload.get("appId")
    bot_open_id = payload.get("identities", {}).get("bot", {}).get("openId")
    return (
        app_id if isinstance(app_id, str) else "",
        bot_open_id if isinstance(bot_open_id, str) else "",
    )


def resolve_field_id(base_token: str, table_id: str, field_name: str) -> str:
    """根据字段名查询飞书 Base 字段 ID。"""

    if not field_name:
        raise RuntimeError("status field name is empty")

    payload = run_json(
        [
            "lark-cli",
            "base",
            "+field-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--as",
            "bot",
            "--json",
        ]
    )
    data = payload.get("data", {})
    fields = data.get("fields") or data.get("data") or []
    if not isinstance(fields, list):
        fields = []

    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name") or field.get("field_name") or field.get("title")
        field_id = field.get("field_id") or field.get("id") or field.get("fieldId")
        if isinstance(name, str) and isinstance(field_id, str) and name == field_name:
            return field_id

    raise RuntimeError(f"could not resolve field id for {field_name!r}")


def update_base_status(
    *,
    base_token: str,
    table_id: str,
    record_id: str,
    status_field_name: str,
    done_status_value: str,
) -> None:
    """把指定 Base 记录的状态字段更新为完成状态。"""

    field_id = resolve_field_id(base_token, table_id, status_field_name)
    payload = json.dumps({field_id: done_status_value}, ensure_ascii=False)
    run_json(
        [
            "lark-cli",
            "base",
            "+record-upsert",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--record-id",
            record_id,
            "--as",
            "bot",
            "--json",
            payload,
        ]
    )


def resolve_worktree_root(repo_root: Path, raw_value: str | None) -> Path:
    """解析 worktree 根目录，并强制它位于 repo_root 下。"""

    if raw_value:
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
    else:
        candidate = (repo_root / DEFAULT_WORKTREE_SUFFIX).resolve()
    ensure_under(repo_root.resolve(), candidate)
    return candidate


def parse_status_values(raw_value: str | list[str] | None) -> list[str]:
    """解析“可处理状态值”列表，支持换行或逗号分隔。"""

    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        values = raw_value
    else:
        normalized = raw_value.replace("\r", "\n").replace(",", "\n")
        values = normalized.split("\n")
    parsed: list[str] = []
    for value in values:
        item = value.strip()
        if item and item not in parsed:
            parsed.append(item)
    return parsed


def format_status_values(values: list[str]) -> str:
    """把状态值列表格式化成 textarea 友好的多行文本。"""

    return "\n".join(values)


def normalize_text_value(value: Any) -> str:
    """把飞书字段值、列表、对象等归一化成可比较的文本。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "name", "value", "title"):
            item = value.get(key)
            text = normalize_text_value(item)
            if text:
                return text
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, list):
        return ", ".join(text for text in (normalize_text_value(item) for item in value) if text).strip()
    return str(value).strip()


def normalize_base_name_value(value: Any) -> str:
    """归一化飞书消息中的多维表格名称。"""

    text = normalize_text_value(value).strip("<>＜＞ ")
    if not text:
        return ""
    markdown_match = re.search(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text)
    if markdown_match:
        return normalize_text_value(markdown_match.group(1)).strip("<>＜＞ ")
    prefix_match = re.search(r"来自\s*[:：]?\s*(.+)$", text)
    if prefix_match:
        return normalize_text_value(prefix_match.group(1)).strip("<>＜＞ ")
    return text


def parse_positive_int(raw_value: Any, default: int) -> int:
    """解析正整数，非法或小于 1 时使用 default/1 的安全值。"""

    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def parse_non_negative_int(raw_value: Any, default: int = 0) -> int:
    """解析非负整数，非法时使用 default。"""

    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        value = default
    return max(0, value)


def normalize_auto_repair_mode(raw_value: Any) -> str:
    """归一化自动修复时间段模式。"""

    value = str(raw_value or DEFAULT_AUTO_REPAIR_MODE).strip()
    if value in {"all_day", "disabled", "time_range"}:
        return value
    return DEFAULT_AUTO_REPAIR_MODE


def normalize_hhmm(raw_value: Any, default: str) -> str:
    """归一化 HH:MM 时间字符串。"""

    value = str(raw_value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not match:
        return default
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return default
    return f"{hour:02d}:{minute:02d}"


def hhmm_to_minutes(value: str) -> int:
    """把 HH:MM 转换为当天第几分钟。"""

    hour_text, minute_text = value.split(":", 1)
    return int(hour_text) * 60 + int(minute_text)


def parse_end_day_offset(raw_value: Any) -> int:
    """解析结束日期偏移，0 表示当日，1 表示次日。"""

    return 1 if parse_non_negative_int(raw_value, DEFAULT_AUTO_REPAIR_END_DAY_OFFSET) >= 1 else 0


def is_auto_repair_time(config: "ProjectConfig", *, now: float | None = None) -> bool:
    """判断当前时间是否处于项目配置允许自动修复的时间段。"""

    mode = normalize_auto_repair_mode(config.auto_repair_mode)
    if mode == "all_day":
        return True
    if mode == "disabled":
        return False

    current = time.localtime(now or time.time())
    current_minute = current.tm_hour * 60 + current.tm_min
    start_minute = hhmm_to_minutes(normalize_hhmm(config.auto_repair_start_time, DEFAULT_AUTO_REPAIR_START_TIME))
    end_minute = hhmm_to_minutes(normalize_hhmm(config.auto_repair_end_time, DEFAULT_AUTO_REPAIR_END_TIME))
    end_offset = parse_end_day_offset(config.auto_repair_end_day_offset)

    if end_offset == 0:
        if end_minute < start_minute:
            return False
        return start_minute <= current_minute <= end_minute

    # A daily cross-day window is active either after today's start or before today's carried-over end.
    return current_minute >= start_minute or current_minute <= end_minute


def describe_auto_repair_window(config: "ProjectConfig") -> str:
    """把自动修复时间段转换成页面可读文案。"""

    mode = normalize_auto_repair_mode(config.auto_repair_mode)
    if mode == "all_day":
        return "全天"
    if mode == "disabled":
        return "不自动修复"
    start_time = normalize_hhmm(config.auto_repair_start_time, DEFAULT_AUTO_REPAIR_START_TIME)
    end_time = normalize_hhmm(config.auto_repair_end_time, DEFAULT_AUTO_REPAIR_END_TIME)
    end_day = "次日" if parse_end_day_offset(config.auto_repair_end_day_offset) else "当日"
    return f"当日 {start_time} 至{end_day} {end_time}"
