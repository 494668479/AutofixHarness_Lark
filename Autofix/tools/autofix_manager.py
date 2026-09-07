#!/usr/bin/env python3
"""Autofix 本地管理台入口。

本模块负责把已经拆出去的能力重新编排起来：HTTP 页面、飞书监听、
任务队列、AI CLI 调用、worktree 提交、Base 状态回写和运行时恢复。
调用示例：

    python3 Autofix/tools/autofix_manager.py

阅读模型：Store 保存事实，Views 渲染页面，Harness 决定每轮 Prompt 与校验，
本文件只负责“什么时候调用谁，以及失败后如何落库和回显”。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import queue
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from feishu_echo_listener import (
    build_ai_prompt,
    consume_events,
    extract_card_context,
    list_chat_messages,
    lookup_base_row_for_card,
    parse_event,
    parse_list_message,
    resolve_chat_id,
    resolve_self_identity_ids,
    send_echo,
    should_skip,
    snapshot_poll_baseline,
    summarize_issue_from_row,
)

from autofix_defaults import (
    APP_NAME,
    DEFAULT_AUTO_REPAIR_END_DAY_OFFSET,
    DEFAULT_AUTO_REPAIR_END_TIME,
    DEFAULT_AUTO_REPAIR_MODE,
    DEFAULT_AUTO_REPAIR_START_TIME,
    DEFAULT_BRANCH_PREFIX,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_CONTEXT_COLLECT_COMMANDS,
    DEFAULT_DB_PATH,
    DEFAULT_DONE_STATUS,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_PORT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PROCESSABLE_STATUSES,
    DEFAULT_REPAIR_FOLLOWUP_PROMPT_TEMPLATE,
    DEFAULT_REPAIR_MAX_LOOPS,
    DEFAULT_REPAIR_PROMPT_TEMPLATE,
    DEFAULT_STATUS_FIELD,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_VERIFY_COMMANDS,
    MAX_CONTEXT_SNAPSHOT_CHARS,
    MAX_REPAIR_MAX_LOOPS,
    MAX_VERIFY_OUTPUT_CHARS,
)
from autofix_models import AIConfig, BaseRow, ProjectConfig, TaskRequest, TaskRow
from autofix_store import AutofixStore
from autofix_utils import (
    BaseStatusUpdateError,
    CannotRepairError,
    NoCodeChangesError,
    build_ai_cli_command,
    build_ai_cli_env,
    clean_subprocess_text,
    build_commit_message,
    command_for_display,
    current_git_branch,
    describe_auto_repair_window,
    discover_cli_path,
    ensure_under,
    format_status_values,
    is_auto_repair_time,
    log_task_multiline,
    log_task_status,
    normalize_base_name_value,
    normalize_path,
    normalize_text_value,
    parse_non_negative_int,
    parse_positive_int,
    read_cli_output,
    repo_root_name,
    resolve_worktree_root,
    run_checked,
    self_check_ai_config,
    sanitize_slug,
    update_base_status,
)
from autofix_feishu_echo import (
    safe_send_task_received,
    safe_send_task_result,
)
from autofix_harness import (
    build_task_command_values,
    build_repair_prompt_round,
    collect_worktree_diff,
    parse_repair_reply_summary,
    parse_repair_reply_status,
    run_task_commands,
)
from autofix_config import infer_defaults
from autofix_task_ops import (
    delete_task_worktree,
    extract_row_problem_description,
    extract_row_repair_version,
    extract_row_status_value,
    extract_source_base_name,
)
from autofix_views import (
    render_ai_config_page,
    render_config_page,
    render_dashboard,
    render_edit_config,
    render_message_page,
    render_prompt_page,
    render_task_detail,
    render_task_row,
    task_detail_payload,
)


class ConfigTaskExecutor:
    """单个项目配置的任务执行器。

    一个激活项目配置对应一个 executor。它只管理本项目的任务分发、
    worktree 创建和 AI 修复流程；全局并发由外部传入的 semaphore 统一控制，
    因此多个项目可以同时激活但仍共享同一个并发上限。
    """

    def __init__(
        self,
        *,
        store: AutofixStore,
        config: ProjectConfig,
        ai_config: AIConfig,
        max_concurrent: int,
        timeout_seconds: int,
        repair_max_loops: int,
        task_semaphore: threading.Semaphore,
    ) -> None:
        """初始化项目执行器并启动 worker。

        ``max_concurrent`` 决定本项目最多创建多少 worker；``task_semaphore``
        是所有项目共享的并发闸门，实际同时运行任务数不会超过全局配置。
        """
        self.store = store
        self.config = config
        self.ai_config = ai_config
        self.max_concurrent = max_concurrent
        self.timeout_seconds = max(1, timeout_seconds)
        self.repair_max_loops = max(1, min(MAX_REPAIR_MAX_LOOPS, repair_max_loops))
        self.task_semaphore = task_semaphore
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._workers = [
            threading.Thread(target=self._worker_loop, name=f"autofix-task-{config.id}-{idx+1}", daemon=True)
            for idx in range(max_concurrent)
        ]
        for worker in self._workers:
            worker.start()

    def close(self) -> None:
        """停止本项目的工作线程。

        用于配置停用、配置变更或服务退出。方法会投递空任务唤醒 worker，
        并等待短时间让线程自然退出。
        """
        self._stop_event.set()
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=5)

    def dispatch(
        self,
        request: TaskRequest,
        *,
        base_row: BaseRow | None = None,
        status_value: str = "",
        problem_description: str = "",
        processable_status_values: list[str] | None = None,
    ) -> TaskRow | None:
        """把一条可修复的飞书/Base 消息登记成任务。

        该方法只负责“接收并入库”：根据自动修复时间段决定初始状态是
        ``queued`` 还是 ``暂停``，保存 AI 配置快照，并发送“已收到任务”回显。
        返回 ``None`` 表示该消息已经处理过或任务无法创建。
        """
        payload_json = json.dumps(dataclasses.asdict(request), ensure_ascii=False, sort_keys=True)
        now = int(time.time())
        branch_name = self._allocate_branch_name(request.record_id)
        worktree_path = self._allocate_worktree_path(branch_name, request.message_id)
        status_value = normalize_text_value(status_value)
        problem_description = normalize_text_value(problem_description)
        processable_values = processable_status_values or self.config.processable_status_values
        should_start_now = is_auto_repair_time(self.config)
        initial_status = "queued" if should_start_now else "暂停"
        initial_error = None if should_start_now else f"当前不在自动修复时间段：{describe_auto_repair_window(self.config)}"
        task_data = {
            "config_id": self.config.id,
            "ai_config_id": self.ai_config.id,
            "ai_config_name": self.ai_config.name,
            "cli_type": self.ai_config.cli_type,
            "cli_path": self.ai_config.cli_path,
            "ai_provider": self.ai_config.ai_provider,
            "ai_model": self.ai_config.model_name,
            "workroot_name": self.config.workroot_name or repo_root_name(self.config.repo_root_path()),
            "chat_name": self.config.chat_name,
            "chat_id": request.chat_id,
            "message_id": request.message_id,
            "record_id": request.record_id,
            "base_token": request.base_token,
            "table_id": request.table_id,
            "branch_name": branch_name,
            "worktree_path": str(worktree_path),
            "repo_root": self.config.repo_root,
            "base_branch": self.config.base_branch,
            "branch_prefix": self.config.branch_prefix,
            "status_field_name": self.config.status_field_name,
            "done_status_value": self.config.done_status_value,
            "processable_status_values": format_status_values(processable_values),
            "status_value": status_value,
            "problem_description": problem_description,
            "repair_version": self.config.repair_version,
            "payload_json": payload_json,
            "prompt_text": "",
            "ai_reply_text": "",
            "context_snapshot": "",
            "verify_output": "",
            "repair_thinking_summary": "",
            "round_action_summary": "",
            "round_validation_summary": "",
            "round_risk_summary": "",
            "status": initial_status,
            "summary": request.issue_summary,
            "round_count": 0,
            "round_history_json": "[]",
            "commit_sha": None,
            "error": initial_error,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        }
        try:
            task_id = self.store.insert_task(task_data)
        except sqlite3.IntegrityError:
            return None

        task = self.store.get_task(task_id)
        if task is None:
            return None
        safe_send_task_received(self.store, task)
        log_task_status(
            task.task_id,
            request.record_id,
            "等待中" if should_start_now else "暂停",
            f"config={self.config.id} workroot={task.workroot_name} branch={branch_name} worktree={worktree_path}",
        )
        log_task_multiline(
            task.task_id,
            request.record_id,
            "分发任务",
            json.dumps(
                {
                    "config_id": self.config.id,
                    "workroot_name": task.workroot_name,
                    "chat_name": task.chat_name,
                    "message_id": request.message_id,
                    "record_id": request.record_id,
                    "branch_name": branch_name,
                    "worktree_path": str(worktree_path),
                    "issue_summary": request.issue_summary,
                    "source_message": request.source_message,
                    "fields": request.fields,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
        if should_start_now:
            self._queue.put(task.task_id)
        return task

    def record_unsupported_status(
        self,
        request: TaskRequest,
        *,
        status_value: str,
        problem_description: str,
    ) -> TaskRow | None:
        """记录“状态不支持”的终态任务。

        当 Base 状态字段不在项目配置的可处理状态列表中时调用。它不会创建
        worktree，也不会进入 AI 队列，只保存一条可追踪的历史记录并回显结果。
        """
        payload_json = json.dumps(dataclasses.asdict(request), ensure_ascii=False, sort_keys=True)
        now = int(time.time())
        branch_name = f"unsupported/{sanitize_slug(request.record_id)}-{sanitize_slug(request.message_id)}"
        worktree_path = (
            self.config.worktree_root_path()
            / "_unsupported"
            / f"{sanitize_slug(request.record_id)}-{sanitize_slug(request.message_id)}"
        ).resolve()
        task_data = {
            "config_id": self.config.id,
            "ai_config_id": self.ai_config.id,
            "ai_config_name": self.ai_config.name,
            "cli_type": self.ai_config.cli_type,
            "cli_path": self.ai_config.cli_path,
            "ai_provider": self.ai_config.ai_provider,
            "ai_model": self.ai_config.model_name,
            "workroot_name": self.config.workroot_name or repo_root_name(self.config.repo_root_path()),
            "chat_name": self.config.chat_name,
            "chat_id": request.chat_id,
            "message_id": request.message_id,
            "record_id": request.record_id,
            "base_token": request.base_token,
            "table_id": request.table_id,
            "branch_name": branch_name,
            "worktree_path": str(worktree_path),
            "repo_root": self.config.repo_root,
            "base_branch": self.config.base_branch,
            "branch_prefix": self.config.branch_prefix,
            "status_field_name": self.config.status_field_name,
            "done_status_value": self.config.done_status_value,
            "processable_status_values": format_status_values(self.config.processable_status_values),
            "status_value": normalize_text_value(status_value),
            "problem_description": normalize_text_value(problem_description),
            "repair_version": self.config.repair_version,
            "payload_json": payload_json,
            "prompt_text": "",
            "ai_reply_text": "",
            "context_snapshot": "",
            "verify_output": "",
            "repair_thinking_summary": "当前状态值不在项目配置的可处理状态列表中，因此不进入自动修复。",
            "round_action_summary": "未创建 worktree，未调用 AI CLI。",
            "round_validation_summary": "未执行验证命令。",
            "round_risk_summary": "未修改代码，未发现代码变更风险。",
            "status": "状态不支持",
            "summary": request.issue_summary,
            "round_count": 0,
            "round_history_json": "[]",
            "commit_sha": None,
            "error": f"当前状态值不在可处理状态列表中：{normalize_text_value(status_value) or '空'}",
            "created_at": now,
            "started_at": None,
            "finished_at": now,
        }
        try:
            task_id = self.store.insert_task(task_data)
        except sqlite3.IntegrityError:
            return self.store.get_task_by_config_message(self.config.id, request.message_id)
        task = self.store.get_task(task_id)
        if task is not None:
            safe_send_task_received(self.store, task)
            safe_send_task_result(self.store, task)
        return task

    def record_rejected_repair_version(
        self,
        request: TaskRequest,
        *,
        status_value: str,
        problem_description: str,
        repair_version: str,
    ) -> TaskRow | None:
        """记录“修复版本错误”的拒绝任务。

        修复版本用于把不同分支策略隔离开。版本不匹配或为空时，本方法生成
        ``拒绝修复`` 记录，避免错误地在不匹配的基础分支上自动改代码。
        """
        payload_json = json.dumps(dataclasses.asdict(request), ensure_ascii=False, sort_keys=True)
        now = int(time.time())
        branch_name = f"rejected/{sanitize_slug(request.record_id)}-{sanitize_slug(request.message_id)}"
        worktree_path = (
            self.config.worktree_root_path()
            / "_rejected"
            / f"{sanitize_slug(request.record_id)}-{sanitize_slug(request.message_id)}"
        ).resolve()
        task_data = {
            "config_id": self.config.id,
            "ai_config_id": self.ai_config.id,
            "ai_config_name": self.ai_config.name,
            "cli_type": self.ai_config.cli_type,
            "cli_path": self.ai_config.cli_path,
            "ai_provider": self.ai_config.ai_provider,
            "ai_model": self.ai_config.model_name,
            "workroot_name": self.config.workroot_name or repo_root_name(self.config.repo_root_path()),
            "chat_name": self.config.chat_name,
            "chat_id": request.chat_id,
            "message_id": request.message_id,
            "record_id": request.record_id,
            "base_token": request.base_token,
            "table_id": request.table_id,
            "branch_name": branch_name,
            "worktree_path": str(worktree_path),
            "repo_root": self.config.repo_root,
            "base_branch": self.config.base_branch,
            "branch_prefix": self.config.branch_prefix,
            "status_field_name": self.config.status_field_name,
            "done_status_value": self.config.done_status_value,
            "processable_status_values": format_status_values(self.config.processable_status_values),
            "status_value": normalize_text_value(status_value),
            "problem_description": normalize_text_value(problem_description),
            "repair_version": normalize_text_value(repair_version),
            "payload_json": payload_json,
            "prompt_text": "",
            "ai_reply_text": "修复版本错误",
            "context_snapshot": "",
            "verify_output": "",
            "repair_thinking_summary": "多维表格修复版本与项目配置不匹配或为空，因此拒绝自动修复。",
            "round_action_summary": "未创建 worktree，未调用 AI CLI。",
            "round_validation_summary": "未执行验证命令。",
            "round_risk_summary": "未修改代码，未发现代码变更风险。",
            "status": "拒绝修复",
            "summary": "修复版本错误",
            "round_count": 0,
            "round_history_json": "[]",
            "commit_sha": None,
            "error": "修复版本错误",
            "created_at": now,
            "started_at": None,
            "finished_at": now,
        }
        try:
            task_id = self.store.insert_task(task_data)
        except sqlite3.IntegrityError:
            return self.store.get_task_by_config_message(self.config.id, request.message_id)
        task = self.store.get_task(task_id)
        if task is not None:
            safe_send_task_received(self.store, task)
            safe_send_task_result(self.store, task)
        return task

    def retry_task(self, task_id: int, *, clean_worktree: bool = False) -> TaskRow | None:
        """重新调度一个失败、超时、Base 回写失败或卡在 queued 的任务。

        对普通失败任务会先清空 AI 输入/输出/摘要/轮次历史，再重新排队。
        ``clean_worktree`` 为 True 时，会先删除当前任务 worktree 和任务分支；
        否则保留 worktree 现状，让 AI 基于已有改动继续修复。
        对 ``base_update_failed`` 且已有 commit 的任务，只重试 Base 状态回写。
        """
        task = self.store.get_task(task_id)
        if task is None:
            return None
        if task.status not in {"failed", "超时", "base_update_failed", "queued"}:
            return task
        if task.status == "queued":
            self._queue.put(task_id)
            return task
        if task.status == "base_update_failed" and task.commit_sha:
            request = self._load_request(task)
            try:
                self._update_base_status(task, request)
            except Exception as exc:
                self.store.update_task(task_id, error=str(exc), finished_at=int(time.time()))
                result_task = self.store.get_task(task_id)
                if result_task is not None:
                    safe_send_task_result(self.store, result_task)
                return result_task
            self.store.update_task(
                task_id,
                status="succeeded",
                error=None,
                finished_at=int(time.time()),
            )
            result_task = self.store.get_task(task_id)
            if result_task is not None:
                safe_send_task_result(self.store, result_task)
            return result_task
        if clean_worktree:
            delete_task_worktree(self.store, task)
        self.store.update_task(
            task_id,
            prompt_text="",
            ai_reply_text="",
            context_snapshot="",
            verify_output="",
            repair_thinking_summary="",
            round_action_summary="",
            round_validation_summary="",
            round_risk_summary="",
            summary="",
            round_count=0,
            round_history_json="[]",
            ai_config_id=self.ai_config.id,
            ai_config_name=self.ai_config.name,
            cli_type=self.ai_config.cli_type,
            cli_path=self.ai_config.cli_path,
            ai_provider=self.ai_config.ai_provider,
            ai_model=self.ai_config.model_name,
        )
        self.store.update_task(
            task_id,
            status="queued",
            error=None,
            finished_at=None,
            started_at=None,
            commit_sha=None,
        )
        self._queue.put(task_id)
        return self.store.get_task(task_id)

    def start_paused_task(self, task_id: int) -> TaskRow | None:
        """绕过时间段限制，立即启动一个 ``暂停`` 任务。

        详情页的“立即开始”按钮调用这里。它复用当前项目绑定的 AI 配置快照，
        把任务改为 ``queued`` 后放进同一套全局并发队列。
        """
        task = self.store.get_task(task_id)
        if task is None:
            return None
        if task.status != "暂停":
            return task
        self.store.update_task(
            task_id,
            status="queued",
            error=None,
            finished_at=None,
            started_at=None,
            round_count=0,
            round_history_json="[]",
            ai_config_id=self.ai_config.id,
            ai_config_name=self.ai_config.name,
            cli_type=self.ai_config.cli_type,
            cli_path=self.ai_config.cli_path,
            ai_provider=self.ai_config.ai_provider,
            ai_model=self.ai_config.model_name,
        )
        self._queue.put(task_id)
        return self.store.get_task(task_id)

    def mark_no_repair(self, task_id: int) -> TaskRow | None:
        """把暂停任务人工标记为 ``无需修复``。

        这是一个人工终态：不创建 worktree、不调用 AI、不更新 Base 完成状态，
        但会发送飞书结果回显，方便群里知道该任务已被处理。
        """
        task = self.store.get_task(task_id)
        if task is None:
            return None
        if task.status != "暂停":
            return task
        self.store.update_task(
            task_id,
            status="无需修复",
            ai_reply_text="人工设置为无需修复",
            error=None,
            finished_at=int(time.time()),
        )
        result_task = self.store.get_task(task_id)
        if result_task is not None:
            safe_send_task_result(self.store, result_task)
        return result_task

    def enqueue_task_id(self, task_id: int) -> None:
        """把已有任务 ID 放回本项目队列。

        RuntimeManager 启动恢复、时间段释放暂停任务，以及手动重试都会用到。
        """
        self._queue.put(task_id)

    def _worker_loop(self) -> None:
        """后台 worker 主循环。

        每个 worker 从项目队列取任务，但真正执行前会先占用全局 semaphore，
        这就是“多项目激活但共享并发数”的实现点。
        """
        while not self._stop_event.is_set():
            task_id = self._queue.get()
            try:
                if task_id is None:
                    return
                with self.task_semaphore:
                    self._process_task(task_id)
            finally:
                self._queue.task_done()

    def _load_request(self, task: TaskRow) -> TaskRequest:
        """从任务快照恢复原始 TaskRequest。

        任务入库时保存了飞书/Base 事件的 payload。重试、恢复和 Base 回写时，
        不需要重新查消息，只要从这个 JSON 还原即可。
        """
        payload = json.loads(task.payload_json)
        return TaskRequest(
            message_id=payload["message_id"],
            chat_id=payload["chat_id"],
            record_id=payload["record_id"],
            base_token=payload["base_token"],
            table_id=payload["table_id"],
            fields=payload["fields"],
            source_message=payload["source_message"],
            issue_summary=payload["issue_summary"],
        )

    def _process_task(self, task_id: int) -> None:
        """执行一个队列任务并把所有终态写回数据库。

        这里是任务状态机的外壳：queued -> running -> succeeded/failed/超时/
        拒绝修复/base_update_failed。真正的 AI 多轮流程在 ``_run_task`` 中完成。
        """
        task = self.store.get_task(task_id)
        if task is None:
            return
        request = self._load_request(task)
        if task.status == "queued":
            self.store.update_task(task_id, status="running", started_at=int(time.time()))
        elif task.status != "running":
            return
        started_at = task.started_at or int(time.time())
        log_task_status(
            task_id,
            request.record_id,
            "执行中",
            f"config={self.config.id} record_id={request.record_id} branch={task.branch_name}",
        )

        if self._task_is_timed_out(task, started_at):
            self.store.update_task(
                task_id,
                status="超时",
                finished_at=int(time.time()),
                error="task timed out",
            )
            result_task = self.store.get_task(task_id)
            if result_task is not None:
                safe_send_task_result(self.store, result_task)
            log_task_status(
                task_id,
                request.record_id,
                "超时",
                f"config={self.config.id} record_id={request.record_id} branch={task.branch_name}",
            )
            return

        result_commit: str | None = None
        ai_reply: str = ""
        error: str | None = None
        failed_status = "failed"
        try:
            result_commit, ai_reply = self._run_task(task, request, timeout_seconds=self.timeout_seconds)
        except CannotRepairError as exc:
            error = str(exc)
            failed_status = "拒绝修复"
            ai_reply = exc.ai_reply
        except subprocess.TimeoutExpired as exc:
            error = f"task timed out after {self.timeout_seconds} seconds"
            failed_status = "超时"
            stdout_text = clean_subprocess_text(exc.stdout)
            stderr_text = clean_subprocess_text(exc.stderr)
            if stdout_text:
                ai_reply = stdout_text
            if stderr_text:
                ai_reply = f"{ai_reply}\n{stderr_text}".strip()
        except BaseStatusUpdateError as exc:
            error = str(exc)
            failed_status = "base_update_failed"
            result_commit = exc.commit_sha
        except Exception as exc:
            error = str(exc)

        finished_at = int(time.time())
        if error is not None:
            values: dict[str, Any] = {
                "status": failed_status,
                "finished_at": finished_at,
                "error": error,
            }
            if result_commit:
                values["commit_sha"] = result_commit
            if ai_reply:
                values["ai_reply_text"] = ai_reply
            self.store.update_task(task_id, **values)
            result_task = self.store.get_task(task_id)
            if result_task is not None:
                safe_send_task_result(self.store, result_task)
            log_task_status(
                task_id,
                request.record_id,
                "失败",
                f"config={self.config.id} record_id={request.record_id} branch={task.branch_name} status={failed_status} error={error}",
            )
            return

        self.store.update_task(
            task_id,
            status="succeeded",
            finished_at=finished_at,
            commit_sha=result_commit,
            error=None,
            ai_reply_text=ai_reply,
        )
        result_task = self.store.get_task(task_id)
        if result_task is not None:
            safe_send_task_result(self.store, result_task)
        log_task_status(
            task_id,
            request.record_id,
            "成功",
            f"config={self.config.id} record_id={request.record_id} branch={task.branch_name} commit={result_commit}",
        )

    def _task_is_timed_out(self, task: TaskRow, started_at: int) -> bool:
        """判断任务是否已经超过全局超时时间。"""
        if started_at <= 0:
            return False
        return int(time.time()) - started_at > self.timeout_seconds

    def _allocate_branch_name(self, record_id: str) -> str:
        """为 Base 记录生成不会撞名的修复分支名。

        分支名基于项目配置的前缀和 record_id；如果已有同名任务或 Git 分支，
        自动追加 ``-r2``、``-r3`` 等后缀。
        """
        base_prefix = self.config.branch_prefix or DEFAULT_BRANCH_PREFIX
        base_branch = f"{base_prefix}{sanitize_slug(record_id)}"
        existing = self._existing_branches()
        candidate = base_branch
        suffix = 2
        while candidate in existing or self._branch_exists(candidate):
            candidate = f"{base_branch}-r{suffix}"
            suffix += 1
        return candidate

    def _existing_branches(self) -> set[str]:
        """读取本仓库历史任务中已经占用过的分支名。"""
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT branch_name FROM repair_tasks WHERE repo_root = ?",
                (self.config.repo_root,),
            ).fetchall()
        return {str(row["branch_name"]) for row in rows}

    def _branch_exists(self, branch_name: str) -> bool:
        """检查 Git 仓库中是否已有指定本地分支。"""
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=self.config.repo_root_path(),
        )
        return result.returncode == 0

    def _allocate_worktree_path(self, branch_name: str, message_id: str) -> Path:
        """为任务生成隔离的 worktree 目录。

        目录固定落在项目配置的 ``REPAIR_WORKTREE_ROOT/<repo-name>`` 下，并使用
        分支名和 message_id 组成唯一目录名，避免不同任务互相覆盖。
        """
        repo_root = self.config.repo_root_path()
        worktree_root = self.config.worktree_root_path()
        root = (worktree_root / repo_root.name).resolve()
        ensure_under(repo_root.resolve(), root)
        path = root / f"{sanitize_slug(branch_name).replace('/', '__')}-{sanitize_slug(message_id)}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _run_task(self, task: TaskRow, request: TaskRequest, *, timeout_seconds: int) -> tuple[str, str]:
        """运行完整的 AI 修复闭环。

        心理模型：创建 worktree -> 收集上下文 -> 首轮 Prompt -> AI CLI 编辑 ->
        解析 AI 状态 -> 检查 diff/运行验证命令 -> 必要时二次 Prompt 继续循环 ->
        通过后提交并回写 Base。返回 ``(commit_sha, latest_ai_reply)``。
        """
        worktree_path = Path(task.worktree_path)
        log_task_status(task.task_id, request.record_id, "执行中", f"creating worktree={worktree_path}")
        self._create_worktree(task.branch_name, worktree_path)
        first_prompt_template = self.store.get_repair_prompt_template()
        followup_prompt_template = self.store.get_followup_prompt_template()
        rounds: list[dict[str, Any]] = []
        latest_ai_reply = ""
        first_ai_reply = ""
        first_prompt_text = ""
        verify_output = ""
        commit_sha = ""
        max_loops = max(1, min(MAX_REPAIR_MAX_LOOPS, self.repair_max_loops))
        deadline = int(time.time()) + max(1, timeout_seconds)
        command_values = build_task_command_values(task, request, worktree_path)
        _, context_snapshot = run_task_commands(
            self.config.context_collect_commands,
            cwd=worktree_path,
            values=command_values,
            timeout_seconds=DEFAULT_COMMAND_TIMEOUT_SECONDS,
            max_chars=MAX_CONTEXT_SNAPSHOT_CHARS,
        )
        self.store.update_task(task.task_id, context_snapshot=context_snapshot)
        for round_index in range(1, max_loops + 1):
            remaining = deadline - int(time.time())
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd="autofix-loop", timeout=timeout_seconds, output=latest_ai_reply or None)
            prompt = build_repair_prompt_round(
                task,
                request,
                worktree_path,
                first_prompt_template if round_index == 1 else followup_prompt_template,
                round_index=round_index,
                first_prompt=first_prompt_text,
                first_ai_reply=first_ai_reply,
                previous_ai_reply=latest_ai_reply,
                context_snapshot=context_snapshot,
                verify_output=verify_output,
            )
            if round_index == 1:
                first_prompt_text = prompt
            self.store.update_task(
                task.task_id,
                prompt_text=prompt,
                round_count=round_index,
                round_history_json=json.dumps(rounds, ensure_ascii=False),
            )
            log_task_status(task.task_id, request.record_id, "执行中", f"第 {round_index}/{max_loops} 轮 running {task.cli_type or 'ai'} cli")
            ai_reply = self._run_ai_cli_exec(
                task,
                request,
                worktree_path,
                prompt,
                timeout_seconds=remaining,
                round_index=round_index,
                max_loops=max_loops,
            ) or ""
            latest_ai_reply = ai_reply
            if round_index == 1:
                first_ai_reply = ai_reply
            if ai_reply:
                log_task_multiline(task.task_id, request.record_id, f"AI输出[第{round_index}轮]", ai_reply)
            decision = parse_repair_reply_status(ai_reply)
            reply_summary = parse_repair_reply_summary(ai_reply)
            missing_required_summary = not reply_summary["repair_thinking_summary"]
            if missing_required_summary:
                decision = "未完成"
            rounds.append(
                {
                    "round": round_index,
                    "prompt_text": prompt,
                    "ai_reply_text": ai_reply,
                    "decision": decision,
                    "verify_output": "",
                    "format_error": "缺少修复思路" if missing_required_summary else "",
                    **reply_summary,
                    "timestamp": int(time.time()),
                }
            )
            self.store.update_task(
                task.task_id,
                ai_reply_text=ai_reply,
                round_count=round_index,
                round_history_json=json.dumps(rounds, ensure_ascii=False),
                **reply_summary,
            )
            if missing_required_summary:
                log_task_status(task.task_id, request.record_id, "执行中", f"第 {round_index} 轮缺少修复思路，准备继续")
                if round_index < max_loops:
                    continue
                raise RuntimeError("AI 未按 Harness 输出格式返回")
            if decision == "无法修复":
                raise CannotRepairError(ai_reply)
            if decision == "已完成":
                current_diff = collect_worktree_diff(worktree_path)
                if current_diff == "当前没有未提交改动。":
                    raise CannotRepairError(ai_reply or "AI 未产生代码修改，按无法修复处理")
                verify_ok, verify_output = run_task_commands(
                    self.config.verify_commands,
                    cwd=worktree_path,
                    values=command_values,
                    timeout_seconds=DEFAULT_COMMAND_TIMEOUT_SECONDS,
                    max_chars=MAX_VERIFY_OUTPUT_CHARS,
                )
                self.store.update_task(task.task_id, verify_output=verify_output)
                rounds[-1]["verify_output"] = verify_output
                rounds[-1]["verify_ok"] = verify_ok
                self.store.update_task(
                    task.task_id,
                    round_history_json=json.dumps(rounds, ensure_ascii=False),
                )
                if not verify_ok:
                    log_task_status(task.task_id, request.record_id, "执行中", f"第 {round_index} 轮验证失败，准备继续")
                    if round_index < max_loops:
                        continue
                    raise RuntimeError(f"达到最大循环次数仍未通过工具层验证：{max_loops}")
                log_task_status(task.task_id, request.record_id, "执行中", f"checking git changes and committing after round {round_index}")
                try:
                    commit_sha = self._commit_worktree(task, request, worktree_path)
                except NoCodeChangesError as exc:
                    if ai_reply:
                        raise CannotRepairError(ai_reply) from exc
                    raise
                break
            if round_index < max_loops:
                log_task_status(task.task_id, request.record_id, "执行中", f"第 {round_index} 轮未完成，准备继续")
        else:
            raise RuntimeError(f"达到最大循环次数仍未完成：{max_loops}")

        self.store.update_task(task.task_id, commit_sha=commit_sha, ai_reply_text=latest_ai_reply)
        log_task_status(task.task_id, request.record_id, "代码提交完成", f"commit={commit_sha}")
        log_task_status(
            task.task_id,
            request.record_id,
            "执行中",
            f"updating Base status={self.config.done_status_value} as bot",
        )
        try:
            self._update_base_status(task, request)
        except Exception as exc:
            raise BaseStatusUpdateError(commit_sha, str(exc)) from exc
        log_task_status(task.task_id, request.record_id, "Base状态已更新", f"status={self.config.done_status_value}")
        return commit_sha, latest_ai_reply

    def _create_worktree(self, branch_name: str, worktree_path: Path) -> None:
        """从项目基础分支创建任务专属 worktree。"""
        if worktree_path.exists():
            if (worktree_path / ".git").exists():
                return
            raise RuntimeError(f"worktree path already exists: {worktree_path}")
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        start_ref = self.config.base_branch or "HEAD"
        run_checked(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path), start_ref],
            cwd=self.config.repo_root_path(),
        )

    def _run_ai_cli_exec(
        self,
        task: TaskRow,
        request: TaskRequest,
        worktree_path: Path,
        prompt: str,
        *,
        timeout_seconds: int,
        round_index: int,
        max_loops: int,
    ) -> str | None:
        """调用项目绑定的可编辑型 AI CLI。

        Prompt 会写入临时文件，同时根据 AI 配置组生成命令和隔离环境变量。
        模型名只作为本次子进程参数传入，不修改用户全局 CLI 配置。
        """
        with tempfile.NamedTemporaryFile(prefix="repair-task-", suffix=".md", delete=False) as handle:
            output_path = Path(handle.name)
        with tempfile.NamedTemporaryFile(prefix="repair-prompt-", suffix=".md", delete=False, mode="w", encoding="utf-8") as handle:
            prompt_path = Path(handle.name)
            handle.write(prompt)

        log_task_multiline(task.task_id, request.record_id, f"AI输入[第{round_index}/{max_loops}轮]", prompt)
        text = ""
        try:
            env = build_ai_cli_env(self.ai_config)
            cmd = build_ai_cli_command(
                self.ai_config,
                prompt=prompt,
                worktree_path=worktree_path,
                prompt_file=prompt_path,
                output_file=output_path,
            )
            log_task_status(task.task_id, request.record_id, "执行中", command_for_display(cmd))
            result = subprocess.run(
                cmd,
                check=True,
                cwd=worktree_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=max(1, timeout_seconds),
            )
            if result.stdout:
                log_task_multiline(task.task_id, request.record_id, f"AI日志(stdout)[第{round_index}轮]", result.stdout.strip())
            if result.stderr:
                log_task_multiline(task.task_id, request.record_id, f"AI日志(stderr)[第{round_index}轮]", result.stderr.strip())
            text = read_cli_output(output_path, result.stdout or "")
            if text:
                log_task_multiline(task.task_id, request.record_id, f"AI最终回复[第{round_index}轮]", text)
        except subprocess.CalledProcessError as exc:
            stdout_text = clean_subprocess_text(exc.stdout)
            stderr_text = clean_subprocess_text(exc.stderr)
            if stdout_text:
                log_task_multiline(task.task_id, request.record_id, f"AI日志(stdout)[第{round_index}轮]", stdout_text)
            if stderr_text:
                log_task_multiline(task.task_id, request.record_id, f"AI日志(stderr)[第{round_index}轮]", stderr_text)
            raise
        except subprocess.TimeoutExpired as exc:
            stdout_text = clean_subprocess_text(exc.stdout)
            stderr_text = clean_subprocess_text(exc.stderr)
            if stdout_text:
                log_task_multiline(task.task_id, request.record_id, f"AI日志(stdout)[第{round_index}轮]", stdout_text)
            if stderr_text:
                log_task_multiline(task.task_id, request.record_id, f"AI日志(stderr)[第{round_index}轮]", stderr_text)
            raise
        finally:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
            try:
                prompt_path.unlink()
            except FileNotFoundError:
                pass

        return text

    def _commit_worktree(self, task: TaskRow, request: TaskRequest, worktree_path: Path) -> str:
        """提交 AI 产生的 worktree 改动并返回提交 SHA。

        如果没有任何未提交改动且 HEAD 也没变化，会抛出 ``NoCodeChangesError``，
        上层会把它转换成“拒绝修复/无法修复”。
        """
        initial_head = self._git_head(worktree_path)
        status = run_checked(["git", "status", "--porcelain"], cwd=worktree_path)
        if status.stdout.strip():
            run_checked(["git", "add", "-A"], cwd=worktree_path)
            commit_message = build_commit_message(request.record_id, request.issue_summary)
            run_checked(["git", "commit", "-m", commit_message], cwd=worktree_path)
        current_head = self._git_head(worktree_path)
        if current_head == initial_head and not status.stdout.strip():
            raise NoCodeChangesError("codex did not produce any file changes")
        return current_head

    def _git_head(self, worktree_path: Path) -> str:
        """读取指定 worktree 当前 HEAD SHA。"""
        result = run_checked(["git", "rev-parse", "HEAD"], cwd=worktree_path)
        return result.stdout.strip()

    def _update_base_status(self, task: TaskRow, request: TaskRequest) -> None:
        """把 Base 记录状态更新为项目配置的完成状态值。"""
        update_base_status(
            base_token=request.base_token,
            table_id=request.table_id,
            record_id=request.record_id,
            status_field_name=task.status_field_name,
            done_status_value=task.done_status_value,
        )


class ConfigWatcher:
    """单个项目配置的飞书监听器。

    Watcher 同时启动事件消费和轮询兜底：事件流负责低延迟，轮询负责在
    lark-cli 事件流中断或漏消息时补偿。解析后的任务交给同项目 executor。
    """

    def __init__(
        self,
        *,
        store: AutofixStore,
        config: ProjectConfig,
        executor: ConfigTaskExecutor,
        self_ids: set[str],
        stop_event: threading.Event,
    ) -> None:
        """绑定项目配置、任务执行器和停止信号。"""
        self.store = store
        self.config = config
        self.executor = executor
        self.self_ids = self_ids
        self.stop_event = stop_event
        self._thread = threading.Thread(target=self._run, name=f"autofix-watch-{config.id}", daemon=True)

    def start(self) -> None:
        """启动监听线程。"""
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        """等待监听线程退出，主要用于服务关闭。"""
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        """建立飞书事件流，并启动聊天记录轮询兜底。"""
        chat_id = self.config.chat_id.strip()
        if not chat_id and self.config.chat_name.strip():
            try:
                chat_id = resolve_chat_id("", self.config.chat_name)
                self.store.update_config_chat_id(self.config.id, chat_id)
            except Exception as exc:
                print(
                    f"[watch:{self.config.id}] failed to resolve chat_id for {self.config.chat_name!r}: {exc}",
                    file=sys.stderr,
                )
                return
        if not chat_id:
            print(f"[watch:{self.config.id}] missing chat_id and chat_name", file=sys.stderr)
            return

        try:
            since_position = snapshot_poll_baseline(chat_id)
        except Exception as exc:
            print(f"[watch:{self.config.id}] cannot establish poll baseline: {exc}", file=sys.stderr)
            since_position = 0

        print(
            f"[watch:{self.config.id}] subscribed chat_name={self.config.chat_name!r} chat_id={chat_id}",
            file=sys.stderr,
        )

        proc = consume_events(chat_id, timeout=None, max_events=0)
        ready = threading.Event()
        stderr_thread = threading.Thread(target=pump_stderr, args=(proc, ready), daemon=True)
        stderr_thread.start()

        poll_stop = threading.Event()
        poll_thread = threading.Thread(
            target=poll_chat_messages_for_config,
            args=(self.store, self.executor, self.config, chat_id, self.self_ids, poll_stop, since_position),
            daemon=True,
        )
        poll_thread.start()

        deadline = time.time() + 60
        while not ready.is_set():
            if self.stop_event.is_set():
                poll_stop.set()
                proc.terminate()
                return
            if proc.poll() is not None:
                print(f"[watch:{self.config.id}] event consumer exited early", file=sys.stderr)
                poll_stop.set()
                return
            if time.time() > deadline:
                print(f"[watch:{self.config.id}] timed out waiting for event consumer readiness", file=sys.stderr)
                poll_stop.set()
                proc.terminate()
                return
            time.sleep(0.2)

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self.stop_event.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = parse_event(payload)
                if event is None:
                    continue
                try:
                    handle_event_for_config(self.store, self.executor, self.config, event, self.self_ids)
                except Exception as exc:
                    print(f"[watch:{self.config.id}] event handler failed for {event.message_id}: {exc}", file=sys.stderr)
        finally:
            poll_stop.set()
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            poll_thread.join(timeout=10)


def pump_stderr(proc: subprocess.Popen[str], ready: threading.Event) -> None:
    """转发 lark-cli stderr，并在事件流 ready 标记出现时通知 watcher。"""
    assert proc.stderr is not None
    for line in proc.stderr:
        sys.stderr.write(line)
        sys.stderr.flush()
        if "[event] ready event_key=im.message.receive_v1" in line:
            ready.set()


def poll_chat_messages_for_config(
    store: AutofixStore,
    executor: ConfigTaskExecutor,
    config: ProjectConfig,
    chat_id: str,
    self_ids: set[str],
    stop_event: threading.Event,
    since_position: int,
) -> None:
    """轮询群消息，补偿实时事件消费可能漏掉的消息。

    ``since_position`` 是启动时快照位置；函数只处理它之后的新消息，并按
    message_position 从旧到新交给统一事件处理函数。
    """
    while not stop_event.wait(DEFAULT_POLL_INTERVAL):
        page_token: str | None = None
        queued: list[tuple[int, Any]] = []
        max_seen_position = since_position
        while True:
            payload = list_chat_messages(chat_id, page_size=50, page_token=page_token)
            if payload is None:
                break
            data = payload.get("data", {})
            messages = data.get("messages") or []
            if not isinstance(messages, list) or not messages:
                break
            oldest_position: int | None = None
            for raw in reversed(messages):
                if not isinstance(raw, dict):
                    continue
                position = raw.get("message_position")
                if isinstance(position, str) and position.isdigit():
                    position_int = int(position)
                elif isinstance(position, int):
                    position_int = position
                else:
                    continue
                if oldest_position is None:
                    oldest_position = position_int
                if position_int <= since_position:
                    continue
                event = parse_list_message(raw)
                if event is None:
                    continue
                queued.append((position_int, event))
                if position_int > max_seen_position:
                    max_seen_position = position_int
            if not data.get("has_more"):
                break
            if oldest_position is None or oldest_position <= since_position:
                break
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token:
                break
        if not queued:
            continue
        for _, event in sorted(queued, key=lambda item: item[0]):
            try:
                handle_event_for_config(store, executor, config, event, self_ids)
            except Exception as exc:
                print(f"[watch:{config.id}] poll handler failed for {event.message_id}: {exc}", file=sys.stderr)
        since_position = max_seen_position


def handle_event_for_config(
    store: AutofixStore,
    executor: ConfigTaskExecutor,
    config: ProjectConfig,
    event: Any,
    self_ids: set[str],
) -> bool:
    """把一条飞书消息转换成自动修复任务或普通回显。

    处理顺序固定：跳过机器人自身消息 -> 去重 -> Base 名称过滤 -> Base 行解析 ->
    修复版本和状态值校验 -> 登记任务。非 Base 消息则走 AI/兜底回显。
    返回值表示这条消息是否被本配置消费。
    """
    if should_skip(event, self_ids):
        return False

    if store.is_processed(config.id, event.message_id):
        return False

    card = extract_card_context(event.content)
    if card.is_base_card:
        source_base_name = normalize_base_name_value(extract_source_base_name(event.content))
        expected_base_name = normalize_base_name_value(config.base_name)
        if not expected_base_name or not source_base_name or source_base_name != expected_base_name:
            store.mark_processed(config.id, event, f"SKIP:base_name_mismatch:{source_base_name}")
            print(
                f"[watch:{config.id}] skipped base card by base_name message_id={event.message_id} expected={expected_base_name!r} actual={source_base_name!r}",
                file=sys.stderr,
            )
            return False
    base_row = lookup_base_row_for_card(card) if card.is_base_card else None

    if card.is_base_card and base_row is None:
        reply_text = "未查找到唯一行"
        send_echo(event.chat_id, event.message_id, reply_text)
        store.mark_processed(config.id, event, reply_text)
        print(f"[watch:{config.id}] unresolved base card message_id={event.message_id}", file=sys.stderr)
        return True

    if card.is_base_card and base_row is not None:
        status_value = extract_row_status_value(base_row, config.status_field_name)
        repair_version = extract_row_repair_version(base_row)
        request = TaskRequest(
            message_id=event.message_id,
            chat_id=event.chat_id,
            record_id=base_row.record_id,
            base_token=base_row.base_token,
            table_id=base_row.table_id,
            fields=base_row.fields,
            source_message=event.content,
            issue_summary=summarize_issue_from_row(card, base_row),
        )
        if not config.repair_version or not repair_version or repair_version != config.repair_version:
            task = executor.record_rejected_repair_version(
                request,
                status_value=status_value,
                problem_description=extract_row_problem_description(base_row),
                repair_version=repair_version,
            )
            store.mark_processed(config.id, event, "RESULT:拒绝修复")
            if task is not None:
                print(
                    f"[watch:{config.id}] rejected repair version task_id={task.task_id} message_id={event.message_id} record_id={base_row.record_id} expected={config.repair_version!r} actual={repair_version!r}",
                    file=sys.stderr,
                )
            return True
        if not status_value or not (config.processable_status_values or DEFAULT_PROCESSABLE_STATUSES) or status_value not in (config.processable_status_values or DEFAULT_PROCESSABLE_STATUSES):
            task = executor.record_unsupported_status(
                request,
                status_value=status_value,
                problem_description=extract_row_problem_description(base_row),
            )
            store.mark_processed(config.id, event, "RESULT:状态不支持")
            if task is not None:
                print(
                    f"[watch:{config.id}] unsupported status task_id={task.task_id} message_id={event.message_id} record_id={base_row.record_id} status_value={status_value!r}",
                    file=sys.stderr,
                )
            return True
        task = executor.dispatch(
            request,
            base_row=base_row,
            status_value=status_value,
            problem_description=extract_row_problem_description(base_row),
            processable_status_values=config.processable_status_values,
        )
        if task is None:
            return False
        store.mark_processed(config.id, event, f"TASK:{task.branch_name}")
        print(
            f"[watch:{config.id}] dispatched task message_id={event.message_id} record_id={base_row.record_id} branch={task.branch_name}",
            file=sys.stderr,
        )
        return True

    prompt = build_ai_prompt(event, None, None)
    reply_text = run_ai_echo(executor.ai_config, prompt) or f"收到：{event.content.strip()}"
    if reply_text != "未查找到唯一行" and not reply_text.startswith("收到："):
        reply_text = f"收到：{reply_text}"
    send_echo(event.chat_id, event.message_id, reply_text)
    store.mark_processed(config.id, event, reply_text)
    print(f"[watch:{config.id}] echoed message_id={event.message_id} chat_id={event.chat_id}", file=sys.stderr)
    return True


def run_ai_echo(ai_config: AIConfig, prompt: str) -> str | None:
    """用指定 AI 配置组生成普通群消息回显。

    回显运行在临时空目录中，避免把非修复类问答误作用到项目 worktree。
    """
    with tempfile.TemporaryDirectory(prefix="autofix-echo-work-") as temp_dir:
        work_dir = Path(temp_dir)
        return _run_ai_echo_in_dir(ai_config, prompt, work_dir)


def _run_ai_echo_in_dir(ai_config: AIConfig, prompt: str, work_dir: Path) -> str | None:
    """在给定目录中执行一次 AI CLI 回显命令。"""
    with tempfile.NamedTemporaryFile(prefix="autofix-echo-", suffix=".md", delete=False) as handle:
        output_path = Path(handle.name)
    with tempfile.NamedTemporaryFile(prefix="autofix-echo-prompt-", suffix=".md", delete=False, mode="w", encoding="utf-8") as handle:
        prompt_path = Path(handle.name)
        handle.write(prompt)
    try:
        env = build_ai_cli_env(ai_config)
        cmd = build_ai_cli_command(
            ai_config,
            prompt=prompt,
            worktree_path=work_dir,
            prompt_file=prompt_path,
            output_file=output_path,
        )
        result = subprocess.run(cmd, check=True, cwd=work_dir, env=env, capture_output=True, text=True, timeout=180)
        text = read_cli_output(output_path, result.stdout or "")
    except Exception as exc:
        print(f"ai echo failed: {exc}", file=sys.stderr)
        text = ""
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        try:
            prompt_path.unlink()
        except FileNotFoundError:
            pass
    if not text:
        return None
    return text


class RuntimeManager:
    """全局运行时编排器。

    它读取所有激活项目配置，为每个项目创建 watcher/executor，并在配置变更时
    热重载。启动时还负责恢复 ``running``/``queued`` 任务、释放进入时间段的
    ``暂停`` 任务，以及维护全局并发 semaphore。
    """

    def __init__(self, store: AutofixStore) -> None:
        """创建运行时管理器，并读取当前全局配置初始化并发闸门。"""
        self.store = store
        self.stop_event = threading.Event()
        self.reload_event = threading.Event()
        self._lock = threading.Lock()
        self._self_ids = resolve_self_identity_ids()
        settings = self.store.get_global_settings()
        self._settings_signature = json.dumps(dataclasses.asdict(settings), sort_keys=True, ensure_ascii=False)
        self._task_semaphore = threading.Semaphore(settings.max_concurrent)
        self._timeout_seconds = settings.timeout_seconds
        self._runtimes: dict[int, tuple[str, ConfigWatcher, ConfigTaskExecutor]] = {}
        self._thread = threading.Thread(target=self._loop, name="autofix-runtime-manager", daemon=True)

    def start(self) -> None:
        """启动所有激活配置的 watcher/executor，并恢复上次未完成任务。"""
        self._refresh_runtimes()
        self._recover_running_tasks()
        self._release_paused_tasks()
        self._thread.start()

    def stop(self) -> None:
        """停止运行时线程，并关闭所有项目 watcher/executor。"""
        self.stop_event.set()
        self.reload_event.set()
        self._thread.join(timeout=10)
        with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for _, watcher, executor in runtimes:
            try:
                watcher.stop_event.set()
            except Exception:
                pass
            executor.close()

    def request_reload(self) -> None:
        """请求运行时尽快重新读取配置。"""
        self.reload_event.set()

    def _loop(self) -> None:
        """周期性刷新激活配置，并检查暂停任务是否可释放。"""
        while not self.stop_event.is_set():
            self._refresh_runtimes()
            self._release_paused_tasks()
            self.reload_event.wait(2.0)
            self.reload_event.clear()

    def _recover_running_tasks(self) -> None:
        """服务启动时修复上次遗留的 ``running`` 和 ``queued`` 任务。

        ``queued`` 会重新入队；``running`` 会根据 worktree 是否已有修改/提交来
        选择补提交并回写 Base、标记超时，或标记为 failed，避免任务永久卡住。
        """
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM repair_tasks WHERE status IN ('running', 'queued') ORDER BY started_at ASC, task_id ASC"
            ).fetchall()
        if not rows:
            return

        configs = {config.id: config for config in self.store.list_configs()}
        for row in rows:
            task = self.store._row_to_task(row)
            config = configs.get(task.config_id)
            if config is None:
                self.store.update_task(task.task_id, status="failed", finished_at=int(time.time()), error="missing config")
                result_task = self.store.get_task(task.task_id)
                if result_task is not None:
                    safe_send_task_result(self.store, result_task)
                continue

            task_path = Path(task.worktree_path)
            started_at = task.started_at or task.created_at
            if task.status == "queued":
                executor = self._find_executor(task.config_id)
                if executor is None:
                    self.store.update_task(task.task_id, status="failed", finished_at=int(time.time()), error="missing executor")
                    result_task = self.store.get_task(task.task_id)
                    if result_task is not None:
                        safe_send_task_result(self.store, result_task)
                    continue
                executor.enqueue_task_id(task.task_id)
                continue
            if task.commit_sha or (task_path.exists() and self._task_has_repair_result(task)):
                self._recover_task_success(config, task)
                continue
            if int(time.time()) - started_at > self._timeout_seconds:
                self.store.update_task(
                    task.task_id,
                    status="超时",
                    finished_at=int(time.time()),
                    error="task timed out",
                )
                result_task = self.store.get_task(task.task_id)
                if result_task is not None:
                    safe_send_task_result(self.store, result_task)
                continue
            self.store.update_task(task.task_id, status="failed", finished_at=int(time.time()), error="restarted without completion")
            result_task = self.store.get_task(task.task_id)
            if result_task is not None:
                safe_send_task_result(self.store, result_task)

    def _task_has_repair_result(self, task: TaskRow) -> bool:
        """判断重启前的 running 任务是否已经留下可恢复的修复结果。"""
        try:
            task_path = Path(task.worktree_path)
            if task.commit_sha:
                return True
            status = run_checked(["git", "status", "--porcelain"], cwd=task_path).stdout.strip()
            if status:
                return True
            head = self._git_head(task_path)
            base_head = run_checked(["git", "rev-parse", task.base_branch], cwd=Path(task.repo_root)).stdout.strip()
            return bool(head and base_head and head != base_head)
        except Exception:
            return False

    def _recover_task_success(self, config: ProjectConfig, task: TaskRow) -> None:
        """把已产生改动或提交的历史 running 任务恢复为成功/回写失败。"""
        task_path = Path(task.worktree_path)
        request = self._load_request(task)
        commit_sha = task.commit_sha or ""
        try:
            if task_path.exists():
                commit_sha = self._git_head(task_path)
                status = run_checked(["git", "status", "--porcelain"], cwd=task_path).stdout.strip()
                if status and not task.commit_sha:
                    run_checked(["git", "add", "-A"], cwd=task_path)
                    run_checked(["git", "commit", "-m", build_commit_message(task.record_id, request.issue_summary)], cwd=task_path)
                    commit_sha = self._git_head(task_path)
            if not commit_sha:
                raise RuntimeError("restarted without completion")
            self.store.update_task(task.task_id, commit_sha=commit_sha)
            self._update_base_status(task, request)
            self.store.update_task(
                task.task_id,
                status="succeeded",
                commit_sha=commit_sha,
                finished_at=int(time.time()),
                error=None,
            )
            result_task = self.store.get_task(task.task_id)
            if result_task is not None:
                safe_send_task_result(self.store, result_task)
        except Exception as exc:
            self.store.update_task(
                task.task_id,
                status="base_update_failed" if commit_sha else "failed",
                finished_at=int(time.time()),
                error=str(exc),
            )
            result_task = self.store.get_task(task.task_id)
            if result_task is not None:
                safe_send_task_result(self.store, result_task)

    def _load_request(self, task: TaskRow) -> TaskRequest:
        """从任务 payload JSON 还原 Base/飞书请求。"""
        payload = json.loads(task.payload_json)
        return TaskRequest(
            message_id=payload["message_id"],
            chat_id=payload["chat_id"],
            record_id=payload["record_id"],
            base_token=payload["base_token"],
            table_id=payload["table_id"],
            fields=payload["fields"],
            source_message=payload["source_message"],
            issue_summary=payload["issue_summary"],
        )

    def _git_head(self, worktree_path: Path) -> str:
        """读取指定 worktree 的 HEAD SHA。"""
        result = run_checked(["git", "rev-parse", "HEAD"], cwd=worktree_path)
        return result.stdout.strip()

    def _update_base_status(self, task: TaskRow, request: TaskRequest) -> None:
        """在启动恢复流程中回写 Base 完成状态。"""
        update_base_status(
            base_token=request.base_token,
            table_id=request.table_id,
            record_id=request.record_id,
            status_field_name=task.status_field_name,
            done_status_value=task.done_status_value,
        )

    def _status_is_supported(self, row_status: str, config: ProjectConfig) -> bool:
        """判断 Base 行状态是否允许进入自动修复。"""
        allowed = config.processable_status_values or DEFAULT_PROCESSABLE_STATUSES
        return row_status in allowed

    def _release_paused_tasks(self) -> None:
        """把已进入自动修复时间段的暂停任务按顺序放回队列。"""
        with self._lock:
            runtime_items = list(self._runtimes.items())
        for config_id, (_, _, executor) in runtime_items:
            if not is_auto_repair_time(executor.config):
                continue
            paused_tasks = self.store.list_paused_tasks_for_config(config_id)
            for task in paused_tasks:
                released = executor.start_paused_task(task.task_id)
                if released is not None and released.status == "queued":
                    log_task_status(
                        task.task_id,
                        task.record_id,
                        "等待中",
                        f"auto repair window opened config={config_id}",
                    )

    def _find_executor(self, config_id: int) -> ConfigTaskExecutor | None:
        """按项目配置 ID 查找当前运行中的 executor。"""
        with self._lock:
            item = self._runtimes.get(config_id)
        return item[2] if item else None

    def _refresh_runtimes(self) -> None:
        """根据数据库配置创建、关闭或重建项目运行时。

        配置签名变化会触发重建，确保 CLI、模型、时间段、并发等变更只影响
        本管理台当前子进程，不污染用户其它 CLI 使用方式。
        """
        settings = self.store.get_global_settings()
        settings_signature = json.dumps(dataclasses.asdict(settings), sort_keys=True, ensure_ascii=False)
        if settings_signature != self._settings_signature:
            with self._lock:
                runtimes = list(self._runtimes.values())
                self._runtimes.clear()
            for _, watcher, executor in runtimes:
                watcher.stop_event.set()
                executor.close()
            self._task_semaphore = threading.Semaphore(max(1, settings.max_concurrent))
            self._timeout_seconds = settings.timeout_seconds
            self._settings_signature = settings_signature

        ai_configs = {config.id: config for config in self.store.list_ai_configs() if config.enabled}
        default_ai_config = self.store.get_default_ai_config()
        configs = {config.id: config for config in self.store.list_configs() if config.enabled}
        with self._lock:
            existing_ids = set(self._runtimes)

            for config_id in list(existing_ids):
                signature, watcher, executor = self._runtimes[config_id]
                config = configs.get(config_id)
                ai_config = ai_configs.get(config.ai_config_id) if config else None
                if ai_config is None and default_ai_config is not None and default_ai_config.enabled:
                    ai_config = default_ai_config
                combined_signature = ""
                if config is not None and ai_config is not None:
                    combined_signature = json.dumps(
                        {"config": config.signature(), "ai_config": ai_config.signature()},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                if config is None or ai_config is None or ai_config.self_check_status == "failed" or combined_signature != signature:
                    watcher.stop_event.set()
                    executor.close()
                    del self._runtimes[config_id]

            for config in configs.values():
                if config.id in self._runtimes:
                    continue
                ai_config = ai_configs.get(config.ai_config_id)
                if ai_config is None and default_ai_config is not None and default_ai_config.enabled:
                    ai_config = default_ai_config
                if ai_config is None:
                    print(f"[runtime] skipped config={config.id}: missing ai config", file=sys.stderr)
                    continue
                if ai_config.self_check_status == "failed":
                    print(f"[runtime] skipped config={config.id}: ai config self-check failed", file=sys.stderr)
                    continue
                combined_signature = json.dumps(
                    {"config": config.signature(), "ai_config": ai_config.signature()},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                executor = ConfigTaskExecutor(
                    store=self.store,
                    config=config,
                    ai_config=ai_config,
                    max_concurrent=max(1, settings.max_concurrent),
                    timeout_seconds=settings.timeout_seconds,
                    repair_max_loops=settings.repair_max_loops,
                    task_semaphore=self._task_semaphore,
                )
                watcher = ConfigWatcher(
                    store=self.store,
                    config=config,
                    executor=executor,
                    self_ids=self._self_ids,
                    stop_event=threading.Event(),
                )
                self._runtimes[config.id] = (combined_signature, watcher, executor)
                watcher.start()

    def active_config_ids(self) -> set[int]:
        """返回当前真正处于运行状态的项目配置 ID 集合。"""
        with self._lock:
            return set(self._runtimes)


class AutofixHTTPRequestHandler(BaseHTTPRequestHandler):
    """管理台 HTTP 请求处理器。

    GET 渲染页面和 SSE/JSON API，POST 执行保存配置、AI 自检、任务重试、
    删除任务等命令。它不保存状态本身，只通过 Store 和 RuntimeManager 交互。
    """

    store: AutofixStore
    runtime: RuntimeManager

    def log_message(self, fmt: str, *args: Any) -> None:
        """把 Web 请求日志写到 stderr，方便终端查看。"""
        print(f"[web] {fmt % args}", file=sys.stderr)

    def _send_html(self, content: str, *, status: int = 200) -> None:
        """发送 UTF-8 HTML 响应。"""
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        """发送 JSON API 响应，并禁止浏览器缓存。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_event(self, event: str, data: str = "") -> None:
        """向前端推送一个 Server-Sent Events 事件。"""
        body = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        self.wfile.write(body)
        self.wfile.flush()

    def _send_redirect(self, location: str) -> None:
        """发送 303 重定向，避免表单刷新重复提交。"""
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _read_form(self) -> dict[str, str]:
        """读取 application/x-www-form-urlencoded 表单为普通字典。"""
        length = int(self.headers.get("Content-Length", "0") or "0")
        data = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = parse_qs(data, keep_blank_values=True)
        result: dict[str, str] = {}
        for key, values in parsed.items():
            if not values:
                result[key] = ""
            else:
                result[key] = values[-1]
        return result

    def do_GET(self) -> None:
        """处理页面、任务 JSON 和实时刷新 SSE 请求。"""
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/stream":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_version = self.store.change_version()
            try:
                while True:
                    current_version = self.store.wait_for_change(last_version, timeout=25.0)
                    if current_version > last_version:
                        last_version = current_version
                        self._send_sse_event("change", str(last_version))
                    else:
                        self._send_sse_event("ping", str(last_version))
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                print(f"[web] sse stream failed: {exc}", file=sys.stderr)
            return
        if path == "/api/tasks":
            tasks = self.store.list_tasks(limit=200)
            updated_at = max(
                [
                    max(
                        task.created_at,
                        task.started_at or 0,
                        task.finished_at or 0,
                        task.feishu_received_at,
                        task.feishu_result_at,
                    )
                    for task in tasks
                ],
                default=0,
            )
            self._send_json(
                {
                    "ok": True,
                    "updated_at": updated_at,
                    "rows_html": "".join(render_task_row(task) for task in tasks) if tasks else '<tr><td colspan="9" class="muted">暂无任务</td></tr>',
                }
            )
            return
        if path.startswith("/api/task/"):
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[2].isdigit():
                task = self.store.get_task(int(parts[2]))
                if task is None:
                    self._send_json({"ok": False, "error": "任务不存在"}, status=404)
                    return
                self._send_json({"ok": True, "task": task_detail_payload(task)})
                return
        if path == "/":
            self._send_html(render_dashboard(self.store, self.runtime))
            return
        if path == "/config":
            self._send_html(render_config_page(self.store, self.runtime))
            return
        if path == "/ai":
            self._send_html(render_ai_config_page(self.store, self.runtime))
            return
        if path.startswith("/ai/") and path.endswith("/edit"):
            parts = path.strip("/").split("/")
            ai_config = self.store.get_ai_config(int(parts[1])) if len(parts) >= 3 and parts[2] == "edit" else None
            self._send_html(render_ai_config_page(self.store, self.runtime, edit_config=ai_config))
            return
        if path == "/prompt":
            self._send_html(render_prompt_page(self.store))
            return
        if path.startswith("/config/") and path.endswith("/edit"):
            parts = path.strip("/").split("/")
            config = self.store.get_config(int(parts[1])) if len(parts) >= 3 and parts[2] == "edit" else None
            self._send_html(render_edit_config(self.store, config))
            return
        if path.startswith("/task/"):
            parts = path.strip("/").split("/")
            if len(parts) >= 2 and parts[1].isdigit():
                self._send_html(render_task_detail(self.store, self.store.get_task(int(parts[1]))))
                return
        self._send_html(render_message_page("页面不存在。"), status=404)

    def do_POST(self) -> None:
        """处理所有会改变系统状态的表单动作。"""
        parsed = urlparse(self.path)
        path = parsed.path
        form = self._read_form()
        message = ""

        if path == "/global/save":
            max_concurrent = parse_positive_int(form.get("max_concurrent"), DEFAULT_MAX_CONCURRENT)
            timeout_seconds = parse_positive_int(form.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS)
            repair_max_loops = parse_positive_int(form.get("repair_max_loops"), DEFAULT_REPAIR_MAX_LOOPS)
            self.store.save_global_settings(max_concurrent, timeout_seconds, repair_max_loops)
            self.runtime.request_reload()
            self._send_redirect("/config")
            return

        if path == "/ai/save":
            ai_config_id_raw = form.get("ai_config_id", "").strip()
            ai_config_id = int(ai_config_id_raw) if ai_config_id_raw else None
            values = {
                "enabled": form.get("enabled") == "1",
                "name": form.get("name", "").strip(),
                "cli_type": form.get("cli_type", "custom").strip() or "custom",
                "cli_path": form.get("cli_path", "").strip(),
                "ai_provider": form.get("ai_provider", "custom").strip() or "custom",
                "model_name": form.get("model_name", "").strip(),
                "api_key_env": form.get("api_key_env", "").strip(),
                "config_home": form.get("config_home", "").strip(),
                "config_home_env": form.get("config_home_env", "").strip(),
                "extra_args": form.get("extra_args", "").strip(),
                "command_template": form.get("command_template", "").strip(),
                "self_check_status": "unchecked",
                "self_check_message": "已保存，尚未自检",
            }
            if not values["cli_path"]:
                values["cli_path"] = discover_cli_path(values["cli_type"])
            self.store.upsert_ai_config(ai_config_id, values)
            self.runtime.request_reload()
            self._send_redirect("/ai")
            return

        if path == "/ai/scan":
            created = self.store.scan_cli_configs()
            self.runtime.request_reload()
            self._send_html(render_ai_config_page(self.store, self.runtime, f"CLI 搜索完成，新增 {created} 个配置组"))
            return

        if path.startswith("/ai/") and path.endswith("/test"):
            ai_config_id = int(path.strip("/").split("/")[1])
            ai_config = self.store.get_ai_config(ai_config_id)
            if ai_config is not None:
                status, message = self_check_ai_config(ai_config)
                self.store.update_ai_config_self_check(ai_config_id, status, message)
                self.runtime.request_reload()
                self._send_html(render_ai_config_page(self.store, self.runtime, f"自检完成：{message}"))
                return
            self._send_redirect("/ai")
            return

        if path.startswith("/ai/") and path.endswith("/delete"):
            ai_config_id = int(path.strip("/").split("/")[1])
            self.store.delete_ai_config(ai_config_id)
            self.runtime.request_reload()
            self._send_redirect("/ai")
            return

        if path == "/prompt/save":
            if form.get("reset_default") == "1":
                self.store.save_repair_prompt_template(DEFAULT_REPAIR_PROMPT_TEMPLATE)
                self.store.save_followup_prompt_template(DEFAULT_REPAIR_FOLLOWUP_PROMPT_TEMPLATE)
            else:
                self.store.save_repair_prompt_template(form.get("prompt_template", ""))
                self.store.save_followup_prompt_template(form.get("followup_prompt_template", ""))
            self._send_html(render_prompt_page(self.store, "提示词已保存"))
            return

        if path == "/config/save":
            config_id_raw = form.get("config_id", "").strip()
            config_id = int(config_id_raw) if config_id_raw else None
            repo_root_value = form.get("repo_root", "").strip()
            if not repo_root_value:
                self._send_html(render_message_page("REPO_ROOT 不能为空。", status_class="status-bad"), status=400)
                return
            repo_root = normalize_path(repo_root_value)
            if not repo_root.exists():
                self._send_html(render_message_page("REPO_ROOT 不存在。", status_class="status-bad"), status=400)
                return
            worktree_root = resolve_worktree_root(repo_root, form.get("worktree_root") or "")
            values = {
                "enabled": form.get("enabled") == "1",
                "ai_config_id": form.get("ai_config_id", "0"),
                "chat_name": form.get("chat_name", "").strip(),
                "chat_id": form.get("chat_id", "").strip(),
                "app_name": form.get("app_name", "").strip(),
                "base_name": form.get("base_name", "").strip(),
                "status_field_name": form.get("status_field_name", DEFAULT_STATUS_FIELD).strip() or DEFAULT_STATUS_FIELD,
                "done_status_value": form.get("done_status_value", DEFAULT_DONE_STATUS).strip() or DEFAULT_DONE_STATUS,
                "repo_root": str(repo_root),
                "base_branch": form.get("base_branch", "").strip() or current_git_branch(repo_root),
                "branch_prefix": form.get("branch_prefix", DEFAULT_BRANCH_PREFIX).strip() or DEFAULT_BRANCH_PREFIX,
                "worktree_root": str(worktree_root),
                "workroot_name": form.get("workroot_name", "").strip() or repo_root_name(repo_root),
                "processable_status_values": form.get("processable_status_values", ""),
                "repair_version": form.get("repair_version", "").strip(),
                "auto_repair_mode": form.get("auto_repair_mode", DEFAULT_AUTO_REPAIR_MODE),
                "auto_repair_start_time": form.get("auto_repair_start_time", DEFAULT_AUTO_REPAIR_START_TIME),
                "auto_repair_end_day_offset": form.get("auto_repair_end_day_offset", str(DEFAULT_AUTO_REPAIR_END_DAY_OFFSET)),
                "auto_repair_end_time": form.get("auto_repair_end_time", DEFAULT_AUTO_REPAIR_END_TIME),
                "context_collect_commands": form.get("context_collect_commands", DEFAULT_CONTEXT_COLLECT_COMMANDS),
                "verify_commands": form.get("verify_commands", DEFAULT_VERIFY_COMMANDS),
            }
            selected_ai_config = self.store.get_ai_config(parse_non_negative_int(values["ai_config_id"], 0))
            if values["enabled"]:
                if selected_ai_config is None:
                    self._send_html(render_message_page("激活项目必须选择 AI 配置组。", status_class="status-bad"), status=400)
                    return
                if not selected_ai_config.enabled:
                    self._send_html(render_message_page("该 AI 配置组已停用，不能绑定到激活项目。", status_class="status-bad"), status=400)
                    return
                if selected_ai_config.self_check_status == "failed":
                    self._send_html(render_message_page("该 AI 配置组自检失败，不能绑定到激活项目。", status_class="status-bad"), status=400)
                    return
            config_id = self.store.upsert_config(config_id, values)
            if values["enabled"] and not values["chat_id"] and values["chat_name"]:
                try:
                    chat_id = resolve_chat_id("", values["chat_name"])
                    self.store.update_config_chat_id(config_id, chat_id)
                except Exception as exc:
                    message = f"已保存，但群ID自动解析失败：{exc}"
            self.runtime.request_reload()
            self._send_redirect("/config")
            return

        if path.startswith("/config/") and path.endswith("/toggle"):
            config_id = int(path.strip("/").split("/")[1])
            config = self.store.get_config(config_id)
            if config is not None:
                if not config.enabled:
                    ai_config = self.store.get_ai_config(config.ai_config_id)
                    if ai_config is None or not ai_config.enabled:
                        self._send_html(render_message_page("激活项目必须先绑定启用的 AI 配置组。", status_class="status-bad"), status=400)
                        return
                    if ai_config.self_check_status == "failed":
                        self._send_html(render_message_page("该 AI 配置组自检失败，不能激活项目。", status_class="status-bad"), status=400)
                        return
                self.store.set_config_enabled(config_id, not config.enabled)
                self.runtime.request_reload()
            self._send_redirect("/config")
            return

        if path.startswith("/config/") and path.endswith("/delete"):
            config_id = int(path.strip("/").split("/")[1])
            self.store.delete_config(config_id)
            self.runtime.request_reload()
            self._send_redirect("/config")
            return

        if path.startswith("/config/") and path.endswith("/init"):
            config_id = int(path.strip("/").split("/")[1])
            config = self.store.get_config(config_id)
            if config is None:
                self._send_redirect("/config")
                return
            defaults = infer_defaults(Path(config.repo_root or Path.cwd()))
            try:
                repo_root = normalize_path(config.repo_root or defaults["repo_root"])
            except Exception:
                repo_root = Path(defaults["repo_root"]).resolve()
            values = {
                "enabled": config.enabled,
                "ai_config_id": config.ai_config_id,
                "chat_name": config.chat_name or defaults["chat_name"],
                "chat_id": config.chat_id or defaults["chat_id"],
                "app_name": config.app_name or defaults["app_name"],
                "base_name": config.base_name or defaults["base_name"],
                "status_field_name": config.status_field_name or defaults["status_field_name"],
                "done_status_value": config.done_status_value or defaults["done_status_value"],
                "repo_root": str(repo_root),
                "base_branch": config.base_branch or defaults["base_branch"],
                "branch_prefix": config.branch_prefix or defaults["branch_prefix"],
                "worktree_root": config.worktree_root or defaults["worktree_root"],
                "workroot_name": config.workroot_name or defaults["workroot_name"],
                "processable_status_values": format_status_values(config.processable_status_values or DEFAULT_PROCESSABLE_STATUSES),
                "repair_version": config.repair_version or defaults["repair_version"],
                "auto_repair_mode": config.auto_repair_mode or defaults["auto_repair_mode"],
                "auto_repair_start_time": config.auto_repair_start_time or defaults["auto_repair_start_time"],
                "auto_repair_end_day_offset": config.auto_repair_end_day_offset,
                "auto_repair_end_time": config.auto_repair_end_time or defaults["auto_repair_end_time"],
                "context_collect_commands": config.context_collect_commands,
                "verify_commands": config.verify_commands,
            }
            config_id = self.store.upsert_config(config_id, values)
            if not values["chat_id"] and values["chat_name"]:
                try:
                    chat_id = resolve_chat_id("", values["chat_name"])
                    self.store.update_config_chat_id(config_id, chat_id)
                    message = "已完成自动查找并写回群ID"
                except Exception as exc:
                    message = f"自动查找未完全成功：{exc}"
            else:
                message = "已按默认项初始化"
            self.runtime.request_reload()
            self._send_redirect("/config")
            return

        if path.startswith("/task/") and path.endswith("/delete-worktree"):
            task_id = int(path.strip("/").split("/")[1])
            task = self.store.get_task(task_id)
            if task is not None:
                try:
                    delete_task_worktree(self.store, task)
                except Exception as exc:
                    self._send_html(render_message_page(f"删除 worktree 失败：{exc}", status_class="status-bad"), status=400)
                    return
                self.store.delete_task(task_id)
            self._send_redirect("/")
            return

        if path.startswith("/task/") and path.endswith("/delete"):
            task_id = int(path.strip("/").split("/")[1])
            self.store.delete_task(task_id)
            self._send_redirect("/")
            return

        if path.startswith("/task/") and path.endswith("/retry-continue"):
            task_id = int(path.strip("/").split("/")[1])
            task = self.store.get_task(task_id)
            if task is not None:
                config = self.store.get_config(task.config_id)
                if config is not None:
                    executor = self._find_executor(config.id)
                    if executor is not None:
                        executor.retry_task(task_id, clean_worktree=False)
                    else:
                        self.store.update_task(task_id, status="failed", error="executor unavailable")
            self._send_redirect(f"/task/{task_id}")
            return

        if path.startswith("/task/") and path.endswith("/retry-clean"):
            task_id = int(path.strip("/").split("/")[1])
            task = self.store.get_task(task_id)
            if task is not None:
                config = self.store.get_config(task.config_id)
                if config is not None:
                    executor = self._find_executor(config.id)
                    if executor is not None:
                        try:
                            executor.retry_task(task_id, clean_worktree=True)
                        except Exception as exc:
                            self._send_html(
                                render_message_page(f"清空 worktree 后重新修复失败：{exc}", status_class="status-bad"),
                                status=400,
                            )
                            return
                    else:
                        self.store.update_task(task_id, status="failed", error="executor unavailable")
            self._send_redirect(f"/task/{task_id}")
            return

        if path.startswith("/task/") and path.endswith("/retry"):
            task_id = int(path.strip("/").split("/")[1])
            task = self.store.get_task(task_id)
            if task is not None:
                config = self.store.get_config(task.config_id)
                if config is not None:
                    executor = self._find_executor(config.id)
                    if executor is not None:
                        executor.retry_task(task_id)
                    else:
                        self.store.update_task(task_id, status="failed", error="executor unavailable")
            self._send_redirect(f"/task/{task_id}")
            return

        if path.startswith("/task/") and path.endswith("/start-now"):
            task_id = int(path.strip("/").split("/")[1])
            task = self.store.get_task(task_id)
            if task is not None:
                executor = self._find_executor(task.config_id)
                if executor is None:
                    self._send_html(render_message_page("项目未运行，无法立即开始。", status_class="status-bad"), status=400)
                    return
                executor.start_paused_task(task_id)
            self._send_redirect(f"/task/{task_id}")
            return

        if path.startswith("/task/") and path.endswith("/no-repair"):
            task_id = int(path.strip("/").split("/")[1])
            task = self.store.get_task(task_id)
            if task is not None and task.status == "暂停":
                executor = self._find_executor(task.config_id)
                if executor is not None:
                    executor.mark_no_repair(task_id)
                else:
                    self.store.update_task(
                        task_id,
                        status="无需修复",
                        ai_reply_text="人工设置为无需修复",
                        error=None,
                        finished_at=int(time.time()),
                    )
                    result_task = self.store.get_task(task_id)
                    if result_task is not None:
                        safe_send_task_result(self.store, result_task)
            self._send_redirect(f"/task/{task_id}")
            return

        self._send_html(render_message_page("未知操作。"), status=404)

    def _find_executor(self, config_id: int) -> ConfigTaskExecutor | None:
        """从 RuntimeManager 中获取指定项目的 executor，供任务按钮调用。"""
        with self.runtime._lock:
            item = self.runtime._runtimes.get(config_id)
            if item is None:
                return None
            return item[2]


class AutofixThreadingHTTPServer(ThreadingHTTPServer):
    """忽略浏览器主动断开导致的常见连接错误。"""

    def handle_error(self, request: Any, client_address: Any) -> None:
        """只打印真正需要排查的 HTTP 处理异常。"""
        exc_type, _, _ = sys.exc_info()
        if exc_type in {ConnectionResetError, BrokenPipeError, ConnectionAbortedError}:
            return
        super().handle_error(request, client_address)


def choose_port(preferred: int) -> int:
    """从首选端口开始寻找一个可绑定的本地端口。"""
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="Autofix local management console")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    return parser


def main() -> int:
    """启动管理台、运行时后台线程和浏览器入口。"""
    parser = build_parser()
    args = parser.parse_args()

    store = AutofixStore(args.db)
    settings = store.get_global_settings()
    if settings.max_concurrent <= 0:
        store.save_global_settings(DEFAULT_MAX_CONCURRENT, settings.timeout_seconds, settings.repair_max_loops)

    runtime = RuntimeManager(store)
    runtime.start()

    handler_cls = type(
        "AutofixHandler",
        (AutofixHTTPRequestHandler,),
        {"store": store, "runtime": runtime},
    )

    port = choose_port(args.port)
    server = AutofixThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"

    server_thread = threading.Thread(target=server.serve_forever, name="autofix-http", daemon=True)
    server_thread.start()

    print(f"{APP_NAME} running at {url}", file=sys.stderr)
    if not args.no_open:
        try:
            webbrowser.open(url, new=2)
        except Exception as exc:
            print(f"browser open failed: {exc}", file=sys.stderr)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("stopping...", file=sys.stderr)
    finally:
        server.shutdown()
        runtime.stop()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
