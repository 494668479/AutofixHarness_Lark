"""任务记录、Base 行和 worktree 清理相关的小操作。

这些函数不启动 AI，也不更新任务状态；它们只负责从已有数据中提取业务字段，
或在用户确认后清理某个任务对应的 worktree/分支。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from feishu_echo_listener import flatten_strings, parse_maybe_json

from autofix_defaults import DEFAULT_BRANCH_PREFIX, DEFAULT_REPAIR_VERSION_FIELD
from autofix_models import BaseRow, TaskRow
from autofix_utils import (
    current_git_branch,
    ensure_under,
    normalize_base_name_value,
    normalize_text_value,
    run_checked,
)


def extract_task_payload(task: TaskRow) -> dict[str, Any]:
    """把任务中持久化的 payload_json 还原为字典。

    调用方可以安全使用返回值；如果 JSON 损坏或不是对象，会返回空字典。
    """

    try:
        payload = json.loads(task.payload_json)
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def extract_task_problem_description(task: TaskRow) -> str:
    """提取任务历史列表中展示的“问题描述”。

    优先读取 Base 字段中的“问题模块/描述、问题模块、描述”，然后退回到
    task.problem_description，最后才使用 summary。
    """

    payload = extract_task_payload(task)
    fields = payload.get("fields") if isinstance(payload, dict) else {}
    if isinstance(fields, dict):
        for key in ("问题模块/描述", "问题模块", "描述"):
            value = normalize_text_value(fields.get(key))
            if value:
                return value
    value = normalize_text_value(task.problem_description)
    if value:
        return value
    return normalize_text_value(task.summary)


def extract_task_status_value(task: TaskRow) -> str:
    """提取任务创建时对应 Base 行的状态值。"""

    if task.status_value.strip():
        return task.status_value.strip()
    payload = extract_task_payload(task)
    fields = payload.get("fields") if isinstance(payload, dict) else {}
    if isinstance(fields, dict):
        value = fields.get(task.status_field_name)
        return normalize_text_value(value)
    return ""


def extract_row_problem_description(row: BaseRow) -> str:
    """从 Base 行字段中提取问题描述。"""

    for key in ("问题模块/描述", "问题模块", "描述"):
        value = normalize_text_value(row.fields.get(key))
        if value:
            return value
    return ""


def extract_row_status_value(row: BaseRow, status_field_name: str) -> str:
    """按项目配置的状态字段名读取 Base 行状态。"""

    return normalize_text_value(row.fields.get(status_field_name))


def extract_row_repair_version(row: BaseRow) -> str:
    """读取 Base 行中的修复版本字段。"""

    return normalize_text_value(row.fields.get(DEFAULT_REPAIR_VERSION_FIELD))


def extract_source_base_name(content: str) -> str:
    """从飞书消息内容中提取“来自 <多维表格名称>”。

    消息可能是 JSON 卡片、纯文本或混合内容；函数会把字符串拍平后寻找
    `来自 自动化修bug测试` / `<来自 自动化修bug测试>` 等形式。
    """

    parsed = parse_maybe_json(content)
    strings = flatten_strings(parsed)
    if not strings:
        strings = [content]

    for index, text in enumerate(strings):
        stripped = normalize_text_value(text).strip("<>＜＞ ")
        if not stripped:
            continue
        bracket_match = re.search(r"[<＜]\s*来自\s+([^>＞\r\n]+?)\s*[>＞]", text)
        if bracket_match:
            return normalize_base_name_value(bracket_match.group(1))
        prefix_match = re.match(r"^来自\s*[:：]?\s*(.+)$", stripped)
        if prefix_match:
            return normalize_base_name_value(prefix_match.group(1))
        inline_match = re.search(r"来自\s*[:：]?\s*([^<>\r\n＞]+)", text)
        if inline_match:
            return normalize_base_name_value(inline_match.group(1))
        if stripped == "来自":
            for candidate in strings[index + 1 :]:
                candidate_text = normalize_text_value(candidate).strip("<>＜＞ ")
                if candidate_text:
                    return normalize_base_name_value(candidate_text)
    return ""


def _git_branch_exists(repo_root: Path, branch_name: str) -> bool:
    """判断指定本地分支是否存在。"""

    if not branch_name:
        return False
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_root,
    )
    return result.returncode == 0


def _branch_is_checked_out_elsewhere(repo_root: Path, branch_name: str, *, exclude_path: Path | None = None) -> str | None:
    """检查分支是否仍被其他 worktree 检出。

    删除分支前必须先确认它没有在别的工作区使用；否则 git 会拒绝删除，
    更重要的是避免误删用户正在使用的分支。
    """

    if not branch_name or not repo_root.exists():
        return None
    try:
        result = run_checked(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    except Exception:
        return None
    current_path: Path | None = None
    current_branch = ""
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.split(" ", 1)[1].strip()).expanduser().resolve()
            current_branch = ""
            continue
        if line.startswith("branch "):
            current_branch = line.split(" ", 1)[1].strip()
            continue
        if line.strip():
            continue
        if current_path is None:
            continue
        if current_branch == f"refs/heads/{branch_name}" and (exclude_path is None or current_path != exclude_path.resolve()):
            return str(current_path)
    if current_path is not None and current_branch == f"refs/heads/{branch_name}" and (exclude_path is None or current_path != exclude_path.resolve()):
        return str(current_path)
    return None


def _count_task_group_tasks(store: AutofixStore, task: TaskRow) -> int:
    """统计同一 repo/worktree 分组下还有多少任务记录。

    当前删除策略需要知道一个 worktree 根目录下是否只剩一个任务分支；
    这个计数帮助决定删除单个分支还是整个 worktree 目录。
    """

    target_root = Path(task.worktree_path).expanduser().resolve().parent
    with store._lock:
        rows = store._conn.execute(
            "SELECT worktree_path FROM repair_tasks WHERE repo_root = ?",
            (task.repo_root,),
        ).fetchall()
    count = 0
    for row in rows:
        other_path = Path(str(row["worktree_path"])).expanduser().resolve()
        if other_path.parent == target_root:
            count += 1
    return count


def delete_task_worktree(store: AutofixStore, task: TaskRow) -> None:
    """删除任务对应的 worktree，并清理可删除的任务分支。

    安全边界:
        - worktree_path 必须位于 repo_root 下。
        - 永远拒绝删除 repo_root 本身。
        - 只删除符合任务分支前缀的分支。
        - 删除后执行 `git worktree prune` 清理 Git 残留元数据。
    """

    raw_path = task.worktree_path.strip()
    if not raw_path:
        return
    worktree_path = Path(raw_path).expanduser().resolve()
    repo_root = Path(task.repo_root).expanduser().resolve()
    ensure_under(repo_root, worktree_path)
    if worktree_path == repo_root:
        raise RuntimeError("refusing to delete REPO_ROOT")
    branch_name = task.branch_name.strip()
    base_branch = task.base_branch.strip() or "master"
    branch_prefix = (task.branch_prefix or DEFAULT_BRANCH_PREFIX).strip() or DEFAULT_BRANCH_PREFIX
    removable_branch = bool(branch_name) and branch_name.startswith(branch_prefix)
    current_branch = current_git_branch(repo_root) if repo_root.exists() else ""

    if repo_root.exists() and worktree_path.exists():
        try:
            run_checked(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo_root)
        except Exception:
            pass
    if worktree_path.exists():
        shutil.rmtree(worktree_path)
    if worktree_path.exists():
        raise RuntimeError(f"failed to delete worktree path: {worktree_path}")

    if removable_branch and _git_branch_exists(repo_root, branch_name):
        occupied_path = _branch_is_checked_out_elsewhere(repo_root, branch_name)
        if occupied_path:
            raise RuntimeError(f"该分支已检出到 {occupied_path}，请先在那个工作区切换到其他分支后再删除")
        if current_branch == branch_name:
            run_checked(["git", "checkout", base_branch], cwd=repo_root)
        run_checked(["git", "branch", "-D", branch_name], cwd=repo_root)

    try:
        if repo_root.exists():
            run_checked(["git", "worktree", "prune"], cwd=repo_root)
    except Exception:
        pass
