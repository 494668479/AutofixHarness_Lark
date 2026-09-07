"""项目配置表单的默认值推断与合并。

这里负责把当前机器环境、当前 git 仓库和已有 ProjectConfig 组合成页面
可直接渲染的表单字典。它不保存数据，保存动作由 AutofixStore 负责。
"""

from __future__ import annotations

import os
from pathlib import Path

from autofix_defaults import (
    DEFAULT_AUTO_REPAIR_END_DAY_OFFSET,
    DEFAULT_AUTO_REPAIR_END_TIME,
    DEFAULT_AUTO_REPAIR_MODE,
    DEFAULT_AUTO_REPAIR_START_TIME,
    DEFAULT_BRANCH_PREFIX,
    DEFAULT_CONTEXT_COLLECT_COMMANDS,
    DEFAULT_DONE_STATUS,
    DEFAULT_PROCESSABLE_STATUSES,
    DEFAULT_REPAIR_VERSION_FIELD,
    DEFAULT_STATUS_FIELD,
    DEFAULT_VERIFY_COMMANDS,
)
from autofix_models import ProjectConfig
from autofix_utils import (
    current_git_branch,
    current_git_root,
    format_status_values,
    read_auth_ids,
    repo_root_name,
    resolve_worktree_root,
)


def infer_defaults(base_dir: Path) -> dict[str, str]:
    """根据当前目录和环境变量推断新建项目配置的默认值。

    典型调用场景是打开“新建项目配置”页面：系统会自动填充 repo_root、
    当前分支、worktree_root、飞书应用 ID、默认上下文命令和验证命令。
    """

    repo_root = current_git_root(base_dir) or base_dir.resolve()
    worktree_root = resolve_worktree_root(repo_root, None)
    app_name, _ = read_auth_ids()
    current_branch = current_git_branch(repo_root)
    return {
        "chat_name": "",
        "chat_id": os.environ.get("FEISHU_CHAT_ID", ""),
        "app_name": app_name or os.environ.get("FEISHU_SELF_APP_ID", ""),
        "base_name": os.environ.get("BASE_NAME", ""),
        "status_field_name": os.environ.get("BASE_STATUS_FIELD_NAME", DEFAULT_STATUS_FIELD),
        "done_status_value": os.environ.get("BASE_DONE_STATUS", DEFAULT_DONE_STATUS),
        "repo_root": str(repo_root),
        "base_branch": os.environ.get("REPAIR_BASE_BRANCH", current_branch),
        "branch_prefix": os.environ.get("REPAIR_BRANCH_PREFIX", DEFAULT_BRANCH_PREFIX),
        "worktree_root": str(worktree_root),
        "workroot_name": os.environ.get("WORKROOT_NAME", repo_root_name(repo_root)),
        "processable_status_values": format_status_values(DEFAULT_PROCESSABLE_STATUSES),
        "repair_version": os.environ.get("REPAIR_VERSION", ""),
        "auto_repair_mode": DEFAULT_AUTO_REPAIR_MODE,
        "auto_repair_start_time": DEFAULT_AUTO_REPAIR_START_TIME,
        "auto_repair_end_day_offset": str(DEFAULT_AUTO_REPAIR_END_DAY_OFFSET),
        "auto_repair_end_time": DEFAULT_AUTO_REPAIR_END_TIME,
        "context_collect_commands": DEFAULT_CONTEXT_COLLECT_COMMANDS,
        "verify_commands": DEFAULT_VERIFY_COMMANDS,
    }


def build_form_config(config: ProjectConfig | None = None, *, defaults: dict[str, str] | None = None) -> dict[str, str]:
    """生成配置编辑页使用的表单值。

    如果传入 config，则以已有配置覆盖默认值；如果 config 为 None，则返回
    新建项目配置默认值。返回值全部是字符串，方便 HTML 模板直接 escape 后使用。
    """

    data = dict(defaults or {})
    if config is not None:
        data.update(
            {
                "ai_config_id": str(config.ai_config_id),
                "chat_name": config.chat_name,
                "chat_id": config.chat_id,
                "app_name": config.app_name,
                "base_name": config.base_name,
                "status_field_name": config.status_field_name,
                "done_status_value": config.done_status_value,
                "repo_root": config.repo_root,
                "base_branch": config.base_branch,
                "branch_prefix": config.branch_prefix,
                "worktree_root": config.worktree_root,
                "workroot_name": config.workroot_name,
                "processable_status_values": format_status_values(config.processable_status_values),
                "repair_version": config.repair_version,
                "auto_repair_mode": config.auto_repair_mode,
                "auto_repair_start_time": config.auto_repair_start_time,
                "auto_repair_end_day_offset": str(config.auto_repair_end_day_offset),
                "auto_repair_end_time": config.auto_repair_end_time,
                "context_collect_commands": config.context_collect_commands,
                "verify_commands": config.verify_commands,
            }
        )
    return data
