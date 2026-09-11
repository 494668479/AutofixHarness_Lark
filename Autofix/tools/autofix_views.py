"""Autofix 管理台的 HTML 渲染层。

本模块只负责把 store/runtime 中的数据转换为 HTML 字符串和页面脚本，不处理
HTTP 路由、不写数据库、不启动任务。这样页面结构和业务执行逻辑可以分开维护。
"""

from __future__ import annotations

import html
import json
import time
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

from autofix_config import build_form_config, infer_defaults
from autofix_defaults import (
    AI_BRAIN_TYPES,
    AI_CONFIG_CLI_TYPES,
    APP_NAME,
    DEFAULT_CONTEXT_COLLECT_COMMANDS,
    DEFAULT_VERIFY_COMMANDS,
    TEMPLATE_DIR,
)
from autofix_feishu_echo import summarize_feishu_echo_state
from autofix_harness import parse_repair_rounds_json, summarize_repair_rounds
from autofix_models import ProjectConfig, TaskRow
from autofix_store import AutofixStore
from autofix_task_ops import extract_task_problem_description
from autofix_utils import (
    ai_provider_label,
    cli_type_label,
    default_config_home_env,
    describe_auto_repair_window,
    normalize_auto_repair_mode,
    normalize_text_value,
    parse_end_day_offset,
    parse_non_negative_int,
)


def input_row(name: str, value: str, *, type_: str = "text", placeholder: str = "") -> str:
    """渲染一个普通 input 控件。

    适用于简单表单字段；复杂字段在模板中直接写 HTML，便于加入说明文字。
    """

    return f'<input type="{html.escape(type_)}" name="{html.escape(name)}" value="{html.escape(value)}" placeholder="{html.escape(placeholder)}">'


def checkbox_row(name: str, checked: bool) -> str:
    """渲染一个值为 1 的 checkbox 控件。"""

    checked_attr = " checked" if checked else ""
    return f'<input type="checkbox" name="{html.escape(name)}" value="1"{checked_attr}>'


def select_options(options: list[tuple[str, str]], selected: str) -> str:
    """把 `(value, label)` 列表渲染成 option HTML。"""

    items = []
    for value, label in options:
        selected_attr = " selected" if str(value) == str(selected) else ""
        items.append(
            f'<option value="{html.escape(str(value))}"{selected_attr}>{html.escape(label)}</option>'
        )
    return "".join(items)


def ai_config_options(store: AutofixStore, selected_id: int = 0) -> str:
    """渲染项目配置页的 AI 配置组下拉选项。

    只展示启用中的 AI 配置；如果当前项目绑定的是停用配置，也保留该项，
    避免编辑旧配置时下拉框丢失当前值。
    """

    configs = store.list_ai_configs()
    if not configs:
        return '<option value="0">未配置</option>'
    return select_options(
        [
            (
                str(config.id),
                f"{config.name or cli_type_label(config.cli_type)} / {cli_type_label(config.cli_type)} / {config.model_name or 'CLI 默认模型'}",
            )
            for config in configs
            if config.enabled or config.id == selected_id
        ],
        str(selected_id or configs[0].id),
    )


def auto_repair_mode_options(selected: str) -> str:
    """渲染自动修复模式下拉选项。"""

    return select_options(
        [
            ("all_day", "全天自动修复"),
            ("disabled", "不自动修复"),
            ("time_range", "指定时间段"),
        ],
        normalize_auto_repair_mode(selected),
    )


def auto_repair_end_day_options(selected: int) -> str:
    """渲染自动修复结束日期选项：当日或次日。"""

    return select_options(
        [
            ("0", "当日"),
            ("1", "次日"),
        ],
        str(parse_end_day_offset(selected)),
    )


@lru_cache(maxsize=None)
def load_template(name: str) -> Template:
    """读取并缓存 templates 目录下的 HTML 模板。"""

    return Template((TEMPLATE_DIR / name).read_text(encoding="utf-8"))


def render_template(template_name: str, **values: str) -> str:
    """使用 string.Template 替换模板变量。"""

    return load_template(template_name).substitute(**values)


def render_page(title: str, body: str, *, header_actions_html: str = "", page_script_html: str = "") -> str:
    """把页面主体包进统一 layout。"""

    return render_template(
        "layout.html",
        page_title=html.escape(title),
        body=body,
        header_actions_html=header_actions_html,
        page_script_html=page_script_html,
    )


def render_collapsible(title: str, body_html: str, *, open_: bool = False) -> str:
    """渲染一个可折叠面板，用于任务详情里的长文本区。"""

    open_attr = " open" if open_ else ""
    return f"""
    <details class="panel collapsible"{open_attr}>
      <summary>{html.escape(title)}</summary>
      {body_html}
    </details>
    """


def task_status_class(status: str) -> str:
    """根据任务状态返回 CSS class。"""

    if status == "succeeded":
        return "status-ok"
    if status in {"failed", "base_update_failed", "超时", "状态不支持", "拒绝修复"}:
        return "status-bad"
    return ""


def render_task_row(task: TaskRow) -> str:
    """渲染任务历史表格中的一行。

    行上会带 data-task-id 和 data-task-status，前端自动刷新时据此判断是否
    从非终态进入终态并播放提示音。
    """

    status_class = task_status_class(task.status)
    return f"""
    <tr data-task-id="{task.task_id}" data-task-status="{html.escape(task.status, quote=True)}">
      <td><a href="/task/{task.task_id}">#{task.task_id}</a></td>
      <td>{html.escape(task.workroot_name)}</td>
      <td>{html.escape(task.ai_config_name or '未记录')}</td>
      <td>{html.escape(task.record_id)}</td>
      <td>{html.escape(task.branch_name)}</td>
      <td class="{status_class}">{html.escape(task.status)}</td>
      <td>{html.escape(summarize_feishu_echo_state(task))}</td>
      <td>{html.escape(extract_task_problem_description(task))}</td>
      <td>{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.created_at))}</td>
    </tr>
    """


def render_dashboard_refresh_script() -> str:
    """渲染任务列表自动刷新脚本。

    优先使用 SSE；SSE 不可用时退回 3 秒轮询。刷新时只替换 tbody，避免整页闪烁。
    """

    return """
    <script>
    (function() {
      const tbody = document.getElementById('task-history-body');
      if (!tbody) return;
      let lastRowsHtml = tbody.innerHTML;
      let refreshing = false;
      let fallbackStarted = false;
      let knownStatuses = readTaskStatuses();

      function readTaskStatuses() {
        const statuses = new Map();
        tbody.querySelectorAll('tr[data-task-id]').forEach((row) => {
          statuses.set(row.dataset.taskId, row.dataset.taskStatus || '');
        });
        return statuses;
      }

      function playSoundsForCompletedTasks(nextStatuses) {
        nextStatuses.forEach((status, taskId) => {
          const previous = knownStatuses.get(taskId);
          if (previous && previous !== status && window.AutofixSound && window.AutofixSound.didEnterTerminal(previous, status)) {
            window.AutofixSound.playTaskComplete(status);
          }
        });
        knownStatuses = nextStatuses;
      }

      async function refresh() {
        if (refreshing) return;
        refreshing = true;
        try {
          const resp = await fetch('/api/tasks?limit=200', { cache: 'no-store' });
          if (!resp.ok) return;
          const data = await resp.json();
          if (!data.ok) return;
          const rowsHtml = data.rows_html || '<tr><td colspan="9" class="muted">暂无任务</td></tr>';
          if (rowsHtml === lastRowsHtml) return;
          lastRowsHtml = rowsHtml;
          tbody.innerHTML = rowsHtml;
          playSoundsForCompletedTasks(readTaskStatuses());
        } catch (err) {
          console.warn('task history refresh failed', err);
        } finally {
          refreshing = false;
        }
      }

      refresh();
      if (window.EventSource) {
        const source = new EventSource('/api/stream');
        source.addEventListener('change', refresh);
        source.onerror = function() {
          source.close();
          if (!fallbackStarted) {
            fallbackStarted = true;
            setInterval(() => {
              if (!document.hidden) refresh();
            }, 3000);
          }
        };
      } else {
        fallbackStarted = true;
        setInterval(() => {
          if (!document.hidden) refresh();
        }, 3000);
      }
    })();
    </script>
    """


def render_task_action_buttons(task: TaskRow) -> str:
    """按任务状态渲染详情页操作按钮。

    failed/超时 提供两种重试模式：保留当前 worktree 继续，或清空 worktree
    后重新修复；base_update_failed 只重试 Base 回写；queued 允许重新入队。
    暂停任务可立即开始或设为无需修复；所有任务都提供删除记录和删除记录并删分支。
    """

    parts: list[str] = []
    if task.status in {"failed", "超时"}:
        parts.append(
            f"""
            <form method="post" action="/task/{task.task_id}/retry-continue" onsubmit="return confirm('保留当前 worktree 的已有改动，并继续修复？');">
              <button class="btn primary" type="submit">基于当前 worktree 继续重试</button>
            </form>
            """
        )
        parts.append(
            f"""
            <form method="post" action="/task/{task.task_id}/retry-clean" onsubmit="return confirm('将删除当前任务 worktree 和对应分支，并清空 AI 输入/输出/摘要后重新修复？');">
              <button class="btn" type="submit">清空 worktree 后重新修复</button>
            </form>
            """
        )
    if task.status == "base_update_failed":
        parts.append(
            f"""
            <form method="post" action="/task/{task.task_id}/retry" onsubmit="return confirm('重新尝试回写多维表格完成状态？');">
              <button class="btn primary" type="submit">重试 Base 状态回写</button>
            </form>
            """
        )
    if task.status == "queued":
        parts.append(
            f"""
            <form method="post" action="/task/{task.task_id}/retry">
              <button class="btn primary" type="submit">重新入队</button>
            </form>
            """
        )
    if task.status == "暂停":
        parts.append(
            f"""
            <form method="post" action="/task/{task.task_id}/start-now">
              <button class="btn primary" type="submit">立即开始</button>
            </form>
            <form method="post" action="/task/{task.task_id}/no-repair" onsubmit="return confirm('将该任务设为无需修复？');">
              <button class="btn" type="submit">设为无需修复</button>
            </form>
            """
        )
    parts.append(
        f"""
        <form method="post" action="/task/{task.task_id}/delete" onsubmit="return confirm('删除该任务记录？');">
          <button class="btn danger" type="submit">删除记录</button>
        </form>
        """
    )
    parts.append(
        f"""
        <form method="post" action="/task/{task.task_id}/delete-worktree" onsubmit="return confirm('删除记录并删分支？');">
          <button class="btn danger" type="submit">删除记录并删分支</button>
        </form>
        """
    )
    return "".join(parts)


def render_round_detail_cards(round_history: list[dict[str, Any]]) -> str:
    """把每轮结构化修复摘要渲染成独立折叠详情。

    这里刻意不展示完整 Prompt 和 AI 输出；它们在任务详情页最下方单独展示。
    每轮卡片只放用户判断本轮质量最需要看的结构化信息。
    """

    if not round_history:
        return '<p class="muted">暂无轮次详情</p>'
    cards: list[str] = []
    for index, round_data in enumerate(round_history, start=1):
        decision = normalize_text_value(round_data.get("decision")) or "未完成"
        verify_output = normalize_text_value(round_data.get("verify_output"))
        format_error = normalize_text_value(round_data.get("format_error"))
        sections = [
            ("修复思路", normalize_text_value(round_data.get("repair_thinking_summary")) or "无"),
            ("本轮处理", normalize_text_value(round_data.get("round_action_summary")) or "无"),
            ("验证情况", normalize_text_value(round_data.get("round_validation_summary")) or "无"),
            ("风险说明", normalize_text_value(round_data.get("round_risk_summary")) or "无"),
        ]
        section_html = "\n".join(
            f"<p><strong>{html.escape(title)}:</strong> {html.escape(value)}</p>"
            for title, value in sections
        )
        verify_html = ""
        if verify_output:
            verify_label = "验证通过" if round_data.get("verify_ok") else "验证失败"
            verify_html = f"""
            <details class="collapsible" style="margin-top:12px;">
              <summary>{html.escape(verify_label)}</summary>
              <pre>{html.escape(verify_output)}</pre>
            </details>
            """
        format_error_html = f'<p class="status-bad">格式问题: {html.escape(format_error)}</p>' if format_error else ""
        cards.append(
            f"""
            <details class="collapsible panel">
              <summary>第 {index} 轮 - {html.escape(decision)}</summary>
              {section_html}
              {format_error_html}
              {verify_html}
            </details>
            """
        )
    return '<div class="grid round-detail-grid">' + "\n".join(cards) + "</div>"


def task_detail_payload(task: TaskRow) -> dict[str, Any]:
    """把 TaskRow 转换成详情页和 `/api/task/<id>` 共用的 JSON 载荷。"""

    round_history = parse_repair_rounds_json(task.round_history_json)
    return {
        "task_id": task.task_id,
        "status": task.status,
        "status_class": task_status_class(task.status),
        "round_count": task.round_count,
        "summary": task.summary or "",
        "commit_sha": task.commit_sha or "",
        "error": task.error or "",
        "prompt_text": task.prompt_text or "无",
        "reply_text": task.ai_reply_text or "无",
        "context_snapshot": task.context_snapshot or "无",
        "verify_output": task.verify_output or "无",
        "repair_thinking_summary": task.repair_thinking_summary or "无",
        "round_action_summary": task.round_action_summary or "无",
        "round_validation_summary": task.round_validation_summary or "无",
        "round_risk_summary": task.round_risk_summary or "无",
        "round_detail_html": render_round_detail_cards(round_history),
        "round_history_text": summarize_repair_rounds(round_history) or "无",
        "feishu_received_status": "收到失败" if task.feishu_received_error else ("已收到" if task.feishu_received_at else "未收到"),
        "feishu_received_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.feishu_received_at)) if task.feishu_received_at else "未发送",
        "feishu_received_text": task.feishu_received_text or "无",
        "feishu_received_error": task.feishu_received_error or "",
        "feishu_result_status": "结果失败" if task.feishu_result_error else ("已完成" if task.feishu_result_at else "未完成"),
        "feishu_result_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.feishu_result_at)) if task.feishu_result_at else "未发送",
        "feishu_result_text": task.feishu_result_text or "无",
        "feishu_result_error": task.feishu_result_error or "",
        "actions_html": render_task_action_buttons(task),
        "updated_at": max(
            task.created_at,
            task.started_at or 0,
            task.finished_at or 0,
            task.feishu_received_at,
            task.feishu_result_at,
        ),
    }


def render_message_page(message: str, *, status_class: str = "muted", back_href: str = "/", back_label: str = "返回") -> str:
    """渲染简单提示页，用于 404、表单校验失败等场景。"""

    return render_page(
        APP_NAME,
        render_template(
            "message.html",
            message_html=html.escape(message),
            status_class=status_class,
            back_href=html.escape(back_href),
            back_label=html.escape(back_label),
        ),
        header_actions_html=f'<a class="btn" href="{html.escape(back_href)}">{html.escape(back_label)}</a>',
    )


def nav_actions(*, current: str = "") -> str:
    """渲染顶部导航按钮，并高亮当前页。"""

    links = [
        ("任务", "/"),
        ("项目配置", "/config"),
        ("AI配置", "/ai"),
        ("提示词", "/prompt"),
    ]
    return "".join(
        f'<a class="btn{" primary" if label == current else ""}" href="{href}">{label}</a>'
        for label, href in links
    )


def render_prompt_page(store: AutofixStore, message: str = "") -> str:
    """渲染 Prompt 配置页。

    页面包含首次 Prompt、二次 Prompt，以及所有可用占位符说明。
    """

    return render_page(
        APP_NAME,
        render_template(
            "prompt_page.html",
            message_block=f'<p class="status-ok">{html.escape(message)}</p>' if message else "",
            prompt_template=html.escape(store.get_repair_prompt_template()),
            followup_prompt_template=html.escape(store.get_followup_prompt_template()),
        ),
        header_actions_html=nav_actions(current="提示词"),
    )


def render_ai_config_page(
    store: AutofixStore,
    runtime: RuntimeManager,
    message: str = "",
    edit_config: AIConfig | None = None,
) -> str:
    """渲染 AI 配置组管理页。

    同一个页面同时包含新增/编辑表单和配置组列表。edit_config 为空时显示新建；
    不为空时将该配置填入表单。
    """

    configs = store.list_ai_configs()
    selected_cli_type = edit_config.cli_type if edit_config else "codex"
    selected_provider = edit_config.ai_provider if edit_config else "gpt"
    rows = []
    for config in configs:
        status_class = "status-ok" if config.self_check_status == "passed" else "status-bad" if config.self_check_status == "failed" else ""
        enabled_class = "status-ok" if config.enabled else "status-bad"
        rows.append(
            f"""
            <tr>
              <td><span class="tag">{config.id}</span></td>
              <td>{html.escape(config.name)}</td>
              <td>{html.escape(cli_type_label(config.cli_type))}</td>
              <td>{html.escape(config.cli_path or '未设置')}</td>
              <td>{html.escape(ai_provider_label(config.ai_provider))}</td>
              <td>{html.escape(config.model_name or 'CLI 默认模型')}</td>
              <td>{html.escape(config.api_key_env or '未设置')}</td>
              <td>{html.escape(config.config_home or '未设置')}</td>
              <td class="{enabled_class}">{'启用' if config.enabled else '停用'}</td>
              <td class="{status_class}">{html.escape(config.self_check_status)}</td>
              <td>{html.escape(config.self_check_message)}</td>
              <td class="actions">
                <a class="btn" href="/ai/{config.id}/edit">编辑</a>
                <form method="post" action="/ai/{config.id}/test">
                  <button class="btn" type="submit">自检</button>
                </form>
                <form method="post" action="/ai/{config.id}/delete" onsubmit="return confirm('删除该 AI 配置组？');">
                  <button class="btn danger" type="submit">删除</button>
                </form>
              </td>
            </tr>
            """
        )
    return render_page(
        APP_NAME,
        render_template(
            "ai_config_page.html",
            message_block=f'<p class="status-ok">{html.escape(message)}</p>' if message else "",
            form_heading="编辑 AI 配置组" if edit_config else "新建 AI 配置组",
            ai_config_id=html.escape(str(edit_config.id) if edit_config else ""),
            enabled_checked=" checked" if (edit_config.enabled if edit_config else True) else "",
            name=html.escape(edit_config.name if edit_config else ""),
            cli_type_options=select_options(AI_CONFIG_CLI_TYPES, selected_cli_type),
            cli_path=html.escape(edit_config.cli_path if edit_config else ""),
            ai_provider_options=select_options(AI_BRAIN_TYPES, selected_provider),
            model_name=html.escape(edit_config.model_name if edit_config else ""),
            api_key_env=html.escape(edit_config.api_key_env if edit_config else ""),
            config_home=html.escape(edit_config.config_home if edit_config else ""),
            config_home_env=html.escape(edit_config.config_home_env if edit_config else default_config_home_env(selected_cli_type)),
            extra_args=html.escape(edit_config.extra_args if edit_config else ""),
            command_template=html.escape(edit_config.command_template if edit_config else ""),
            config_rows_html="".join(rows) or '<tr><td colspan="12" class="muted">暂无 AI 配置组</td></tr>',
        ),
        header_actions_html=nav_actions(current="AI配置"),
    )


def render_dashboard(store: AutofixStore, runtime: RuntimeManager, message: str = "") -> str:
    """渲染任务历史首页。"""

    tasks = store.list_tasks(limit=200)

    task_rows = [render_task_row(task) for task in tasks]

    return render_page(
        APP_NAME,
        render_template(
            "dashboard.html",
            message_block=f'<p class="status-ok">{html.escape(message)}</p>' if message else "",
            task_rows_html="".join(task_rows) or '<tr><td colspan="9" class="muted">暂无任务</td></tr>',
        ),
        header_actions_html=nav_actions(current="任务"),
        page_script_html=render_dashboard_refresh_script(),
    )


def render_config_page(store: AutofixStore, runtime: RuntimeManager, message: str = "") -> str:
    """渲染全局配置、新建项目配置表单和项目配置列表。"""

    settings = store.get_global_settings()
    configs = store.list_configs()
    ai_configs_by_id = {config.id: config for config in store.list_ai_configs()}
    default_ai_config = store.get_default_ai_config()
    active_ids = runtime.active_config_ids()
    defaults = infer_defaults(Path.cwd())

    config_rows = []
    for config in configs:
        active = "已激活" if config.enabled else "未激活"
        active_class = "status-ok" if config.enabled else "status-bad"
        running = "运行中" if config.id in active_ids else "停止"
        ai_config = ai_configs_by_id.get(config.ai_config_id) or default_ai_config
        ai_config_label = (
            f"{ai_config.name or cli_type_label(ai_config.cli_type)} / {ai_config.model_name or 'CLI 默认模型'}"
            if ai_config else "未绑定"
        )
        config_rows.append(
            f"""
            <tr>
              <td><span class="tag">{config.id}</span></td>
              <td>{html.escape(config.workroot_name or config.repo_root)}</td>
              <td>{html.escape(ai_config_label)}</td>
              <td>{html.escape(config.chat_name)}</td>
              <td>{html.escape(config.base_name or '未设置')}</td>
              <td>{html.escape(config.repair_version or '未设置')}</td>
              <td>{html.escape(describe_auto_repair_window(config))}</td>
              <td class="{active_class}">{active}</td>
              <td>{html.escape(running)}</td>
              <td>{html.escape(config.repo_root)}</td>
              <td>{html.escape(config.base_branch)}</td>
              <td>{html.escape(config.branch_prefix)}</td>
              <td class="actions">
                <form method="post" action="/config/{config.id}/toggle">
                  <button class="btn" type="submit">{'停用' if config.enabled else '激活'}</button>
                </form>
                <a class="btn" href="/config/{config.id}/edit">编辑</a>
                <form method="post" action="/config/{config.id}/init">
                  <button class="btn" type="submit">初始化查找</button>
                </form>
                <form method="post" action="/config/{config.id}/delete" onsubmit="return confirm('删除该项目配置？');">
                  <button class="btn danger" type="submit">删除</button>
                </form>
              </td>
            </tr>
            """
        )

    return render_page(
        APP_NAME,
        render_template(
            "config_page.html",
            message_block=f'<p class="status-ok">{html.escape(message)}</p>' if message else "",
            global_max_concurrent=html.escape(str(settings.max_concurrent)),
            global_timeout_seconds=html.escape(str(settings.timeout_seconds)),
            global_repair_max_loops=html.escape(str(settings.repair_max_loops)),
            ai_config_options_html=ai_config_options(store, default_ai_config.id if default_ai_config else 0),
            defaults_chat_name=html.escape(defaults["chat_name"]),
            defaults_app_name=html.escape(defaults["app_name"]),
            defaults_base_name=html.escape(defaults["base_name"]),
            defaults_status_field_name=html.escape(defaults["status_field_name"]),
            defaults_done_status_value=html.escape(defaults["done_status_value"]),
            defaults_repair_version=html.escape(defaults["repair_version"]),
            defaults_repo_root=html.escape(defaults["repo_root"]),
            defaults_base_branch=html.escape(defaults["base_branch"]),
            defaults_branch_prefix=html.escape(defaults["branch_prefix"]),
            defaults_worktree_root=html.escape(defaults["worktree_root"]),
            defaults_workroot_name=html.escape(defaults["workroot_name"]),
            defaults_processable_status_values=html.escape(defaults["processable_status_values"]),
            defaults_auto_repair_mode_options=auto_repair_mode_options(defaults["auto_repair_mode"]),
            defaults_auto_repair_start_time=html.escape(defaults["auto_repair_start_time"]),
            defaults_auto_repair_end_day_options=auto_repair_end_day_options(parse_non_negative_int(defaults["auto_repair_end_day_offset"], 0)),
            defaults_auto_repair_end_time=html.escape(defaults["auto_repair_end_time"]),
            defaults_context_collect_commands=html.escape(defaults["context_collect_commands"]),
            defaults_verify_commands=html.escape(defaults["verify_commands"]),
            config_rows_html="".join(config_rows) or '<tr><td colspan="13" class="muted">暂无项目配置</td></tr>',
        ),
        header_actions_html=nav_actions(current="项目配置"),
    )


def render_task_detail(store: AutofixStore, task: TaskRow | None) -> str:
    """渲染任务详情页。

    详情页展示任务元数据、飞书回显、AI 输入输出、上下文快照、验证输出和轮次历史，
    并通过 SSE/轮询实时刷新字段。
    """

    if task is None:
        return render_message_page("任务不存在。")

    detail = task_detail_payload(task)
    feishu_echo_html = f"""
    <div class="split" style="margin-top:16px;">
      {render_collapsible(
          "收到回显",
          f'''
          <p><code id="feishu-received-status">{html.escape(detail["feishu_received_status"])}</code> / <code id="feishu-received-at">{html.escape(detail["feishu_received_at"])}</code></p>
          <pre id="feishu-received-text">{html.escape(detail["feishu_received_text"])}</pre>
          <p id="feishu-received-error" class="status-bad">{html.escape(detail["feishu_received_error"])}</p>
          ''',
      )}
      {render_collapsible(
          "结果回显",
          f'''
          <p><code id="feishu-result-status">{html.escape(detail["feishu_result_status"])}</code> / <code id="feishu-result-at">{html.escape(detail["feishu_result_at"])}</code></p>
          <pre id="feishu-result-text">{html.escape(detail["feishu_result_text"])}</pre>
          <p id="feishu-result-error" class="status-bad">{html.escape(detail["feishu_result_error"])}</p>
          ''',
      )}
    </div>
    """
    refresh_script = f"""
    <script>
    (function() {{
      const taskId = {task.task_id};
      let lastStatus = {json.dumps(task.status, ensure_ascii=False)};
      let lastSignature = '';
      let fallbackStarted = false;

      function setText(id, value) {{
        const el = document.getElementById(id);
        if (el) el.textContent = value;
      }}

      function setHtml(id, value) {{
        const el = document.getElementById(id);
        if (el) el.innerHTML = value;
      }}

      function setStatusClass(value) {{
        const tag = document.querySelector('[data-task-status]');
        if (!tag) return;
        tag.className = 'tag ' + (value === 'succeeded' ? 'status-ok' : value === 'failed' || value === 'base_update_failed' || value === '超时' || value === '状态不支持' || value === '拒绝修复' ? 'status-bad' : '');
        tag.textContent = value;
      }}

      async function refreshTask() {{
        try {{
          const resp = await fetch(`/api/task/${{taskId}}`, {{ cache: 'no-store' }});
          if (!resp.ok) return;
          const data = await resp.json();
          if (!data.ok || !data.task) return;
          const task = data.task;
          const signature = JSON.stringify(task);
          if (signature === lastSignature) return;
          lastSignature = signature;
          if (lastStatus !== task.status && window.AutofixSound && window.AutofixSound.didEnterTerminal(lastStatus, task.status)) {{
            window.AutofixSound.playTaskComplete(task.status);
          }}
          lastStatus = task.status;
          setStatusClass(task.status);
          setText('task-round-count', task.round_count);
          setText('task-summary', task.summary || '无');
          setText('task-commit-sha', task.commit_sha || '');
          setText('task-error', task.error || '');
          setText('task-status-text', task.status);
          setText('task-prompt-text', task.prompt_text || '无');
          setText('task-reply-text', task.reply_text || '无');
          setText('task-context-snapshot-text', task.context_snapshot || '无');
          setText('task-verify-output-text', task.verify_output || '无');
          setText('task-repair-thinking-summary', task.repair_thinking_summary || '无');
          setText('task-round-action-summary', task.round_action_summary || '无');
          setText('task-round-validation-summary', task.round_validation_summary || '无');
          setText('task-round-risk-summary', task.round_risk_summary || '无');
          setHtml('task-round-detail-html', task.round_detail_html || '<p class="muted">暂无轮次详情</p>');
          setText('task-round-history-text', task.round_history_text || '无');
          setText('feishu-received-status', task.feishu_received_status);
          setText('feishu-received-at', task.feishu_received_at);
          setText('feishu-received-text', task.feishu_received_text);
          setText('feishu-received-error', task.feishu_received_error || '');
          setText('feishu-result-status', task.feishu_result_status);
          setText('feishu-result-at', task.feishu_result_at);
          setText('feishu-result-text', task.feishu_result_text);
          setText('feishu-result-error', task.feishu_result_error || '');
          setHtml('task-actions', task.actions_html || '');
        }} catch (err) {{
          console.warn('task refresh failed', err);
        }}
      }}

      refreshTask();
      if (window.EventSource) {{
        const source = new EventSource('/api/stream');
        source.addEventListener('change', refreshTask);
        source.onerror = function() {{
          source.close();
          if (!fallbackStarted) {{
            fallbackStarted = true;
            setInterval(() => {{
              if (!document.hidden) refreshTask();
            }}, 3000);
          }}
        }};
      }} else {{
        fallbackStarted = true;
        setInterval(() => {{
          if (!document.hidden) refreshTask();
        }}, 3000);
      }}
    }})();
    </script>
    """
    return render_page(
        APP_NAME,
        render_template(
            "task_detail.html",
            task_id=html.escape(str(task.task_id)),
            workroot_name=html.escape(task.workroot_name),
            ai_config_name=html.escape(task.ai_config_name or "未记录"),
            cli_type=html.escape(cli_type_label(task.cli_type)),
            ai_provider=html.escape(ai_provider_label(task.ai_provider)),
            ai_model=html.escape(task.ai_model or "CLI 默认模型"),
            cli_path=html.escape(task.cli_path),
            status=html.escape(task.status),
            record_id=html.escape(task.record_id),
            branch_name=html.escape(task.branch_name),
            worktree_path=html.escape(task.worktree_path),
            repair_version=html.escape(task.repair_version),
            status_field_name=html.escape(task.status_field_name),
            done_status_value=html.escape(task.done_status_value),
            round_count=html.escape(str(detail["round_count"])),
            delete_task_action=f"/task/{task.task_id}/delete",
            delete_worktree_action=f"/task/{task.task_id}/delete-worktree",
            actions_html=detail["actions_html"],
            feishu_echo_html=feishu_echo_html,
            prompt_text=html.escape(detail["prompt_text"]),
            reply_text=html.escape(detail["reply_text"]),
            context_snapshot=html.escape(detail["context_snapshot"]),
            verify_output=html.escape(detail["verify_output"]),
            repair_thinking_summary=html.escape(detail["repair_thinking_summary"]),
            round_action_summary=html.escape(detail["round_action_summary"]),
            round_validation_summary=html.escape(detail["round_validation_summary"]),
            round_risk_summary=html.escape(detail["round_risk_summary"]),
            round_detail_html=detail["round_detail_html"],
            round_history_text=html.escape(detail["round_history_text"]),
            summary=html.escape(detail["summary"]),
            commit_sha=html.escape(detail["commit_sha"]),
            error=html.escape(detail["error"]),
        ),
        header_actions_html=nav_actions(),
        page_script_html=refresh_script,
    )


def render_edit_config(store: AutofixStore, config: ProjectConfig | None, message: str = "") -> str:
    """渲染单个项目配置的编辑页。"""

    defaults = build_form_config(config, defaults=infer_defaults(Path.cwd()))
    config_id = str(config.id) if config else ""
    enabled = config.enabled if config else False
    return render_page(
        APP_NAME,
        render_template(
            "edit_config.html",
            message_block=f'<p class="status-ok">{html.escape(message)}</p>' if message else "",
            page_heading="编辑项目配置" if config else "新建项目配置",
            config_id=html.escape(config_id),
            hidden_chat_id=html.escape(config.chat_id if config else ""),
            config_enabled_checked=" checked" if enabled else "",
            ai_config_options_html=ai_config_options(store, config.ai_config_id if config else 0),
            chat_name=html.escape(defaults["chat_name"]),
            app_name=html.escape(defaults["app_name"]),
            base_name=html.escape(defaults["base_name"]),
            status_field_name=html.escape(defaults["status_field_name"]),
            done_status_value=html.escape(defaults["done_status_value"]),
            processable_status_values=html.escape(defaults["processable_status_values"]),
            repair_version=html.escape(defaults["repair_version"]),
            repo_root=html.escape(defaults["repo_root"]),
            base_branch=html.escape(defaults["base_branch"]),
            branch_prefix=html.escape(defaults["branch_prefix"]),
            worktree_root=html.escape(defaults["worktree_root"]),
            workroot_name=html.escape(defaults["workroot_name"]),
            auto_repair_mode_options=auto_repair_mode_options(defaults["auto_repair_mode"]),
            auto_repair_start_time=html.escape(defaults["auto_repair_start_time"]),
            auto_repair_end_day_options=auto_repair_end_day_options(parse_non_negative_int(defaults["auto_repair_end_day_offset"], 0)),
            auto_repair_end_time=html.escape(defaults["auto_repair_end_time"]),
            context_collect_commands=html.escape(defaults["context_collect_commands"]),
            verify_commands=html.escape(defaults["verify_commands"]),
        ),
        header_actions_html=nav_actions(),
    )
