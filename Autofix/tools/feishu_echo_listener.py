#!/usr/bin/env python3
"""飞书群消息监听与 Base 卡片解析工具。

新管理台会复用本模块的“消息解析、Base 行定位、飞书回复”函数；本文件也保留
一个可单独运行的旧式监听入口。调用示例：

    python3 Autofix/tools/feishu_echo_listener.py --chat-name "修复群"

阅读模型：Event 是原始群消息，CardContext 是从消息卡片中抽出的定位线索，
BaseRow 是最终查到的多维表格行。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import threading
import tempfile
import time
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repair_task_dispatcher import RepairTaskDispatcher, RepairTaskRequest


DEFAULT_CHAT_NAME = "群名"
DEFAULT_CHAT_ID = "oc_xxxxx"
DEFAULT_STATE_DB = Path(__file__).resolve().parents[1] / ".feishu_echo" / "echo.sqlite3"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_POLL_PAGE_SIZE = 50
DEFAULT_SELF_APP_ID = "cli_xxxxx"
DEFAULT_SELF_OPEN_BOT_ID = "ou_xxxxx"
DEFAULT_MAX_CONCURRENT_TASKS = 2


@dataclass(frozen=True)
class Event:
    """标准化后的飞书消息事件。

    无论来源是实时事件流还是历史消息轮询，都会先转成这个结构，再交给
    ``handle_event`` 或管理台的项目级处理函数。
    """

    message_id: str
    chat_id: str
    sender_id: str
    sender_type: str
    message_type: str
    content: str


@dataclass(frozen=True)
class BaseRow:
    """一条可用于生成修复任务的多维表格记录。"""

    base_token: str
    table_id: str
    record_id: str
    fields: dict[str, Any]
    source: str


@dataclass(frozen=True)
class CardContext:
    """从飞书消息卡片中抽取出的 Base 定位上下文。

    它可能只包含 record_id、record_url、table_url 或搜索关键词中的一部分；
    后续查表逻辑会按“精确记录 -> 链接解析 -> 关键词搜索”的顺序补齐。
    """

    is_base_card: bool
    record_id: str | None
    record_url: str | None
    table_url: str | None
    search_keyword: str | None
    summary: str


def build_parser() -> argparse.ArgumentParser:
    """构建旧监听入口的命令行参数。"""
    parser = argparse.ArgumentParser(description="Subscribe to a Feishu group and echo incoming messages.")
    parser.add_argument("--chat-id", default=DEFAULT_CHAT_ID, help="Target chat_id to subscribe to.")
    parser.add_argument("--chat-name", default=DEFAULT_CHAT_NAME, help="Human-readable chat name for logs.")
    parser.add_argument(
        "--state-db",
        type=Path,
        default=DEFAULT_STATE_DB,
        help="SQLite path used to deduplicate processed message_id values.",
    )
    parser.add_argument(
        "--consume-timeout",
        default=None,
        help="Optional timeout passed through to lark-cli event consume, e.g. 30s.",
    )
    parser.add_argument(
        "--consume-max-events",
        type=int,
        default=0,
        help="Optional max-events passed through to lark-cli event consume.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="Seconds between chat list polls used to catch Base cards that do not reach the realtime stream.",
    )
    parser.add_argument(
        "--retry-base-status",
        action="store_true",
        help="Retry Base status updates for tasks that already committed code but failed to update Base.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the commands and exit.")
    return parser


def run_checked(args: list[str], *, capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess[str]:
    """执行命令并在失败时抛出 ``CalledProcessError``。"""
    return subprocess.run(args, check=True, capture_output=capture_output, text=text)


def ensure_state(db_path: Path) -> sqlite3.Connection:
    """创建消息去重数据库并返回连接。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            replied_at INTEGER NOT NULL,
            reply_text TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def flatten_strings(value: Any) -> list[str]:
    """递归提取 JSON/列表/字典中的所有字符串。

    飞书卡片内容层级较深；先扁平化为字符串列表，后续提取链接、record_id 和
    标签值会更稳定。
    """
    items: list[str] = []
    if isinstance(value, str):
        items.append(value)
    elif isinstance(value, dict):
        for nested in value.values():
            items.extend(flatten_strings(nested))
    elif isinstance(value, list):
        for nested in value:
            items.extend(flatten_strings(nested))
    return items


def parse_maybe_json(content: str) -> Any:
    """如果消息内容看起来像 JSON，就解析它；否则保留原文本。"""
    stripped = content.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content


def is_url(text: str) -> bool:
    """粗略判断字符串中是否包含 HTTP(S) URL。"""
    return bool(re.search(r"https?://", text))


def extract_first_url(strings: list[str], *, contains: str | None = None) -> str | None:
    """从字符串列表中提取第一个 URL，可按子串过滤。"""
    for text in strings:
        for match in re.finditer(r"\[[^\]]+\]\((https?://[^)\s]+)\)", text):
            url = match.group(1)
            if contains and contains not in url:
                continue
            return url
        for match in re.finditer(r"https?://[^\s<>'\"）)\]]+", text):
            url = match.group(0)
            if contains and contains not in url:
                continue
            return url
    return None


def extract_record_id(strings: list[str]) -> str | None:
    """提取形如 ``rec...`` 的 Base 记录 ID。"""
    for text in strings:
        match = re.search(r"\brec[A-Za-z0-9]{6,}\b", text)
        if match:
            return match.group(0)
    return None


def extract_label_value(strings: list[str], labels: list[str]) -> str | None:
    """从卡片文本中提取指定标签后面的值。

    同时支持“标签: 值”和“标签独占一行，下一行是值”两种常见卡片结构。
    """
    for index, text in enumerate(strings):
        stripped = text.strip()
        if not stripped:
            continue
        for label in labels:
            if stripped == label:
                for candidate in strings[index + 1 :]:
                    candidate = candidate.strip()
                    if not candidate or is_url(candidate):
                        continue
                    if re.fullmatch(r"rec[A-Za-z0-9]+", candidate):
                        continue
                    if candidate in labels:
                        continue
                    if candidate.startswith("字段") or candidate.startswith("记录") or candidate.startswith("来自"):
                        continue
                    return candidate
            match = re.search(rf"{re.escape(label)}\s*[:：]\s*(.+)", stripped)
            if match:
                value = match.group(1).strip()
                if value:
                    return value
    return None


def extract_card_context(content: str) -> CardContext:
    """把飞书消息内容解析成 Base 卡片上下文。"""
    parsed = parse_maybe_json(content)
    strings = flatten_strings(parsed)
    if not strings:
        strings = [content]

    record_id = extract_record_id(strings)
    record_url = extract_first_url(strings, contains="/record/") or extract_first_url(strings, contains="record=")
    table_url = extract_first_url(strings, contains="/base/") or extract_first_url(strings, contains="table=")
    search_keyword = extract_label_value(strings, ["问题模块/描述", "问题模块", "描述"])

    summary_parts = []
    if record_id:
        summary_parts.append(f"record_id={record_id}")
    if record_url:
        summary_parts.append(f"record_url={record_url}")
    if table_url:
        summary_parts.append(f"table_url={table_url}")
    if search_keyword:
        summary_parts.append(f"keyword={search_keyword}")
    summary = " ; ".join(summary_parts) if summary_parts else content[:400]

    is_base_card = bool(record_id or record_url or table_url or search_keyword)
    return CardContext(
        is_base_card=is_base_card,
        record_id=record_id,
        record_url=record_url,
        table_url=table_url,
        search_keyword=search_keyword,
        summary=summary,
    )


def summarize_issue_from_row(card: CardContext, row: BaseRow) -> str:
    """为任务列表生成简短问题摘要。

    优先使用卡片摘要，再补充多维表格里的问题描述、预期结果、优先级等字段。
    """
    parts: list[str] = []
    if card.summary:
        parts.append(card.summary)
    for key in ("问题模块/描述", "预期结果", "类型", "优先级", "状态", "备注"):
        value = row.fields.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            flattened = [str(item) for item in value if item is not None]
            if not flattened:
                continue
            value_text = " / ".join(flattened)
        else:
            value_text = str(value)
        if value_text:
            parts.append(f"{key}={value_text}")
    if not parts:
        parts.append(row.record_id)
    return " ; ".join(parts)


def resolve_self_identity_ids() -> set[str]:
    """解析当前机器人身份，用于过滤机器人自己发出的消息。"""
    try:
        result = run_checked(
            [
                "lark-cli",
                "auth",
                "status",
                "--json",
                "--verify",
            ]
        )
        payload = json.loads(result.stdout)
        app_id = payload.get("appId")
        open_id = payload.get("identities", {}).get("bot", {}).get("openId")
        ids = {value for value in (app_id, open_id) if isinstance(value, str) and value}
        if ids:
            return ids
    except Exception as exc:
        print(f"lark-cli auth status unavailable, using built-in bot ids: {exc}", file=sys.stderr)

    return {DEFAULT_SELF_APP_ID, DEFAULT_SELF_OPEN_BOT_ID}


def parse_message_position(value: Any) -> int | None:
    """把飞书消息位置转换成整数；无法识别时返回 ``None``。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def run_base_json(args: list[str]) -> dict[str, Any] | None:
    """运行 lark-cli Base 命令并返回 ``ok=true`` 的 JSON。"""
    try:
        result = run_checked(args)
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"base command failed: {' '.join(args)}: {message}", file=sys.stderr)
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"base command returned invalid json: {' '.join(args)}: {exc}", file=sys.stderr)
        return None
    if not payload.get("ok", False):
        print(f"base command not ok: {' '.join(args)}: {payload}", file=sys.stderr)
        return None
    return payload


def run_im_json(args: list[str]) -> dict[str, Any] | None:
    """运行 lark-cli IM 命令并返回 ``ok=true`` 的 JSON。"""
    try:
        result = run_checked(args)
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"im command failed: {' '.join(args)}: {message}", file=sys.stderr)
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"im command returned invalid json: {' '.join(args)}: {exc}", file=sys.stderr)
        return None
    if not payload.get("ok", False):
        print(f"im command not ok: {' '.join(args)}: {payload}", file=sys.stderr)
        return None
    return payload


def resolve_base_context(url: str) -> dict[str, Any] | None:
    """从 Base URL 解析 base_token、table_id 和可选 record_id。"""
    payload = run_base_json(
        [
            "lark-cli",
            "base",
            "+url-resolve",
            "--url",
            url,
            "--as",
            "bot",
            "--json",
        ]
    )
    if not payload:
        return None
    data = payload.get("data", {})
    base_token = data.get("base_token")
    table_id = data.get("table_id")
    record_id = data.get("record_id")
    if not isinstance(base_token, str) or not base_token:
        return None
    if not isinstance(table_id, str) or not table_id:
        return None
    resolved: dict[str, Any] = {
        "base_token": base_token,
        "table_id": table_id,
    }
    if isinstance(record_id, str) and record_id:
        resolved["record_id"] = record_id
    return resolved


def parse_base_rows(payload: dict[str, Any], source: str) -> list[BaseRow]:
    """把 lark-cli 表格输出转换为 ``BaseRow`` 列表。"""
    data = payload.get("data", {})
    rows = data.get("data") or []
    field_names = data.get("fields") or []
    record_ids = data.get("record_id_list") or []
    base_token = data.get("base_token")
    table_id = data.get("table_id")
    if not isinstance(base_token, str):
        base_token = ""
    if not isinstance(table_id, str):
        table_id = ""

    parsed: list[BaseRow] = []
    for index, row in enumerate(rows):
        if not isinstance(row, list):
            continue
        record_id = ""
        if index < len(record_ids) and isinstance(record_ids[index], str):
            record_id = record_ids[index]
        fields: dict[str, Any] = {}
        for field_index, field_name in enumerate(field_names):
            if field_index < len(row):
                fields[str(field_name)] = row[field_index]
        if record_id:
            parsed.append(
                BaseRow(
                    base_token=base_token,
                    table_id=table_id,
                    record_id=record_id,
                    fields=fields,
                    source=source,
                )
            )
    return parsed


def lookup_record_by_id(context: dict[str, Any], record_id: str, source: str) -> BaseRow | None:
    """按 record_id 精确读取一条 Base 记录。"""
    payload = run_base_json(
        [
            "lark-cli",
            "base",
            "+record-get",
            "--base-token",
            context["base_token"],
            "--table-id",
            context["table_id"],
            "--record-id",
            record_id,
            "--as",
            "bot",
            "--format",
            "json",
        ]
    )
    if not payload:
        return None
    rows = parse_base_rows(payload, source)
    if len(rows) == 1:
        return rows[0]
    return None


def search_unique_record(context: dict[str, Any], keyword: str, search_field: str, source: str) -> BaseRow | None:
    """按关键词搜索 Base 记录，只在命中唯一结果时返回。"""
    payload = run_base_json(
        [
            "lark-cli",
            "base",
            "+record-search",
            "--base-token",
            context["base_token"],
            "--table-id",
            context["table_id"],
            "--keyword",
            keyword,
            "--search-field",
            search_field,
            "--as",
            "bot",
            "--format",
            "json",
            "--limit",
            "2",
        ]
    )
    if not payload:
        return None
    data = payload.get("data", {})
    record_ids = data.get("record_id_list") or []
    if len(record_ids) != 1:
        return None
    rows = parse_base_rows(payload, source)
    if len(rows) == 1:
        return rows[0]
    return None


def normalize_row(context: dict[str, Any], row: BaseRow) -> BaseRow:
    """用已解析的 Base 上下文补齐行对象中的 base_token/table_id。"""
    return BaseRow(
        base_token=context["base_token"],
        table_id=context["table_id"],
        record_id=row.record_id,
        fields=row.fields,
        source=row.source,
    )


def lookup_base_row_for_card(card: CardContext) -> BaseRow | None:
    """根据卡片线索查找唯一 Base 行。

    查找优先级：record_id 精确查询 -> record_url 解析查询 -> 问题描述关键词搜索。
    任一步不能唯一定位时返回 ``None``，由上层回复“未查找到唯一行”。
    """
    candidate_urls = [url for url in [card.record_url, card.table_url] if url]
    if not candidate_urls:
        return None

    context = None
    for url in candidate_urls:
        context = resolve_base_context(url)
        if context:
            break
    if not context:
        return None

    if card.record_id:
        row = lookup_record_by_id(context, card.record_id, "record_id")
        if row:
            return normalize_row(context, row)

    if card.record_url:
        resolved = resolve_base_context(card.record_url)
        if resolved:
            record_id = resolved.get("record_id") or card.record_id
            if isinstance(record_id, str) and record_id:
                row = lookup_record_by_id(resolved, record_id, "record_link")
                if row:
                    return normalize_row(resolved, row)

    if card.search_keyword:
        row = search_unique_record(context, card.search_keyword, "问题模块/描述", "search")
        if row:
            return normalize_row(context, row)

    return None


def row_to_prompt_data(row: BaseRow) -> dict[str, Any]:
    """把 Base 行转换成适合塞进 Prompt 的 JSON 结构。"""
    return {
        "table_id": row.table_id,
        "record_id": row.record_id,
        "source": row.source,
        "fields": row.fields,
    }


def build_ai_prompt(event: Event, card: CardContext | None, row: BaseRow | None) -> str:
    """构造普通群消息的 AI 回显 Prompt。

    该 Prompt 只用于“收到：...”这类短回复，不参与自动修复代码。
    """
    if row is not None:
        return textwrap.dedent(
            f"""
            你是一个飞书群消息处理器。请根据消息内容和多维表格行信息，输出一条可以直接发到群里的中文一句话。
            规则：
            - 只输出最终回显正文
            - 不要换行
            - 不要 Markdown
            - 如果能做到，开头保留“收到：”
            - 重点概括表格行的关键信息，不要解释流程

            消息摘要：
            {card.summary if card else event.content}

            Base行信息：
            {json.dumps(row_to_prompt_data(row), ensure_ascii=False, indent=2)}
            """
        ).strip()

    return textwrap.dedent(
        f"""
        你是一个飞书群消息处理器。请把下面这条消息整理成一句可以直接发到群里的中文回显。
        规则：
        - 只输出最终回显正文
        - 不要换行
        - 不要 Markdown
        - 开头保留“收到：”
        - 保留原意，尽量简短

        消息内容：
        {event.content}
        """
    ).strip()


def run_codex_exec(prompt: str) -> str | None:
    """使用 codex CLI 生成一次普通群消息回显。"""
    with tempfile.NamedTemporaryFile(prefix="feishu-echo-", suffix=".md", delete=False) as handle:
        output_path = Path(handle.name)

    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--output-last-message",
        str(output_path),
        prompt,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
        text = output_path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        print(f"codex exec failed: {exc}", file=sys.stderr)
        text = ""
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


def build_fallback_reply(event: Event, card: CardContext | None, row: BaseRow | None) -> str:
    """在 AI 回显不可用时生成确定性的短回复。"""
    if row is not None:
        selected = {}
        for key in ("问题模块/描述", "预期结果", "状态", "优先级", "类型", "备注", "修复版本"):
            value = row.fields.get(key)
            if value is not None:
                selected[key] = value
        if not selected:
            selected = row.fields
        return f"收到：已定位记录 {row.record_id}。{json.dumps(selected, ensure_ascii=False)}"

    if card and card.is_base_card:
        return "未查找到唯一行"

    return f"收到：{event.content.strip()}"


def is_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    """检查消息是否已经回显或分发过，避免重复处理。"""
    row = conn.execute(
        "SELECT 1 FROM processed_messages WHERE message_id = ? LIMIT 1",
        (message_id,),
    ).fetchone()
    return row is not None


def mark_processed(conn: sqlite3.Connection, event: Event, reply_text: str) -> None:
    """把消息处理结果写入去重表。"""
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_messages (message_id, chat_id, replied_at, reply_text)
        VALUES (?, ?, ?, ?)
        """,
        (event.message_id, event.chat_id, int(time.time()), reply_text),
    )
    conn.commit()


def resolve_chat_id(chat_id: str, chat_name: str) -> str:
    """根据传入 chat_id 或群名解析目标飞书群。"""
    if chat_id:
        return chat_id

    result = run_checked(
        [
            "lark-cli",
            "im",
            "+chat-search",
            "--as",
            "bot",
            "--json",
            "--query",
            chat_name,
            "--chat-modes",
            "group",
            "--page-size",
            "20",
        ]
    )
    payload = json.loads(result.stdout)
    chats = payload.get("data", {}).get("chats") or payload.get("chats") or []
    exact = [item for item in chats if item.get("name") == chat_name and item.get("chat_id")]
    if exact:
        return exact[0]["chat_id"]
    if len(chats) == 1 and chats[0].get("chat_id"):
        return chats[0]["chat_id"]

    names = ", ".join(item.get("name", "<unknown>") for item in chats[:5]) or "<none>"
    raise RuntimeError(f"could not resolve chat_id for {chat_name!r}; visible matches: {names}")


def pump_stderr(proc: subprocess.Popen[str], ready: threading.Event) -> None:
    """转发事件消费进程 stderr，并捕捉 ready 标记。"""
    assert proc.stderr is not None
    for line in proc.stderr:
        sys.stderr.write(line)
        sys.stderr.flush()
        if "[event] ready event_key=im.message.receive_v1" in line:
            ready.set()


def consume_events(
    chat_id: str,
    timeout: str | None,
    max_events: int,
) -> subprocess.Popen[str]:
    """启动 lark-cli 实时事件消费进程。

    返回的 Popen 由调用方读取 stdout 中的 NDJSON 事件，并负责最终 terminate。
    """
    jq = f'select(.chat_id=="{chat_id}")'
    cmd = [
        "lark-cli",
        "event",
        "consume",
        "im.message.receive_v1",
        "--as",
        "bot",
        "--jq",
        jq,
    ]
    if timeout:
        cmd.extend(["--timeout", timeout])
    if max_events > 0:
        cmd.extend(["--max-events", str(max_events)])
    keep_stdin_open = not timeout and max_events == 0
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if keep_stdin_open else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def parse_event(raw: dict[str, Any]) -> Event | None:
    """把实时事件 JSON 转为 ``Event``；缺少必要字段时返回 ``None``。"""
    message_id = raw.get("message_id") or raw.get("id")
    chat_id = raw.get("chat_id")
    sender_id = raw.get("sender_id", "")
    sender_type = raw.get("sender_type", "")
    message_type = raw.get("message_type", "")
    content = raw.get("content", "")
    if not isinstance(message_id, str) or not message_id:
        return None
    if not isinstance(chat_id, str) or not chat_id:
        return None
    if not isinstance(sender_id, str):
        return None
    if not isinstance(sender_type, str):
        return None
    if not isinstance(message_type, str):
        return None
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return Event(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_type=sender_type,
        message_type=message_type,
        content=content,
    )


def parse_list_message(raw: dict[str, Any]) -> Event | None:
    """把历史消息列表中的单条消息转为 ``Event``。"""
    sender = raw.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}
    parsed = {
        "message_id": raw.get("message_id"),
        "chat_id": raw.get("chat_id"),
        "sender_id": sender.get("id") or sender.get("open_bot_id") or raw.get("sender_id", ""),
        "sender_type": sender.get("sender_type") or raw.get("sender_type", ""),
        "message_type": raw.get("msg_type") or raw.get("message_type") or "",
        "content": raw.get("content", ""),
    }
    return parse_event(parsed)


def send_echo(chat_id: str, source_message_id: str, reply_text: str, *, idempotency_suffix: str = "") -> None:
    """用飞书“回复”功能回复指定消息。

    ``chat_id`` 保留在签名里用于调用者语义清晰；实际回复依赖 message_id。
    ``idempotency_suffix`` 用于区分收到回显和完成回显，避免幂等键冲突。
    """
    key_seed = f"{source_message_id}:{idempotency_suffix}".encode("utf-8")
    key = f"echo-{hashlib.sha1(key_seed).hexdigest()[:24]}"
    cmd = [
        "lark-cli",
        "im",
        "+messages-reply",
        "--as",
        "bot",
        "--message-id",
        source_message_id,
        "--idempotency-key",
        key,
        "--text",
        reply_text,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = " ".join(part.strip() for part in (exc.stderr, exc.stdout) if part and part.strip())
        raise RuntimeError(detail or f"lark-cli messages-reply failed with exit code {exc.returncode}") from exc


def list_chat_messages(
    chat_id: str,
    *,
    page_size: int = DEFAULT_POLL_PAGE_SIZE,
    page_token: str | None = None,
) -> dict[str, Any] | None:
    """读取群历史消息页，用于轮询兜底。"""
    cmd = [
        "lark-cli",
        "im",
        "+chat-messages-list",
        "--as",
        "bot",
        "--chat-id",
        chat_id,
        "--json",
        "--order",
        "desc",
        "--page-size",
        str(page_size),
        "--no-reactions",
    ]
    if page_token:
        cmd.extend(["--page-token", page_token])
    return run_im_json(cmd)


def snapshot_poll_baseline(chat_id: str) -> int:
    """读取当前最新消息位置，作为启动后的轮询起点。"""
    payload = list_chat_messages(chat_id, page_size=1)
    if payload is None:
        raise RuntimeError("could not read chat history to establish the poll baseline")
    data = payload.get("data", {})
    messages = data.get("messages") or []
    if not messages:
        return 0
    if not isinstance(messages, list):
        raise RuntimeError("unexpected message list shape while establishing the poll baseline")
    latest = messages[0]
    if not isinstance(latest, dict):
        raise RuntimeError("unexpected latest message shape while establishing the poll baseline")
    position = parse_message_position(latest.get("message_position"))
    if position is None:
        raise RuntimeError("latest message is missing a usable message_position")
    return position


def poll_chat_messages(
    conn: sqlite3.Connection,
    lock: threading.Lock,
    dispatcher: RepairTaskDispatcher,
    chat_id: str,
    self_ids: set[str],
    in_flight: set[str],
    stop_event: threading.Event,
    poll_interval: float,
    since_position: int,
) -> None:
    """旧监听入口的聊天记录轮询线程。

    它只处理 baseline 之后出现的新消息，并使用 ``in_flight`` 防止事件流和轮询
    同时处理同一条消息。
    """
    print(f"polling chat_id={chat_id} since_position={since_position}", file=sys.stderr)

    while not stop_event.wait(poll_interval):
        page_token: str | None = None
        queued: list[tuple[int, Event]] = []
        max_seen_position = since_position

        while True:
            payload = list_chat_messages(chat_id, page_token=page_token)
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
                position = parse_message_position(raw.get("message_position"))
                if position is None:
                    continue
                if oldest_position is None:
                    oldest_position = position
                if position <= since_position:
                    continue
                event = parse_list_message(raw)
                if event is None:
                    continue
                queued.append((position, event))
                if position > max_seen_position:
                    max_seen_position = position

            if not payload.get("data", {}).get("has_more"):
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
                handle_event(conn, lock, dispatcher, event, self_ids, in_flight)
            except Exception as exc:
                print(f"poll handler failed for message_id={event.message_id}: {exc}", file=sys.stderr)

        since_position = max_seen_position


def should_skip(event: Event, self_ids: set[str]) -> bool:
    """判断消息是否应被忽略，例如机器人自身消息或系统消息。"""
    if event.sender_id in self_ids:
        return True
    if event.message_type == "system":
        return True
    return False


def handle_event(
    conn: sqlite3.Connection,
    lock: threading.Lock,
    dispatcher: RepairTaskDispatcher,
    event: Event,
    self_ids: set[str],
    in_flight: set[str] | None = None,
) -> bool:
    """旧监听入口的单消息处理函数。

    Base 卡片会转成 ``RepairTaskRequest`` 交给旧 dispatcher；普通消息会发送短回显。
    返回值表示这条消息是否被消费。
    """
    if should_skip(event, self_ids):
        return False

    with lock:
        if is_processed(conn, event.message_id):
            return False
    if dispatcher.has_message(event.message_id):
        return False

    if in_flight is not None:
        with lock:
            if event.message_id in in_flight:
                return False
            in_flight.add(event.message_id)

    try:
        card = extract_card_context(event.content)
        base_row = lookup_base_row_for_card(card) if card.is_base_card else None

        if card.is_base_card and base_row is None:
            reply_text = "未查找到唯一行"
            send_echo(event.chat_id, event.message_id, reply_text)
            with lock:
                mark_processed(conn, event, reply_text)
            print(f"replied unresolved base card message_id={event.message_id}", file=sys.stderr)
            return True

        if card.is_base_card and base_row is not None:
            task = dispatcher.dispatch(
                RepairTaskRequest(
                    message_id=event.message_id,
                    chat_id=event.chat_id,
                    record_id=base_row.record_id,
                    base_token=base_row.base_token,
                    table_id=base_row.table_id,
                    fields=base_row.fields,
                    source_message=event.content,
                    issue_summary=summarize_issue_from_row(card, base_row),
                )
            )
            if task is None:
                return False
            with lock:
                mark_processed(conn, event, f"TASK:{task.branch_name}")
            print(
                f"dispatched repair task message_id={event.message_id} record_id={base_row.record_id} branch={task.branch_name}",
                file=sys.stderr,
            )
            return True
        else:
            prompt = build_ai_prompt(event, None, None)
            reply_text = run_codex_exec(prompt) or build_fallback_reply(event, None, None)
            if reply_text != "未查找到唯一行" and not reply_text.startswith("收到："):
                reply_text = f"收到：{reply_text}"

        send_echo(event.chat_id, event.message_id, reply_text)

        with lock:
            mark_processed(conn, event, reply_text)
    finally:
        with lock:
            if in_flight is not None:
                in_flight.discard(event.message_id)

    print(f"echoed message_id={event.message_id} chat_id={event.chat_id}", file=sys.stderr)
    return True


def main() -> int:
    """运行独立飞书监听器。"""
    parser = build_parser()
    args = parser.parse_args()

    self_ids = resolve_self_identity_ids()
    chat_id = resolve_chat_id(args.chat_id, args.chat_name)
    print(f"subscribing chat_name={args.chat_name!r} chat_id={chat_id}", file=sys.stderr)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "chat_name": args.chat_name,
                    "chat_id": chat_id,
                    "state_db": str(args.state_db),
                    "self_ids": sorted(self_ids),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    state = ensure_state(args.state_db)
    state_lock = threading.Lock()
    dispatcher = RepairTaskDispatcher(
        repo_root=DEFAULT_REPO_ROOT,
        state_db=args.state_db,
        max_concurrent_tasks=DEFAULT_MAX_CONCURRENT_TASKS,
    )
    if args.retry_base_status:
        succeeded = dispatcher.retry_base_status_failures()
        dispatcher.close()
        state.close()
        print(f"retried Base status updates, succeeded={succeeded}", file=sys.stderr)
        return 0

    in_flight: set[str] = set()
    stop_event = threading.Event()
    if args.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than zero")
    since_position = snapshot_poll_baseline(chat_id)
    print(f"poll baseline established at message_position={since_position}", file=sys.stderr)
    proc = consume_events(chat_id, args.consume_timeout, args.consume_max_events)
    ready = threading.Event()
    stderr_thread = threading.Thread(target=pump_stderr, args=(proc, ready), daemon=True)
    stderr_thread.start()
    poller_thread = threading.Thread(
        target=poll_chat_messages,
        args=(state, state_lock, dispatcher, chat_id, self_ids, in_flight, stop_event, args.poll_interval, since_position),
        daemon=True,
    )
    poller_thread.start()

    deadline = time.time() + 60
    while not ready.is_set():
        if proc.poll() is not None:
            raise RuntimeError(f"event consumer exited before readiness with code {proc.returncode}")
        if time.time() > deadline:
            raise RuntimeError("timed out waiting for event consumer readiness")
        time.sleep(0.2)

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"skipping malformed event line: {exc}: {line}", file=sys.stderr)
                continue

            event = parse_event(payload)
            if event is None:
                continue
            try:
                handle_event(state, state_lock, dispatcher, event, self_ids, in_flight)
            except Exception as exc:
                print(f"event handler failed for message_id={event.message_id}: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("stopping on keyboard interrupt", file=sys.stderr)
    finally:
        stop_event.set()
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
        poller_thread.join(timeout=10)
        dispatcher.close()
        state.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
