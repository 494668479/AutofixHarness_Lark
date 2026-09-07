"""Autofix 的核心数据模型。

这些 dataclass 是模块之间传递状态的稳定 API：存储层负责从 SQLite 行
构造它们，执行层和页面层只读取字段，不直接依赖数据库列细节。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GlobalSettings:
    """全局运行配置。

    max_concurrent 控制所有激活项目共享的并发上限；timeout_seconds 是单个
    任务从开始运行到终止的最长秒数；repair_max_loops 是 AI 多轮修复次数。
    """

    max_concurrent: int
    timeout_seconds: int
    repair_max_loops: int


@dataclass(frozen=True)
class AIConfig:
    """一组可编辑型 AI CLI 后端配置。

    CLI 是真正进入 worktree 修改代码的执行器；ai_provider/model_name 只描述
    这组 CLI 使用的模型大脑。api_key_env 只保存环境变量名，不保存明文密钥。
    """

    id: int
    enabled: bool
    name: str
    cli_type: str
    cli_path: str
    ai_provider: str
    model_name: str
    api_key_env: str
    config_home: str
    config_home_env: str
    extra_args: str
    command_template: str
    self_check_status: str
    self_check_message: str
    created_at: int
    updated_at: int

    def signature(self) -> str:
        """返回影响运行时重载的稳定签名。

        RuntimeManager 用它判断 AI 配置是否发生变化；变化时会重建对应运行时，
        但不会把时间戳这类元数据纳入比较。
        """

        return json.dumps(
            {
                "enabled": self.enabled,
                "name": self.name,
                "cli_type": self.cli_type,
                "cli_path": self.cli_path,
                "ai_provider": self.ai_provider,
                "model_name": self.model_name,
                "api_key_env": self.api_key_env,
                "config_home": self.config_home,
                "config_home_env": self.config_home_env,
                "extra_args": self.extra_args,
                "command_template": self.command_template,
                "self_check_status": self.self_check_status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass(frozen=True)
class ProjectConfig:
    """一个项目的监听、入队和修复规则。

    每个激活项目配置会绑定一个 AIConfig，并决定从哪个飞书群、多维表格、
    git 仓库和自动修复时间段接收任务。
    """

    id: int
    enabled: bool
    ai_config_id: int
    chat_name: str
    chat_id: str
    app_name: str
    base_name: str
    status_field_name: str
    done_status_value: str
    repo_root: str
    base_branch: str
    branch_prefix: str
    worktree_root: str
    workroot_name: str
    processable_status_values: list[str]
    repair_version: str
    auto_repair_mode: str
    auto_repair_start_time: str
    auto_repair_end_day_offset: int
    auto_repair_end_time: str
    context_collect_commands: str
    verify_commands: str
    created_at: int
    updated_at: int

    def repo_root_path(self) -> Path:
        """把配置中的 repo_root 字符串转换成 Path，供 git 命令使用。"""

        return Path(self.repo_root)

    def worktree_root_path(self) -> Path:
        """把配置中的 worktree_root 字符串转换成 Path，供 worktree 创建使用。"""

        return Path(self.worktree_root)

    def signature(self) -> str:
        """返回影响项目运行时重载的稳定签名。

        RuntimeManager 会把该签名和绑定 AIConfig 的签名合并比较；只要监听群、
        仓库、时间段、Prompt 前置命令等关键配置改变，就会重建 watcher/executor。
        """

        return json.dumps(
            {
                "enabled": self.enabled,
                "ai_config_id": self.ai_config_id,
                "chat_name": self.chat_name,
                "chat_id": self.chat_id,
                "app_name": self.app_name,
                "base_name": self.base_name,
                "status_field_name": self.status_field_name,
                "done_status_value": self.done_status_value,
                "repo_root": self.repo_root,
                "base_branch": self.base_branch,
                "branch_prefix": self.branch_prefix,
                "worktree_root": self.worktree_root,
                "workroot_name": self.workroot_name,
                "processable_status_values": self.processable_status_values,
                "repair_version": self.repair_version,
                "auto_repair_mode": self.auto_repair_mode,
                "auto_repair_start_time": self.auto_repair_start_time,
                "auto_repair_end_day_offset": self.auto_repair_end_day_offset,
                "auto_repair_end_time": self.auto_repair_end_time,
                "context_collect_commands": self.context_collect_commands,
                "verify_commands": self.verify_commands,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass(frozen=True)
class BaseRow:
    """从飞书多维表格解析出的单条记录。"""

    base_token: str
    table_id: str
    record_id: str
    fields: dict[str, Any]
    source: str


@dataclass(frozen=True)
class TaskRequest:
    """创建自动修复任务所需的最小请求数据。

    它来自一条飞书消息和可能关联的 Base 行，之后会被序列化进任务记录，
    让任务重试或服务重启后仍能还原同一份输入。
    """

    message_id: str
    chat_id: str
    record_id: str
    base_token: str
    table_id: str
    fields: dict[str, Any]
    source_message: str
    issue_summary: str


@dataclass(frozen=True)
class TaskRow:
    """SQLite 中 repair_tasks 表的一行任务快照。

    该对象既用于执行器判断状态，也用于页面展示任务详情。字段名基本对应
    数据库列，因此调用方不需要知道 SQL 查询细节。
    """

    task_id: int
    config_id: int
    ai_config_id: int
    ai_config_name: str
    cli_type: str
    cli_path: str
    ai_provider: str
    ai_model: str
    workroot_name: str
    chat_name: str
    chat_id: str
    message_id: str
    record_id: str
    base_token: str
    table_id: str
    branch_name: str
    worktree_path: str
    repo_root: str
    base_branch: str
    branch_prefix: str
    status_field_name: str
    done_status_value: str
    processable_status_values: str
    status_value: str
    problem_description: str
    repair_version: str
    payload_json: str
    prompt_text: str
    ai_reply_text: str
    context_snapshot: str
    verify_output: str
    repair_thinking_summary: str
    round_action_summary: str
    round_validation_summary: str
    round_risk_summary: str
    status: str
    summary: str
    feishu_received_at: int
    feishu_received_text: str
    feishu_received_error: str
    feishu_result_at: int
    feishu_result_text: str
    feishu_result_error: str
    round_count: int
    round_history_json: str
    commit_sha: str | None
    error: str | None
    created_at: int
    started_at: int | None
    finished_at: int | None
