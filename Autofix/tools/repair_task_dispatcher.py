#!/usr/bin/env python3
"""旧版单配置自动修复 dispatcher。

当前主流程已经迁移到 ``autofix_manager.py`` 的多配置管理台；本模块保留给
``feishu_echo_listener.py`` 的独立监听模式使用。它的职责是：收到一条 Base
记录任务后创建 worktree、调用 Codex CLI、提交修改并回写 Base 状态。

调用示例：

    dispatcher = RepairTaskDispatcher(repo_root=Path.cwd(), state_db=Path("echo.sqlite3"))
    dispatcher.dispatch(RepairTaskRequest(...))

新功能优先加在多配置管理台里；这里的注释重点说明旧 API 如何正确调用。
"""

from __future__ import annotations

import dataclasses
import ast
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import textwrap
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MAX_CONCURRENT_TASKS = 2
DEFAULT_WORKTREE_ROOT = Path(tempfile.gettempdir()) / "testautofix-worktrees"
DEFAULT_CODEX_TIMEOUT = 1800
DEFAULT_REPAIR_STATUS = "AI修复完成"
DEFAULT_CODEX_BIN_CANDIDATES: list[Path] = []


@dataclass(frozen=True)
class RepairTaskRequest:
    """旧 dispatcher 接收的一条修复请求。"""

    message_id: str
    chat_id: str
    record_id: str
    base_token: str
    table_id: str
    fields: dict[str, Any]
    source_message: str
    issue_summary: str


@dataclass(frozen=True)
class RepairTaskRow:
    """旧 dispatcher 数据库中的任务行快照。"""

    task_id: int
    message_id: str
    chat_id: str
    record_id: str
    base_token: str
    table_id: str
    branch_name: str
    worktree_path: str
    payload_json: str
    status: str
    summary: str
    commit_sha: str | None
    error: str | None
    created_at: int
    started_at: int | None
    finished_at: int | None


@dataclass(frozen=True)
class RepairTaskResult:
    """旧 dispatcher 单次修复执行结果。"""

    branch_name: str
    worktree_path: str
    commit_sha: str | None
    summary: str
    error: str | None


class BaseStatusUpdateError(RuntimeError):
    """表示代码已提交但 Base 状态回写失败。

    上层捕获该异常后会把任务标记为 ``base_update_failed``，保留 commit_sha，
    以便后续只重试 Base 状态回写。
    """

    def __init__(self, commit_sha: str, message: str) -> None:
        """保存已产生的 commit SHA，并构造可读错误信息。"""
        super().__init__(f"code committed as {commit_sha}, but failed to update Base status: {message}")
        self.commit_sha = commit_sha


def run_checked(args: list[str], *, cwd: Path | None = None, capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess[str]:
    """执行外部命令并要求退出码为 0。"""
    return subprocess.run(args, check=True, cwd=cwd, capture_output=capture_output, text=text)


def clean_subprocess_text(value: Any) -> str:
    """把 subprocess 异常里的 bytes/stdout/stderr 清理成可读文本。"""

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


def summarize_lark_cli_error(raw_message: str) -> str:
    """把 lark-cli 的 JSON 错误压缩成人类可读的一行摘要。"""
    raw_message = raw_message.strip()
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        return raw_message

    error = payload.get("error")
    if not isinstance(error, dict):
        return json.dumps(payload, ensure_ascii=False)

    parts: list[str] = []
    identity = payload.get("identity")
    if identity:
        parts.append(f"identity={identity}")
    for key in ("type", "subtype", "code", "message", "log_id", "hint"):
        value = error.get(key)
        if value:
            parts.append(f"{key}={value}")
    missing_scopes = error.get("missing_scopes")
    if missing_scopes:
        parts.append(f"missing_scopes={missing_scopes}")
    return "; ".join(parts) or json.dumps(error, ensure_ascii=False)


def run_base_json(args: list[str]) -> dict[str, Any]:
    """执行 Base 相关 lark-cli 命令，并返回 ``ok=true`` 的 JSON。"""
    try:
        result = run_checked(args)
    except subprocess.CalledProcessError as exc:
        message = summarize_lark_cli_error(exc.stderr or exc.stdout or str(exc))
        raise RuntimeError(f"base command failed: {' '.join(args)}: {message}") from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"base command returned invalid json: {' '.join(args)}: {exc}") from exc
    if not payload.get("ok", False):
        message = summarize_lark_cli_error(json.dumps(payload, ensure_ascii=False))
        raise RuntimeError(f"base command not ok: {' '.join(args)}: {message}")
    return payload


def sanitize_branch_suffix(value: str) -> str:
    """把 record_id/message_id 转成适合放入 Git 分支或目录名的片段。"""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return slug or "task"


def build_commit_message(record_id: str, issue_summary: str) -> str:
    """基于 Base 记录和问题摘要生成 72 字符以内的提交标题。"""
    head = f"Base {record_id}"
    summary = " ".join(issue_summary.split())
    if summary:
        message = f"{head}: {summary}"
    else:
        message = head
    return message[:72]


def log_task_status(task_id: int, record_id: str, status: str, message: str) -> None:
    """输出单行任务状态日志。"""
    print(f"[repair-task:{task_id}][record_id={record_id}] {status} {message}", file=sys.stderr, flush=True)


def log_task_multiline(task_id: int, record_id: str, label: str, text: str) -> None:
    """输出多行任务日志，并给每一行都补齐任务前缀。"""
    lines = text.splitlines() or [""]
    for line in lines:
        print(f"[repair-task:{task_id}][record_id={record_id}] {label} {line}", file=sys.stderr, flush=True)


def build_repair_prompt(task: RepairTaskRequest, *, branch_name: str, worktree_path: Path) -> str:
    """构建旧流程的一次性 Codex 修复 Prompt。"""
    fields_json = json.dumps(task.fields, ensure_ascii=False, indent=2, sort_keys=True)
    return textwrap.dedent(
        f"""
        你在一个已经准备好的 git worktree 中工作。请修复当前仓库里的这个 bug，并只修改必要的项目文件。

        约束：
        - 不要创建分支，不要创建 worktree
        - 只修改 worktree_path 目录下的文件，不要修改原始仓库 checkout
        - 尽量保持改动最小
        - 如需新增或修改测试，只做能验证这次修复的最小集合
        - 完成后把需要的文件修改好，不要提交

        任务信息：
        - record_id: {task.record_id}
        - branch_name: {branch_name}
        - worktree_path: {worktree_path}
        - Base 摘要: {task.issue_summary}
        - 原始消息: {task.source_message}

        Base 行字段：
        {fields_json}

        输出要求：
        - 修改完成后，简要说明你改了什么
        - 如果发现问题无法修复，也要说明原因
        """
    ).strip()


def resolve_codex_bin() -> str:
    """解析可执行的 Codex CLI 路径。

    优先级：``CODEX_BIN`` 环境变量 -> PATH 中的 ``codex`` -> macOS ChatGPT
    App 内置路径。全部失败时抛错，让调用者明确配置。
    """
    configured = os.environ.get("CODEX_BIN")
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            return str(path)
        raise RuntimeError(f"CODEX_BIN does not exist: {path}")

    from_path = shutil.which("codex")
    if from_path:
        return from_path

    for candidate in DEFAULT_CODEX_BIN_CANDIDATES:
        if candidate.exists():
            return str(candidate)

    raise RuntimeError(
        "codex executable not found. Install the Codex CLI, add it to PATH, "
        "or start the listener with CODEX_BIN=/path/to/codex."
    )


class RepairTaskDispatcher:
    """旧版单仓库任务调度器。

    它维护自己的 SQLite 表、worker 队列和并发数。与新管理台不同，这里只面向
    一个仓库、一个 Codex CLI、一个 Base 完成状态。
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        state_db: Path,
        max_concurrent_tasks: int = DEFAULT_MAX_CONCURRENT_TASKS,
        worktree_root: Path = DEFAULT_WORKTREE_ROOT,
        status_field_id: str = "fld2taQTh4",
        status_value: str = DEFAULT_REPAIR_STATUS,
        base_update_identity: str | None = None,
    ) -> None:
        """初始化旧 dispatcher 的仓库、数据库、并发 worker 和 Base 回写身份。"""
        self.repo_root = repo_root
        self.state_db = state_db
        self.worktree_root = worktree_root / repo_root.name
        self.max_concurrent_tasks = max_concurrent_tasks
        self.status_field_id = status_field_id
        self.status_value = status_value
        self.base_update_identity = base_update_identity or os.environ.get("FEISHU_BASE_UPDATE_AS", "bot")
        if self.base_update_identity not in {"bot", "user"}:
            raise ValueError("base_update_identity must be 'bot' or 'user'")
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._db_lock = threading.Lock()
        self._conn = sqlite3.connect(state_db, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._workers = [
            threading.Thread(target=self._worker_loop, name=f"repair-worker-{index+1}", daemon=True)
            for index in range(max_concurrent_tasks)
        ]
        for worker in self._workers:
            worker.start()

    def _ensure_schema(self) -> None:
        """创建旧 dispatcher 的任务表和索引。"""
        with self._db_lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repair_tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    chat_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    base_token TEXT NOT NULL,
                    table_id TEXT NOT NULL,
                    branch_name TEXT NOT NULL UNIQUE,
                    worktree_path TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    commit_sha TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_repair_tasks_record_id ON repair_tasks(record_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_repair_tasks_status ON repair_tasks(status)"
            )
            self._conn.commit()

    def close(self) -> None:
        """停止 worker 并关闭旧 dispatcher 的后台处理。"""
        self._stop_event.set()
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=5)

    def has_message(self, message_id: str) -> bool:
        """检查某条飞书消息是否已经登记成修复任务。"""
        with self._db_lock:
            row = self._conn.execute(
                "SELECT 1 FROM repair_tasks WHERE message_id = ? LIMIT 1",
                (message_id,),
            ).fetchone()
            return row is not None

    def dispatch(self, request: RepairTaskRequest) -> RepairTaskRow | None:
        """登记并排队一条旧流程修复任务。

        若 message_id 已存在则返回 ``None``。成功时会分配唯一分支和 worktree，
        状态置为 ``queued``，随后由后台 worker 自动执行。
        """
        payload_json = json.dumps(dataclasses.asdict(request), ensure_ascii=False, sort_keys=True)
        now = int(time.time())
        with self._db_lock:
            existing = self._conn.execute(
                "SELECT task_id FROM repair_tasks WHERE message_id = ? LIMIT 1",
                (request.message_id,),
            ).fetchone()
            if existing is not None:
                return None

            branch_name = self._allocate_branch_name(request.record_id)
            worktree_path = self._allocate_worktree_path(branch_name, request.message_id)
            cursor = self._conn.execute(
                """
                INSERT INTO repair_tasks (
                    message_id, chat_id, record_id, base_token, table_id,
                    branch_name, worktree_path, payload_json, status, summary,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.message_id,
                    request.chat_id,
                    request.record_id,
                    request.base_token,
                    request.table_id,
                    branch_name,
                    str(worktree_path),
                    payload_json,
                    "queued",
                    request.issue_summary,
                    now,
                ),
            )
            self._conn.commit()
            task_id = int(cursor.lastrowid)

        log_task_status(
            task_id,
            request.record_id,
            "等待中",
            f"record_id={request.record_id} branch={branch_name} worktree={worktree_path}",
        )
        task_log = json.dumps(
            {
                "message_id": request.message_id,
                "chat_id": request.chat_id,
                "record_id": request.record_id,
                "base_token": request.base_token,
                "table_id": request.table_id,
                "branch_name": branch_name,
                "worktree_path": str(worktree_path),
                "issue_summary": request.issue_summary,
                "source_message": request.source_message,
                "fields": request.fields,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        log_task_multiline(task_id, request.record_id, "分发任务", task_log)
        self._queue.put(task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> RepairTaskRow | None:
        """按任务 ID 读取旧任务行。"""
        with self._db_lock:
            row = self._conn.execute(
                "SELECT * FROM repair_tasks WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def _row_to_task(self, row: sqlite3.Row) -> RepairTaskRow:
        """把 SQLite 行转换成 ``RepairTaskRow``。"""
        return RepairTaskRow(
            task_id=int(row["task_id"]),
            message_id=str(row["message_id"]),
            chat_id=str(row["chat_id"]),
            record_id=str(row["record_id"]),
            base_token=str(row["base_token"]),
            table_id=str(row["table_id"]),
            branch_name=str(row["branch_name"]),
            worktree_path=str(row["worktree_path"]),
            payload_json=str(row["payload_json"]),
            status=str(row["status"]),
            summary=str(row["summary"]),
            commit_sha=row["commit_sha"],
            error=row["error"],
            created_at=int(row["created_at"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def _allocate_branch_name(self, record_id: str) -> str:
        """为旧流程任务分配不重复的 ``fix/base-...`` 分支。"""
        base_branch = f"fix/base-{sanitize_branch_suffix(record_id)}"
        existing_branches = {
            str(row["branch_name"])
            for row in self._conn.execute("SELECT branch_name FROM repair_tasks").fetchall()
        }
        candidate = base_branch
        suffix = 2
        while candidate in existing_branches or self._branch_exists(candidate):
            candidate = f"{base_branch}-r{suffix}"
            suffix += 1
        return candidate

    def _branch_exists(self, branch_name: str) -> bool:
        """检查仓库中是否已有指定本地分支。"""
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=self.repo_root,
        )
        return result.returncode == 0

    def _allocate_worktree_path(self, branch_name: str, message_id: str) -> Path:
        """为旧流程任务生成隔离 worktree 路径。"""
        safe_branch = sanitize_branch_suffix(branch_name).replace("/", "__")
        safe_message = sanitize_branch_suffix(message_id)
        path = self.worktree_root / f"{safe_branch}-{safe_message}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _worker_loop(self) -> None:
        """旧 dispatcher 的后台 worker 循环。"""
        while not self._stop_event.is_set():
            task_id = self._queue.get()
            try:
                if task_id is None:
                    return
                self._process_task(task_id)
            finally:
                self._queue.task_done()

    def _update_task(self, task_id: int, **values: Any) -> None:
        """更新旧任务表中的一组字段。"""
        if not values:
            return
        columns = ", ".join(f"{key} = ?" for key in values)
        params = list(values.values()) + [task_id]
        with self._db_lock:
            self._conn.execute(f"UPDATE repair_tasks SET {columns} WHERE task_id = ?", params)
            self._conn.commit()

    def _load_request(self, task: RepairTaskRow) -> RepairTaskRequest:
        """从旧任务 payload JSON 还原原始修复请求。"""
        payload = json.loads(task.payload_json)
        return RepairTaskRequest(
            message_id=payload["message_id"],
            chat_id=payload["chat_id"],
            record_id=payload["record_id"],
            base_token=payload["base_token"],
            table_id=payload["table_id"],
            fields=payload["fields"],
            source_message=payload["source_message"],
            issue_summary=payload["issue_summary"],
        )

    def retry_base_status_failures(self, *, limit: int = 20) -> int:
        """重试已经提交代码但 Base 状态回写失败的旧任务。"""
        with self._db_lock:
            rows = self._conn.execute(
                """
                SELECT * FROM repair_tasks
                WHERE status = ? AND commit_sha IS NOT NULL
                ORDER BY finished_at DESC, task_id DESC
                LIMIT ?
                """,
                ("base_update_failed", limit),
            ).fetchall()

        succeeded = 0
        for row in rows:
            task = self._row_to_task(row)
            request = self._load_request(task)
            log_task_status(
                task.task_id,
                request.record_id,
                "Base状态重试",
                f"commit={task.commit_sha} status={self.status_value} as {self.base_update_identity}",
            )
            try:
                self._update_base_status(request)
            except Exception as exc:
                error = f"code committed as {task.commit_sha}, but failed to update Base status: {exc}"
                self._update_task(task.task_id, error=error, finished_at=int(time.time()))
                log_task_status(task.task_id, request.record_id, "Base状态重试失败", error)
                continue

            self._update_task(
                task.task_id,
                status="succeeded",
                error=None,
                finished_at=int(time.time()),
            )
            log_task_status(
                task.task_id,
                request.record_id,
                "Base状态重试成功",
                f"commit={task.commit_sha} status={self.status_value}",
            )
            succeeded += 1
        return succeeded

    def _process_task(self, task_id: int) -> None:
        """执行一个旧任务，并根据异常类型写入最终状态。"""
        task = self.get_task(task_id)
        if task is None:
            return
        request = self._load_request(task)
        started_at = int(time.time())
        self._update_task(task_id, status="running", started_at=started_at)
        log_task_status(
            task_id,
            request.record_id,
            "执行中",
            f"record_id={request.record_id} branch={task.branch_name}",
        )

        result: RepairTaskResult | None = None
        error: str | None = None
        failed_status = "failed"
        failed_commit_sha: str | None = None
        try:
            result = self._run_task(task, request)
        except BaseStatusUpdateError as exc:
            error = str(exc)
            failed_status = "base_update_failed"
            failed_commit_sha = exc.commit_sha
        except Exception as exc:
            error = str(exc)
        finished_at = int(time.time())

        if result is None:
            values: dict[str, Any] = {
                "status": failed_status,
                "finished_at": finished_at,
                "error": error,
            }
            if failed_commit_sha:
                values["commit_sha"] = failed_commit_sha
            self._update_task(task_id, **values)
            log_task_status(
                task_id,
                request.record_id,
                "失败",
                f"record_id={request.record_id} branch={task.branch_name} status={failed_status} error={error}",
            )
            return

        self._update_task(
            task_id,
            status="succeeded",
            finished_at=finished_at,
            commit_sha=result.commit_sha,
            error=None,
        )
        log_task_status(
            task_id,
            request.record_id,
            "成功",
            f"record_id={request.record_id} branch={result.branch_name} commit={result.commit_sha}",
        )

    def _run_task(self, task: RepairTaskRow, request: RepairTaskRequest) -> RepairTaskResult:
        """运行旧流程的一次性修复。

        流程固定为：创建 worktree -> 调用 Codex CLI -> 检查并提交 diff ->
        更新 Base 状态。旧流程没有新管理台的多轮 Prompt 和验证命令。
        """
        worktree_path = Path(task.worktree_path)
        log_task_status(task.task_id, request.record_id, "执行中", f"creating worktree={worktree_path}")
        self._create_worktree(task.branch_name, worktree_path)
        log_task_status(task.task_id, request.record_id, "执行中", "running codex exec")
        codex_summary = self._run_codex_exec(task, request, worktree_path)
        if codex_summary:
            log_task_multiline(task.task_id, request.record_id, "AI输出", codex_summary)
        log_task_status(task.task_id, request.record_id, "执行中", "checking git changes and committing")
        commit_sha = self._commit_worktree(task, request, worktree_path)
        self._update_task(task.task_id, commit_sha=commit_sha)
        log_task_status(task.task_id, request.record_id, "代码提交完成", f"commit={commit_sha}")
        log_task_status(
            task.task_id,
            request.record_id,
            "执行中",
            f"updating Base status=AI修复完成 as {self.base_update_identity}",
        )
        try:
            self._update_base_status(request)
        except Exception as exc:
            raise BaseStatusUpdateError(commit_sha, str(exc)) from exc
        log_task_status(task.task_id, request.record_id, "Base状态已更新", f"status={self.status_value}")
        summary = codex_summary or request.issue_summary
        return RepairTaskResult(
            branch_name=task.branch_name,
            worktree_path=str(worktree_path),
            commit_sha=commit_sha,
            summary=summary,
            error=None,
        )

    def _create_worktree(self, branch_name: str, worktree_path: Path) -> None:
        """从当前 HEAD 创建旧流程任务 worktree。"""
        if worktree_path.exists():
            raise RuntimeError(f"worktree path already exists: {worktree_path}")
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        run_checked(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch_name,
                str(worktree_path),
                "HEAD",
            ],
            cwd=self.repo_root,
        )

    def _run_codex_exec(self, task: RepairTaskRow, request: RepairTaskRequest, worktree_path: Path) -> str | None:
        """调用 Codex CLI 执行旧流程单轮自动修复。"""
        with tempfile.NamedTemporaryFile(prefix="repair-task-", suffix=".md", delete=False) as handle:
            output_path = Path(handle.name)

        prompt = build_repair_prompt(request, branch_name=task.branch_name, worktree_path=worktree_path)
        log_task_multiline(task.task_id, request.record_id, "AI输入", prompt)
        cmd = [
            resolve_codex_bin(),
            "exec",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(worktree_path),
            "--skip-git-repo-check",
            "--output-last-message",
            str(output_path),
            prompt,
        ]
        try:
            result = subprocess.run(
                cmd,
                check=True,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=DEFAULT_CODEX_TIMEOUT,
            )
            if result.stdout:
                log_task_multiline(task.task_id, request.record_id, "AI日志(stdout)", result.stdout.strip())
            if result.stderr:
                log_task_multiline(task.task_id, request.record_id, "AI日志(stderr)", result.stderr.strip())
            text = output_path.read_text(encoding="utf-8").strip()
            if text:
                log_task_multiline(task.task_id, request.record_id, "AI最终回复", text)
        except subprocess.CalledProcessError as exc:
            stdout_text = clean_subprocess_text(exc.stdout)
            stderr_text = clean_subprocess_text(exc.stderr)
            if stdout_text:
                log_task_multiline(task.task_id, request.record_id, "AI日志(stdout)", stdout_text)
            if stderr_text:
                log_task_multiline(task.task_id, request.record_id, "AI日志(stderr)", stderr_text)
            raise
        except subprocess.TimeoutExpired as exc:
            stdout_text = clean_subprocess_text(exc.stdout)
            stderr_text = clean_subprocess_text(exc.stderr)
            if stdout_text:
                log_task_multiline(task.task_id, request.record_id, "AI日志(stdout)", stdout_text)
            if stderr_text:
                log_task_multiline(task.task_id, request.record_id, "AI日志(stderr)", stderr_text)
            raise
        finally:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass

        if not text:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None
        return " ".join(lines)

    def _commit_worktree(self, task: RepairTaskRow, request: RepairTaskRequest, worktree_path: Path) -> str:
        """提交旧流程 worktree 中的修改并返回 HEAD SHA。"""
        initial_head = self._git_head(worktree_path)
        status = run_checked(["git", "status", "--porcelain"], cwd=worktree_path)
        if status.stdout.strip():
            run_checked(["git", "add", "-A"], cwd=worktree_path)
            commit_message = build_commit_message(request.record_id, request.issue_summary)
            run_checked(["git", "commit", "-m", commit_message], cwd=worktree_path)
        current_head = self._git_head(worktree_path)
        if current_head == initial_head and not status.stdout.strip():
            raise RuntimeError("codex did not produce any file changes")
        return current_head

    def _git_head(self, worktree_path: Path) -> str:
        """读取 worktree 当前 HEAD SHA。"""
        result = run_checked(["git", "rev-parse", "HEAD"], cwd=worktree_path)
        return result.stdout.strip()

    def _update_base_status(self, request: RepairTaskRequest) -> None:
        """把旧流程任务对应的 Base 状态字段写为完成状态。"""
        payload = json.dumps({self.status_field_id: self.status_value}, ensure_ascii=False)
        run_base_json(
            [
                "lark-cli",
                "base",
                "+record-upsert",
                "--base-token",
                request.base_token,
                "--table-id",
                request.table_id,
                "--record-id",
                request.record_id,
                "--as",
                self.base_update_identity,
                "--json",
                payload,
            ]
        )
