"""飞书任务回显文案和安全发送封装。

任务进入系统时发送“收到任务”回复；任务到达终态时发送“任务结果”回复。
发送失败只记录到任务字段和 stderr，不让自动修复主流程因为回显异常而崩掉。
"""

from __future__ import annotations

import sys
import time
from typing import Any

from feishu_echo_listener import send_echo

from autofix_models import TaskRow
from autofix_utils import sanitize_slug


def build_task_received_reply(task: TaskRow) -> str:
    """构造收到任务后的飞书回复文本。

    只包含识别任务所需的短信息；如果任务因为自动修复时间段进入“暂停”，
    文案会额外说明当前已暂停。
    """

    parts = [
        f"收到任务：#{task.task_id}",
        f"WorkRoot：{task.workroot_name}",
        f"记录ID：{task.record_id}",
    ]
    if task.branch_name and not task.branch_name.startswith(("unsupported/", "rejected/")):
        parts.append(f"分支：{task.branch_name}")
    if task.repair_version:
        parts.append(f"修复版本：{task.repair_version}")
    if task.status == "暂停":
        parts.append("当前不在自动修复时间段，已暂停")
    return "\n".join(parts)


def build_task_result_reply(task: TaskRow) -> str:
    """构造任务完成后的飞书结果回复文本。

    这里刻意不附带 AI 说明，避免群里被长输出刷屏；详细 AI 输入输出仍在
    管理台任务详情中查看。
    """

    status_label = {
        "succeeded": "成功",
        "failed": "失败",
        "base_update_failed": "失败（Base状态更新失败）",
        "超时": "超时",
        "拒绝修复": "拒绝修复",
        "状态不支持": "状态不支持",
        "无需修复": "无需修复",
        "暂停": "暂停",
    }.get(task.status, task.status)
    parts = [
        f"任务结果：{status_label}",
        f"任务：#{task.task_id}",
        f"WorkRoot：{task.workroot_name}",
        f"记录ID：{task.record_id}",
    ]
    if task.branch_name and not task.branch_name.startswith(("unsupported/", "rejected/")):
        parts.append(f"分支：{task.branch_name}")
    if task.commit_sha:
        parts.append(f"Commit：{task.commit_sha}")
    return "\n".join(parts)


def summarize_feishu_echo_state(task: TaskRow) -> str:
    """把收到回显和结果回显压缩成任务列表可读的一行状态。"""

    received = "收到失败" if task.feishu_received_error else ("已收到" if task.feishu_received_at else "未收到")
    result = "结果失败" if task.feishu_result_error else ("已完成" if task.feishu_result_at else "未完成")
    return f"{received} / {result}"


def safe_send_task_received(store: "AutofixStore", task: TaskRow) -> None:
    """安全发送“收到任务”飞书回复并回写任务字段。

    调用方只需要传 store 和 task；函数内部负责幂等 suffix、发送异常捕获、
    以及 feishu_received_* 字段更新。
    """

    reply_text = build_task_received_reply(task)
    try:
        send_echo(
            task.chat_id,
            task.message_id,
            reply_text,
            idempotency_suffix=f"received-{task.task_id}",
        )
        store.update_task(
            task.task_id,
            feishu_received_at=int(time.time()),
            feishu_received_text=reply_text,
            feishu_received_error="",
        )
    except Exception as exc:
        store.update_task(
            task.task_id,
            feishu_received_at=task.feishu_received_at or int(time.time()),
            feishu_received_text=reply_text,
            feishu_received_error=str(exc),
        )
        print(f"[repair-task:{task.task_id}][record_id={task.record_id}] 回显收到任务失败 {exc}", file=sys.stderr)


def safe_send_task_result(store: "AutofixStore", task: TaskRow) -> None:
    """安全发送“任务结果”飞书回复并回写任务字段。

    结果回显使用任务 ID 和状态生成幂等 suffix，避免同一终态被重复回复。
    """

    reply_text = build_task_result_reply(task)
    try:
        send_echo(
            task.chat_id,
            task.message_id,
            reply_text,
            idempotency_suffix=f"result-{task.task_id}-{sanitize_slug(task.status)}",
        )
        store.update_task(
            task.task_id,
            feishu_result_at=int(time.time()),
            feishu_result_text=reply_text,
            feishu_result_error="",
        )
    except Exception as exc:
        store.update_task(
            task.task_id,
            feishu_result_at=task.feishu_result_at or int(time.time()),
            feishu_result_text=reply_text,
            feishu_result_error=str(exc),
        )
        print(f"[repair-task:{task.task_id}][record_id={task.record_id}] 回显任务结果失败 {exc}", file=sys.stderr)
