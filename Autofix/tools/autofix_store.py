"""SQLite 持久化层。

Autofix 同时有后台监听、任务执行线程、网页刷新接口在读写状态。这个模块把
所有数据库访问集中起来，用同一个连接和锁保证更新原子性，并通过 change_version
通知 SSE 页面刷新。

调用示例:
    store = AutofixStore(db_path)
    task = store.get_task(task_id)
    store.update_task(task_id, status="running")
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from autofix_defaults import (
    APP_SCHEMA_VERSION,
    DEFAULT_AUTO_REPAIR_END_DAY_OFFSET,
    DEFAULT_AUTO_REPAIR_END_TIME,
    DEFAULT_AUTO_REPAIR_MODE,
    DEFAULT_AUTO_REPAIR_START_TIME,
    DEFAULT_BRANCH_PREFIX,
    DEFAULT_CONTEXT_COLLECT_COMMANDS,
    DEFAULT_DONE_STATUS,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_PROCESSABLE_STATUSES,
    DEFAULT_REPAIR_FOLLOWUP_PROMPT_TEMPLATE,
    DEFAULT_REPAIR_MAX_LOOPS,
    DEFAULT_REPAIR_PROMPT_TEMPLATE,
    DEFAULT_STATUS_FIELD,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_VERIFY_COMMANDS,
    MAX_REPAIR_MAX_LOOPS,
)
from autofix_models import AIConfig, GlobalSettings, ProjectConfig, TaskRow
from autofix_utils import (
    default_ai_config_name,
    default_ai_provider,
    default_config_home_env,
    discover_cli_path,
    format_status_values,
    normalize_auto_repair_mode,
    normalize_hhmm,
    parse_end_day_offset,
    parse_non_negative_int,
    parse_positive_int,
    parse_status_values,
    resolve_codex_bin,
)


class AutofixStore:
    """Autofix 的唯一 SQLite 数据入口。

    表职责:
        - global_settings: 全局并发、超时和 Prompt 配置。
        - ai_configs: 可编辑型 AI CLI 配置组。
        - project_configs: 飞书群/Base/git/worktree 项目配置。
        - repair_tasks: 自动修复任务和轮次/回显状态。
        - processed_messages: 每个配置已处理过的飞书消息，防止重复入队。
    """

    def __init__(self, db_path: Path) -> None:
        """打开数据库、初始化 schema，并准备变更通知条件变量。"""

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._change_condition = threading.Condition()
        self._change_version = 0
        self._ensure_schema()

    def close(self) -> None:
        """关闭 SQLite 连接。"""

        with self._lock:
            self._conn.close()

    def _ensure_schema(self) -> None:
        """创建当前版本 schema。

        本项目不再兼容旧任务/旧配置结构；schema_version 不匹配时会清空任务、
        项目配置、AI 配置和已处理消息，然后按当前版本重建。
        """

        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS global_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            stored_schema_version = self._schema_version_locked()
            should_rebuild_task_data = stored_schema_version != APP_SCHEMA_VERSION
            if should_rebuild_task_data:
                self._conn.execute("DELETE FROM global_settings WHERE key != ?", ("schema_version",))
                self._conn.execute("DROP TABLE IF EXISTS repair_tasks")
                self._conn.execute("DROP TABLE IF EXISTS processed_messages")
                self._conn.execute("DROP TABLE IF EXISTS project_configs")
                self._conn.execute("DROP TABLE IF EXISTS ai_configs")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    name TEXT NOT NULL DEFAULT '',
                    cli_type TEXT NOT NULL DEFAULT 'codex',
                    cli_path TEXT NOT NULL DEFAULT '',
                    ai_provider TEXT NOT NULL DEFAULT 'gpt',
                    model_name TEXT NOT NULL DEFAULT '',
                    api_key_env TEXT NOT NULL DEFAULT '',
                    config_home TEXT NOT NULL DEFAULT '',
                    config_home_env TEXT NOT NULL DEFAULT '',
                    extra_args TEXT NOT NULL DEFAULT '',
                    command_template TEXT NOT NULL DEFAULT '',
                    self_check_status TEXT NOT NULL DEFAULT 'unchecked',
                    self_check_message TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    ai_config_id INTEGER NOT NULL DEFAULT 0,
                    chat_name TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    app_name TEXT NOT NULL DEFAULT '',
                    base_name TEXT NOT NULL DEFAULT '',
                    status_field_name TEXT NOT NULL DEFAULT '状态',
                    done_status_value TEXT NOT NULL DEFAULT 'AI修复完成',
                    repo_root TEXT NOT NULL DEFAULT '',
                    base_branch TEXT NOT NULL DEFAULT '',
                    branch_prefix TEXT NOT NULL DEFAULT 'fix/base-',
                    worktree_root TEXT NOT NULL DEFAULT '',
                    workroot_name TEXT NOT NULL DEFAULT '',
                    processable_status_values TEXT NOT NULL DEFAULT '',
                    repair_version TEXT NOT NULL DEFAULT '',
                    auto_repair_mode TEXT NOT NULL DEFAULT 'all_day',
                    auto_repair_start_time TEXT NOT NULL DEFAULT '00:00',
                    auto_repair_end_day_offset INTEGER NOT NULL DEFAULT 0,
                    auto_repair_end_time TEXT NOT NULL DEFAULT '23:59',
                    context_collect_commands TEXT NOT NULL DEFAULT 'git status --short
git log --oneline -20
rg -n -- "{{issue_summary}}" .',
                    verify_commands TEXT NOT NULL DEFAULT 'git diff --check',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repair_tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_id INTEGER NOT NULL,
                    ai_config_id INTEGER NOT NULL DEFAULT 0,
                    ai_config_name TEXT NOT NULL DEFAULT '',
                    cli_type TEXT NOT NULL DEFAULT 'codex',
                    cli_path TEXT NOT NULL DEFAULT '',
                    ai_provider TEXT NOT NULL DEFAULT '',
                    ai_model TEXT NOT NULL DEFAULT '',
                    workroot_name TEXT NOT NULL,
                    chat_name TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    base_token TEXT NOT NULL,
                    table_id TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    repo_root TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    branch_prefix TEXT NOT NULL,
                    status_field_name TEXT NOT NULL,
                    done_status_value TEXT NOT NULL,
                    processable_status_values TEXT NOT NULL DEFAULT '',
                    status_value TEXT NOT NULL DEFAULT '',
                    problem_description TEXT NOT NULL DEFAULT '',
                    repair_version TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    prompt_text TEXT NOT NULL DEFAULT '',
                    ai_reply_text TEXT NOT NULL DEFAULT '',
                    context_snapshot TEXT NOT NULL DEFAULT '',
                    verify_output TEXT NOT NULL DEFAULT '',
                    repair_thinking_summary TEXT NOT NULL DEFAULT '',
                    round_action_summary TEXT NOT NULL DEFAULT '',
                    round_validation_summary TEXT NOT NULL DEFAULT '',
                    round_risk_summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    feishu_received_at INTEGER NOT NULL DEFAULT 0,
                    feishu_received_text TEXT NOT NULL DEFAULT '',
                    feishu_received_error TEXT NOT NULL DEFAULT '',
                    feishu_result_at INTEGER NOT NULL DEFAULT 0,
                    feishu_result_text TEXT NOT NULL DEFAULT '',
                    feishu_result_error TEXT NOT NULL DEFAULT '',
                    round_count INTEGER NOT NULL DEFAULT 0,
                    round_history_json TEXT NOT NULL DEFAULT '[]',
                    commit_sha TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    UNIQUE(config_id, message_id),
                    UNIQUE(config_id, branch_name)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    config_id INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    replied_at INTEGER NOT NULL,
                    reply_text TEXT NOT NULL,
                    PRIMARY KEY (config_id, message_id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_configs_enabled ON project_configs(enabled)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_configs_enabled ON ai_configs(enabled)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_repair_tasks_config_id ON repair_tasks(config_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_repair_tasks_status ON repair_tasks(status)"
            )
            self._ensure_default_ai_config_locked()
            self._set_schema_version_locked(APP_SCHEMA_VERSION)
            self._conn.commit()

    def _schema_version_locked(self) -> int:
        """在已持有锁的情况下读取当前 schema 版本。"""

        row = self._conn.execute(
            "SELECT value FROM global_settings WHERE key = ? LIMIT 1",
            ("schema_version",),
        ).fetchone()
        return parse_non_negative_int(row["value"] if row else None, 0)

    def _set_schema_version_locked(self, version: int) -> None:
        """在已持有锁的情况下写入 schema 版本。"""

        self._conn.execute(
            "INSERT OR REPLACE INTO global_settings(key, value) VALUES (?, ?)",
            ("schema_version", str(parse_non_negative_int(version, APP_SCHEMA_VERSION))),
        )

    def notify_changed(self) -> None:
        """通知 SSE/轮询页面：任务或配置状态已经变化。"""

        with self._change_condition:
            self._change_version += 1
            self._change_condition.notify_all()

    def change_version(self) -> int:
        """返回当前变更版本号，供 SSE 连接记录游标。"""

        with self._change_condition:
            return self._change_version

    def wait_for_change(self, after_version: int, timeout: float = 25.0) -> int:
        """阻塞等待 change_version 增长。

        如果 timeout 内没有变化，返回当前版本；SSE 用它发送 ping 保持连接。
        """

        deadline = time.monotonic() + max(0.1, timeout)
        with self._change_condition:
            while self._change_version <= after_version:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._change_version
                self._change_condition.wait(remaining)
            return self._change_version

    def _ensure_default_ai_config_locked(self) -> None:
        """确保空库里至少有一组默认 Codex AI 配置。"""

        row = self._conn.execute("SELECT COUNT(*) AS count FROM ai_configs").fetchone()
        if row is not None and int(row["count"]) > 0:
            return
        codex_bin = resolve_codex_bin()
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO ai_configs (
                enabled, name, cli_type, cli_path, ai_provider, model_name, api_key_env,
                config_home, config_home_env, extra_args, command_template,
                self_check_status, self_check_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "默认 Codex",
                "codex",
                codex_bin,
                "gpt",
                "",
                "",
                "",
                "CODEX_HOME",
                "",
                "",
                "unchecked",
                "系统初始化生成",
                now,
                now,
            ),
        )

    def get_global_settings(self) -> GlobalSettings:
        """读取全局设置，并对非法值应用安全默认值。"""

        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM global_settings").fetchall()
        data = {str(row["key"]): str(row["value"]) for row in rows}
        max_concurrent = parse_positive_int(data.get("max_concurrent"), DEFAULT_MAX_CONCURRENT)
        timeout_seconds = parse_positive_int(data.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS)
        repair_max_loops = min(
            MAX_REPAIR_MAX_LOOPS,
            parse_positive_int(data.get("repair_max_loops"), DEFAULT_REPAIR_MAX_LOOPS),
        )
        return GlobalSettings(
            max_concurrent=max_concurrent,
            timeout_seconds=timeout_seconds,
            repair_max_loops=repair_max_loops,
        )

    def save_global_settings(self, max_concurrent: int, timeout_seconds: int, repair_max_loops: int) -> None:
        """保存全局并发、超时和最大循环次数。"""

        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO global_settings(key, value) VALUES (?, ?)",
                [
                    ("max_concurrent", str(parse_positive_int(max_concurrent, DEFAULT_MAX_CONCURRENT))),
                    ("timeout_seconds", str(parse_positive_int(timeout_seconds, DEFAULT_TIMEOUT_SECONDS))),
                    ("repair_max_loops", str(min(MAX_REPAIR_MAX_LOOPS, parse_positive_int(repair_max_loops, DEFAULT_REPAIR_MAX_LOOPS)))),
                ],
            )
            self._conn.commit()

    def get_repair_prompt_template(self) -> str:
        """读取首次 Prompt；未配置时返回内置默认文案。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM global_settings WHERE key = ? LIMIT 1",
                ("repair_prompt_template",),
            ).fetchone()
        if row is None:
            return DEFAULT_REPAIR_PROMPT_TEMPLATE
        value = str(row["value"]).strip()
        return value or DEFAULT_REPAIR_PROMPT_TEMPLATE

    def save_repair_prompt_template(self, prompt_template: str) -> None:
        """保存首次 Prompt；空文本表示恢复内置默认文案。"""

        value = prompt_template.strip() or DEFAULT_REPAIR_PROMPT_TEMPLATE
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO global_settings(key, value) VALUES (?, ?)",
                ("repair_prompt_template", value),
            )
            self._conn.commit()

    def get_followup_prompt_template(self) -> str:
        """读取二次 Prompt；未配置时返回内置默认文案。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM global_settings WHERE key = ? LIMIT 1",
                ("repair_followup_prompt_template",),
            ).fetchone()
        if row is None:
            return DEFAULT_REPAIR_FOLLOWUP_PROMPT_TEMPLATE
        value = str(row["value"]).strip()
        return value or DEFAULT_REPAIR_FOLLOWUP_PROMPT_TEMPLATE

    def save_followup_prompt_template(self, prompt_template: str) -> None:
        """保存二次 Prompt；空文本表示恢复内置默认文案。"""

        value = prompt_template.strip() or DEFAULT_REPAIR_FOLLOWUP_PROMPT_TEMPLATE
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO global_settings(key, value) VALUES (?, ?)",
                ("repair_followup_prompt_template", value),
            )
            self._conn.commit()

    def list_ai_configs(self) -> list[AIConfig]:
        """列出所有 AI 配置组，启用项排在前面。"""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ai_configs ORDER BY enabled DESC, id ASC"
            ).fetchall()
        return [self._row_to_ai_config(row) for row in rows]

    def list_enabled_ai_configs(self) -> list[AIConfig]:
        """列出当前可供项目绑定的启用 AI 配置组。"""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ai_configs WHERE enabled = 1 ORDER BY id ASC"
            ).fetchall()
        return [self._row_to_ai_config(row) for row in rows]

    def get_ai_config(self, ai_config_id: int) -> AIConfig | None:
        """按 ID 读取 AI 配置组。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ai_configs WHERE id = ? LIMIT 1",
                (ai_config_id,),
            ).fetchone()
        return self._row_to_ai_config(row) if row else None

    def get_default_ai_config(self) -> AIConfig | None:
        """返回默认 AI 配置组：优先启用项，否则取第一条。"""

        configs = self.list_enabled_ai_configs() or self.list_ai_configs()
        return configs[0] if configs else None

    def upsert_ai_config(self, ai_config_id: int | None, values: dict[str, Any]) -> int:
        """新增或更新 AI 配置组。

        ai_config_id 为 None 时插入新配置；否则更新现有配置。返回最终配置 ID。
        """

        now = int(time.time())
        cli_type = str(values.get("cli_type") or "custom").strip() or "custom"
        row_values = {
            "enabled": 1 if values.get("enabled") else 0,
            "name": str(values.get("name") or "").strip() or default_ai_config_name(cli_type, str(values.get("model_name") or "")),
            "cli_type": cli_type,
            "cli_path": str(values.get("cli_path") or "").strip(),
            "ai_provider": str(values.get("ai_provider") or "custom").strip() or "custom",
            "model_name": str(values.get("model_name") or "").strip(),
            "api_key_env": str(values.get("api_key_env") or "").strip(),
            "config_home": str(values.get("config_home") or "").strip(),
            "config_home_env": str(values.get("config_home_env") or default_config_home_env(cli_type)).strip(),
            "extra_args": str(values.get("extra_args") or "").strip(),
            "command_template": str(values.get("command_template") or "").strip(),
            "self_check_status": str(values.get("self_check_status") or "unchecked").strip() or "unchecked",
            "self_check_message": str(values.get("self_check_message") or "").strip(),
            "updated_at": now,
        }
        if ai_config_id is None:
            row_values["created_at"] = now
            with self._lock:
                cursor = self._conn.execute(
                    """
                    INSERT INTO ai_configs (
                        enabled, name, cli_type, cli_path, ai_provider, model_name, api_key_env,
                        config_home, config_home_env, extra_args, command_template,
                        self_check_status, self_check_message, created_at, updated_at
                    ) VALUES (:enabled, :name, :cli_type, :cli_path, :ai_provider, :model_name, :api_key_env,
                              :config_home, :config_home_env, :extra_args, :command_template,
                              :self_check_status, :self_check_message, :created_at, :updated_at)
                    """,
                    row_values,
                )
                self._conn.commit()
                return int(cursor.lastrowid)

        row_values["id"] = ai_config_id
        assignments = ", ".join(f"{key} = :{key}" for key in row_values)
        with self._lock:
            self._conn.execute(
                f"UPDATE ai_configs SET {assignments} WHERE id = :id",
                row_values,
            )
            self._conn.commit()
        return ai_config_id

    def delete_ai_config(self, ai_config_id: int) -> None:
        """删除 AI 配置组，并把绑定它的项目改绑到可用 fallback。"""

        fallback_id = 0
        for config in self.list_enabled_ai_configs() or self.list_ai_configs():
            if config.id != ai_config_id:
                fallback_id = config.id
                break
        with self._lock:
            self._conn.execute(
                "UPDATE project_configs SET ai_config_id = ? WHERE ai_config_id = ?",
                (fallback_id, ai_config_id),
            )
            self._conn.execute("DELETE FROM ai_configs WHERE id = ?", (ai_config_id,))
            self._conn.commit()

    def update_ai_config_self_check(self, ai_config_id: int, status: str, message: str) -> None:
        """更新 AI 配置组自检结果。"""

        with self._lock:
            self._conn.execute(
                """
                UPDATE ai_configs
                SET self_check_status = ?, self_check_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, message, int(time.time()), ai_config_id),
            )
            self._conn.commit()

    def scan_cli_configs(self) -> int:
        """扫描当前机器上的常见 AI CLI，并为新发现的 CLI 创建候选配置组。"""

        created = 0
        existing = {
            (config.cli_type, str(Path(config.cli_path).expanduser()))
            for config in self.list_ai_configs()
            if config.cli_path
        }
        for cli_type, _ in AI_CONFIG_CLI_TYPES:
            if cli_type == "custom":
                continue
            cli_path = discover_cli_path(cli_type)
            if not cli_path:
                continue
            key = (cli_type, str(Path(cli_path).expanduser()))
            if key in existing:
                continue
            self.upsert_ai_config(
                None,
                {
                    "enabled": True,
                    "name": default_ai_config_name(cli_type, ""),
                    "cli_type": cli_type,
                    "cli_path": cli_path,
                    "ai_provider": default_ai_provider(cli_type),
                    "model_name": "",
                    "config_home_env": default_config_home_env(cli_type),
                    "self_check_status": "unchecked",
                    "self_check_message": "自动搜索发现，尚未自检",
                },
            )
            existing.add(key)
            created += 1
        return created

    def list_configs(self) -> list[ProjectConfig]:
        """列出项目配置，启用项排在前面。"""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM project_configs ORDER BY enabled DESC, id DESC"
            ).fetchall()
        return [self._row_to_config(row) for row in rows]

    def get_config(self, config_id: int) -> ProjectConfig | None:
        """按 ID 读取项目配置。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_configs WHERE id = ? LIMIT 1",
                (config_id,),
            ).fetchone()
        return self._row_to_config(row) if row else None

    def create_blank_config(self) -> int:
        """创建一条带默认值的空项目配置，供编辑页继续填写。"""

        now = int(time.time())
        default_ai_config = self.get_default_ai_config()
        default_ai_config_id = default_ai_config.id if default_ai_config else 0
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO project_configs (
                    enabled, ai_config_id, chat_name, chat_id, app_name, status_field_name,
                    base_name, done_status_value, repo_root, base_branch, branch_prefix,
                    worktree_root, workroot_name, processable_status_values, repair_version,
                    auto_repair_mode, auto_repair_start_time, auto_repair_end_day_offset, auto_repair_end_time,
                    context_collect_commands, verify_commands,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    0,
                    default_ai_config_id,
                    "",
                    "",
                    "",
                    DEFAULT_STATUS_FIELD,
                    "",
                    DEFAULT_DONE_STATUS,
                    "",
                    "",
                    DEFAULT_BRANCH_PREFIX,
                    "",
                    "",
                    format_status_values(DEFAULT_PROCESSABLE_STATUSES),
                    "",
                    DEFAULT_AUTO_REPAIR_MODE,
                    DEFAULT_AUTO_REPAIR_START_TIME,
                    DEFAULT_AUTO_REPAIR_END_DAY_OFFSET,
                    DEFAULT_AUTO_REPAIR_END_TIME,
                    DEFAULT_CONTEXT_COLLECT_COMMANDS,
                    DEFAULT_VERIFY_COMMANDS,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def upsert_config(self, config_id: int | None, values: dict[str, Any]) -> int:
        """新增或更新项目配置。

        会统一归一化状态列表、时间段、分支前缀、上下文命令和验证命令。
        """

        now = int(time.time())
        default_ai_config = self.get_default_ai_config()
        row_values = {
            "enabled": 1 if values.get("enabled") else 0,
            "ai_config_id": parse_non_negative_int(values.get("ai_config_id"), default_ai_config.id if default_ai_config else 0),
            "chat_name": str(values.get("chat_name") or "").strip(),
            "chat_id": str(values.get("chat_id") or "").strip(),
            "app_name": str(values.get("app_name") or "").strip(),
            "base_name": str(values.get("base_name") or "").strip(),
            "status_field_name": str(values.get("status_field_name") or DEFAULT_STATUS_FIELD).strip() or DEFAULT_STATUS_FIELD,
            "done_status_value": str(values.get("done_status_value") or DEFAULT_DONE_STATUS).strip() or DEFAULT_DONE_STATUS,
            "repo_root": str(values.get("repo_root") or "").strip(),
            "base_branch": str(values.get("base_branch") or "").strip(),
            "branch_prefix": str(values.get("branch_prefix") or DEFAULT_BRANCH_PREFIX).strip() or DEFAULT_BRANCH_PREFIX,
            "worktree_root": str(values.get("worktree_root") or "").strip(),
            "workroot_name": str(values.get("workroot_name") or "").strip(),
            "processable_status_values": format_status_values(
                parse_status_values(values.get("processable_status_values")) or DEFAULT_PROCESSABLE_STATUSES
            ),
            "repair_version": str(values.get("repair_version") or "").strip(),
            "auto_repair_mode": normalize_auto_repair_mode(values.get("auto_repair_mode")),
            "auto_repair_start_time": normalize_hhmm(values.get("auto_repair_start_time"), DEFAULT_AUTO_REPAIR_START_TIME),
            "auto_repair_end_day_offset": parse_end_day_offset(values.get("auto_repair_end_day_offset")),
            "auto_repair_end_time": normalize_hhmm(values.get("auto_repair_end_time"), DEFAULT_AUTO_REPAIR_END_TIME),
            "context_collect_commands": str(values.get("context_collect_commands", DEFAULT_CONTEXT_COLLECT_COMMANDS) or "").strip(),
            "verify_commands": str(values.get("verify_commands", DEFAULT_VERIFY_COMMANDS) or "").strip(),
            "updated_at": now,
        }

        if config_id is None:
            row_values["created_at"] = now
            with self._lock:
                cursor = self._conn.execute(
                    """
                    INSERT INTO project_configs (
                        enabled, ai_config_id, chat_name, chat_id, app_name, status_field_name,
                        base_name, done_status_value, repo_root, base_branch, branch_prefix,
                        worktree_root, workroot_name, processable_status_values, repair_version,
                        auto_repair_mode, auto_repair_start_time, auto_repair_end_day_offset, auto_repair_end_time,
                        context_collect_commands, verify_commands,
                        created_at, updated_at
                    ) VALUES (:enabled, :ai_config_id, :chat_name, :chat_id, :app_name, :status_field_name,
                              :base_name, :done_status_value, :repo_root, :base_branch, :branch_prefix,
                              :worktree_root, :workroot_name, :processable_status_values, :repair_version,
                              :auto_repair_mode, :auto_repair_start_time, :auto_repair_end_day_offset, :auto_repair_end_time,
                              :context_collect_commands, :verify_commands,
                              :created_at, :updated_at)
                    """,
                    row_values,
                )
                self._conn.commit()
                return int(cursor.lastrowid)

        assignments = ", ".join(f"{key} = :{key}" for key in row_values)
        row_values["id"] = config_id
        with self._lock:
            self._conn.execute(
                f"UPDATE project_configs SET {assignments} WHERE id = :id",
                row_values,
            )
            self._conn.commit()
        return config_id

    def delete_config(self, config_id: int) -> None:
        """删除一个项目配置；历史任务不在这里级联删除。"""

        with self._lock:
            self._conn.execute("DELETE FROM project_configs WHERE id = ?", (config_id,))
            self._conn.commit()

    def set_config_enabled(self, config_id: int, enabled: bool) -> None:
        """切换项目配置激活状态。"""

        with self._lock:
            self._conn.execute(
                "UPDATE project_configs SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, int(time.time()), config_id),
            )
            self._conn.commit()

    def update_config_chat_id(self, config_id: int, chat_id: str) -> None:
        """保存通过群名自动解析出的 chat_id。"""

        with self._lock:
            self._conn.execute(
                "UPDATE project_configs SET chat_id = ?, updated_at = ? WHERE id = ?",
                (chat_id, int(time.time()), config_id),
            )
            self._conn.commit()

    def list_tasks(self, *, limit: int = 200) -> list[TaskRow]:
        """按创建时间倒序列出任务历史。"""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM repair_tasks ORDER BY created_at DESC, task_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_task(self, task_id: int) -> TaskRow | None:
        """按任务 ID 读取任务。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM repair_tasks WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def get_task_by_config_message(self, config_id: int, message_id: str) -> TaskRow | None:
        """按项目配置和飞书 message_id 查找任务，用于重复消息去重。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM repair_tasks WHERE config_id = ? AND message_id = ? LIMIT 1",
                (config_id, message_id),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list_paused_tasks_for_config(self, config_id: int, *, limit: int = 200) -> list[TaskRow]:
        """列出某配置下等待自动修复时间段释放的暂停任务。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM repair_tasks
                WHERE config_id = ? AND status = '暂停'
                ORDER BY created_at ASC, task_id ASC
                LIMIT ?
                """,
                (config_id, limit),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def insert_task(self, task: TaskRow | dict[str, Any]) -> int:
        """插入一条修复任务，并通知页面刷新。"""

        now = int(time.time())
        if isinstance(task, TaskRow):
            values = dataclasses.asdict(task)
        else:
            values = dict(task)
        values.setdefault("prompt_text", "")
        values.setdefault("ai_reply_text", "")
        values.setdefault("context_snapshot", "")
        values.setdefault("verify_output", "")
        values.setdefault("repair_thinking_summary", "")
        values.setdefault("round_action_summary", "")
        values.setdefault("round_validation_summary", "")
        values.setdefault("round_risk_summary", "")
        values.setdefault("round_count", 0)
        values.setdefault("round_history_json", "[]")
        values.setdefault("commit_sha", None)
        values.setdefault("error", None)
        values.setdefault("created_at", now)
        values.setdefault("started_at", None)
        values.setdefault("finished_at", None)
        columns = ", ".join(values.keys())
        placeholders = ", ".join(f":{key}" for key in values.keys())
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT INTO repair_tasks ({columns}) VALUES ({placeholders})",
                values,
            )
            self._conn.commit()
            task_id = int(cursor.lastrowid)
        self.notify_changed()
        return task_id

    def update_task(self, task_id: int, **values: Any) -> None:
        """局部更新任务字段，并通知页面刷新。"""

        if not values:
            return
        values["task_id"] = task_id
        assignments = ", ".join(f"{key} = :{key}" for key in values if key != "task_id")
        with self._lock:
            self._conn.execute(
                f"UPDATE repair_tasks SET {assignments} WHERE task_id = :task_id",
                values,
            )
            self._conn.commit()
        self.notify_changed()

    def delete_task(self, task_id: int) -> None:
        """删除任务记录，并通知页面刷新。"""

        with self._lock:
            self._conn.execute("DELETE FROM repair_tasks WHERE task_id = ?", (task_id,))
            self._conn.commit()
        self.notify_changed()

    def is_processed(self, config_id: int, message_id: str) -> bool:
        """判断某条飞书消息是否已经被指定项目配置处理过。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed_messages WHERE config_id = ? AND message_id = ? LIMIT 1",
                (config_id, message_id),
            ).fetchone()
        return row is not None

    def mark_processed(self, config_id: int, event: Any, reply_text: str) -> None:
        """记录飞书消息已处理，防止轮询或事件流重复入队。"""

        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO processed_messages (config_id, message_id, chat_id, replied_at, reply_text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (config_id, event.message_id, event.chat_id, int(time.time()), reply_text),
            )
            self._conn.commit()

    def _row_to_ai_config(self, row: sqlite3.Row) -> AIConfig:
        """把 ai_configs 查询结果转换成 AIConfig。"""

        return AIConfig(
            id=int(row["id"]),
            enabled=bool(row["enabled"]),
            name=str(row["name"]),
            cli_type=str(row["cli_type"]),
            cli_path=str(row["cli_path"]),
            ai_provider=str(row["ai_provider"]),
            model_name=str(row["model_name"]),
            api_key_env=str(row["api_key_env"]),
            config_home=str(row["config_home"]),
            config_home_env=str(row["config_home_env"]),
            extra_args=str(row["extra_args"]),
            command_template=str(row["command_template"]),
            self_check_status=str(row["self_check_status"]),
            self_check_message=str(row["self_check_message"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def _row_to_config(self, row: sqlite3.Row | None) -> ProjectConfig | None:
        """把 project_configs 查询结果转换成 ProjectConfig。"""

        if row is None:
            return None
        return ProjectConfig(
            id=int(row["id"]),
            enabled=bool(row["enabled"]),
            ai_config_id=int(row["ai_config_id"]),
            chat_name=str(row["chat_name"]),
            chat_id=str(row["chat_id"]),
            app_name=str(row["app_name"]),
            base_name=str(row["base_name"]),
            status_field_name=str(row["status_field_name"]),
            done_status_value=str(row["done_status_value"]),
            repo_root=str(row["repo_root"]),
            base_branch=str(row["base_branch"]),
            branch_prefix=str(row["branch_prefix"]),
            worktree_root=str(row["worktree_root"]),
            workroot_name=str(row["workroot_name"]),
            processable_status_values=parse_status_values(str(row["processable_status_values"])),
            repair_version=str(row["repair_version"]),
            auto_repair_mode=normalize_auto_repair_mode(row["auto_repair_mode"]),
            auto_repair_start_time=normalize_hhmm(row["auto_repair_start_time"], DEFAULT_AUTO_REPAIR_START_TIME),
            auto_repair_end_day_offset=parse_end_day_offset(row["auto_repair_end_day_offset"]),
            auto_repair_end_time=normalize_hhmm(row["auto_repair_end_time"], DEFAULT_AUTO_REPAIR_END_TIME),
            context_collect_commands=str(row["context_collect_commands"]),
            verify_commands=str(row["verify_commands"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def _row_to_task(self, row: sqlite3.Row) -> TaskRow:
        """把 repair_tasks 查询结果转换成 TaskRow。"""

        return TaskRow(
            task_id=int(row["task_id"]),
            config_id=int(row["config_id"]),
            ai_config_id=int(row["ai_config_id"]),
            ai_config_name=str(row["ai_config_name"]),
            cli_type=str(row["cli_type"]),
            cli_path=str(row["cli_path"]),
            ai_provider=str(row["ai_provider"]),
            ai_model=str(row["ai_model"]),
            workroot_name=str(row["workroot_name"]),
            chat_name=str(row["chat_name"]),
            chat_id=str(row["chat_id"]),
            message_id=str(row["message_id"]),
            record_id=str(row["record_id"]),
            base_token=str(row["base_token"]),
            table_id=str(row["table_id"]),
            branch_name=str(row["branch_name"]),
            worktree_path=str(row["worktree_path"]),
            repo_root=str(row["repo_root"]),
            base_branch=str(row["base_branch"]),
            branch_prefix=str(row["branch_prefix"]),
            status_field_name=str(row["status_field_name"]),
            done_status_value=str(row["done_status_value"]),
            processable_status_values=str(row["processable_status_values"]),
            status_value=str(row["status_value"]),
            problem_description=str(row["problem_description"]),
            repair_version=str(row["repair_version"]),
            payload_json=str(row["payload_json"]),
            prompt_text=str(row["prompt_text"]),
            ai_reply_text=str(row["ai_reply_text"]),
            context_snapshot=str(row["context_snapshot"]),
            verify_output=str(row["verify_output"]),
            repair_thinking_summary=str(row["repair_thinking_summary"]),
            round_action_summary=str(row["round_action_summary"]),
            round_validation_summary=str(row["round_validation_summary"]),
            round_risk_summary=str(row["round_risk_summary"]),
            status=str(row["status"]),
            summary=str(row["summary"]),
            feishu_received_at=parse_non_negative_int(row["feishu_received_at"], 0),
            feishu_received_text=str(row["feishu_received_text"]),
            feishu_received_error=str(row["feishu_received_error"]),
            feishu_result_at=parse_non_negative_int(row["feishu_result_at"], 0),
            feishu_result_text=str(row["feishu_result_text"]),
            feishu_result_error=str(row["feishu_result_error"]),
            round_count=int(row["round_count"]),
            round_history_json=str(row["round_history_json"]),
            commit_sha=row["commit_sha"],
            error=row["error"],
            created_at=int(row["created_at"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
