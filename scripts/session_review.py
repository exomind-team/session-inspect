#!/usr/bin/env python3
"""
Unified Session Review — Codex + Claude Code session radar.

Default behavior scans both platforms. --platform narrows the result set.
"""
import argparse
import json
import platform as platform_module
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import scan_claude
import scan_codex
from scan_common import dump_json, make_record, public_record, sanitize_cell, short_cwd


SCRIPT_VERSION = "0.6.0"
DEFAULT_ACTION_LIMIT = 12


def scan_all(platform="all", days=7, project=None, grep=None, session_id=None, group_by="thread"):
    records = []
    if platform in ("all", "codex"):
        records.extend(scan_codex.scan(days=days, project=project, grep=grep, session_id=session_id))
    if platform in ("all", "claude"):
        records.extend(scan_claude.scan(days=days, project=project, grep=grep, session_id=session_id))
    records.sort(key=lambda r: r["last_active_ts"], reverse=True)
    annotate_action_fields(records)
    if group_by == "cwd" and not session_id:
        records = group_records_by_cwd(records)
        annotate_action_fields(records)
    return records


def group_records_by_cwd(records):
    groups = defaultdict(list)
    for record in records:
        key = record.get("cwd") or record.get("display_name") or record.get("id")
        groups[key].append(record)

    grouped = []
    for cwd, items in groups.items():
        items.sort(key=lambda r: r["last_active_ts"], reverse=True)
        latest = items[0]
        platforms = sorted(set(i["platform"] for i in items))
        first_msg = ""
        last_msg = ""
        for item in reversed(items):
            if item.get("first_message"):
                first_msg = item["first_message"]
                break
        for item in items:
            if item.get("last_message"):
                last_msg = item["last_message"]
                break
        rec = make_record(
            platform="+".join(platforms),
            id=latest.get("id", ""),
            title=latest.get("title") or latest.get("display_name"),
            cwd=cwd,
            display_name=latest.get("display_name") or short_cwd(cwd),
            last_active_dt=datetime.fromtimestamp(latest["last_active_ts"]),
            user_turns=sum(i.get("user_turns", 0) for i in items),
            agent_turns=sum(i.get("agent_turns", 0) for i in items),
            session_count=sum(i.get("session_count", 0) for i in items),
            rollout_count=sum(i.get("rollout_count", 0) for i in items),
            total_size_bytes=int(sum(i.get("total_size_mb", 0) for i in items) * 1024 * 1024),
            first_message=first_msg,
            last_message=last_msg,
            grep_text=" ".join(i.get("_grep_text", "") for i in items),
            source_path=latest.get("source_path", ""),
        )
        rec["workspace"] = short_cwd(cwd)
        rec["group_items"] = len(items)
        grouped.append(rec)
    grouped.sort(key=lambda r: r["last_active_ts"], reverse=True)
    return grouped


def annotate_action_fields(records):
    for record in records:
        state, reasons, score = infer_action_state(record)
        record["completion_state"] = state
        record["action_score"] = score
        record["action_reasons"] = reasons
        record["next_action_hint"] = next_action_hint(record, state)


def infer_action_state(record):
    text = " ".join([
        record.get("summary", ""),
        record.get("first_message", ""),
        record.get("last_message", ""),
    ]).lower()
    reasons = []
    score = 0
    days = record.get("days_since_active", 9999)
    user_turns = record.get("user_turns", 0)
    agent_turns = record.get("agent_turns", 0)
    total_turns = user_turns + agent_turns

    if days <= 1:
        score += 30
        reasons.append("最近 24 小时活跃")
    elif days <= 3:
        score += 18
        reasons.append("最近 3 天活跃")
    elif days <= 7:
        score += 8
        reasons.append("一周内活跃")

    if total_turns >= 500:
        score += 20
        reasons.append("长线程，恢复价值高")
    elif total_turns >= 80:
        score += 12
        reasons.append("中等以上工作量")
    elif total_turns >= 20:
        score += 6
        reasons.append("有一定工作量")

    interrupted_markers = [
        "request interrupted", "request cancelled", "interrupted",
        "cancelled", "中断", "取消", "crash", "崩溃",
    ]
    waiting_markers = [
        "等待", "await", "待确认", "需要确认", "pending", "blocked",
        "等你", "请确认", "需要用户",
    ]
    complete_markers = [
        "已完成", "完成了", "收口", "提交", "pushed", "merged",
        "验证通过", "validation-ok", "completed", "done",
    ]
    handoff_markers = [
        "下一步", "继续", "接手", "恢复", "todo", "计划", "plan",
        "follow up", "后续",
    ]
    noise_markers = [
        "任务通知", "task-notification", "只读调查", "reviewer",
        "blind", "skill judge", "subagent", "无上下文",
    ]

    if any(marker in text for marker in interrupted_markers):
        score += 35
        reasons.append("出现中断/取消信号")
        state = "interrupted"
    elif any(marker in text for marker in waiting_markers):
        score += 25
        reasons.append("出现等待/阻塞信号")
        state = "waiting"
    elif any(marker in text for marker in handoff_markers):
        score += 18
        reasons.append("出现继续/下一步信号")
        state = "handoff-ready"
    elif any(marker in text for marker in complete_markers):
        score -= 10
        reasons.append("出现完成/提交信号")
        state = "likely-complete"
    else:
        state = "unknown"

    if any(marker in text for marker in noise_markers):
        score -= 18
        reasons.append("可能是通知/子代理/评审噪音")
        if state == "unknown":
            state = "background/noise"

    if user_turns == 0 and agent_turns <= 2:
        score -= 15
        reasons.append("交互轮数很少")

    return state, reasons[:4], score


def next_action_hint(record, state):
    if state in ("interrupted", "waiting", "handoff-ready", "unknown"):
        return f"恢复该会话并检查最后上下文：{record.get('id')}"
    if state == "likely-complete":
        return "低优先级：先确认是否已提交/汇报"
    if state == "background/noise":
        return "低优先级：可能只需略过或查看父会话"
    return "按需查看"


def print_markdown(records, *, platform, days, session_id=None, group_by="thread"):
    now = datetime.now()
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else "全部"
    scope = "Codex + Claude Code" if platform == "all" else platform
    if session_id:
        print(f"=== Session Review: {session_id} ({scope}) ===")
    else:
        print(f"=== Session Review ({scope}, {start} ~ {now.strftime('%Y-%m-%d')}) ===")
    print(f"=== 共 {len(records)} 条记录，视图: {group_by} ===\n")
    if not records:
        print("未找到匹配会话。")
        return

    print("| 状态 | 来源 | 线程/项目 | 工作目录 | 最后活跃 | 轮数 | 摘要 |")
    print("|------|------|-----------|----------|----------|------|------|")
    for record in records:
        turns = f"{record.get('user_turns', 0)}/{record.get('agent_turns', 0)}"
        label = record.get("title") or record.get("display_name") or record.get("id")
        workspace = record.get("workspace") or short_cwd(record.get("cwd", ""))
        print(
            "| {status} | {platform} | {label} | {workspace} | {last_active} | {turns} | {summary} |".format(
                status=sanitize_cell(record.get("status")),
                platform=sanitize_cell(record.get("platform")),
                label=sanitize_cell(label),
                workspace=sanitize_cell(workspace),
                last_active=sanitize_cell(record.get("last_active")),
                turns=sanitize_cell(turns),
                summary=sanitize_cell(record.get("summary")),
            )
        )

    focus = build_focus_sections(records)
    if focus:
        print("\n---\n")
        print("### 焦点提示\n")
        for line in focus:
            print(line)


def print_detail(records, *, platform, days, session_id=None, group_by="thread"):
    now = datetime.now()
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else "全部"
    scope = "Codex + Claude Code" if platform == "all" else platform
    if session_id:
        print(f"=== Session Review Detail: {session_id} ({scope}) ===")
    else:
        print(f"=== Session Review Detail ({scope}, {start} ~ {now.strftime('%Y-%m-%d')}) ===")
    print(f"=== 共 {len(records)} 条记录，视图: {group_by} ===\n")
    if not records:
        print("未找到匹配会话。")
        return

    for idx, record in enumerate(records, 1):
        label = record.get("title") or record.get("display_name") or record.get("id")
        count_label = "rollout" if record.get("platform") == "codex" else "session"
        count_value = record.get("session_count_or_rollout_count", 0)
        print(f"## {idx}. {sanitize_cell(label)}")
        print(f"状态: {sanitize_cell(record.get('status'))}")
        print(f"来源: {sanitize_cell(record.get('platform'))}")
        print(f"ID: `{sanitize_cell(record.get('id'))}`")
        print(f"工作目录: `{sanitize_cell(record.get('cwd') or record.get('workspace'))}`")
        print(f"最后活跃: {sanitize_cell(record.get('last_active'))} ({record.get('days_since_active')} 天前)")
        print(f"轮数: 用户 {record.get('user_turns', 0)} / Agent {record.get('agent_turns', 0)}")
        print(f"{count_label} 数: {count_value}")
        print(f"总大小: {record.get('total_size_mb', 0)} MB")
        print(f"摘要: {sanitize_cell(record.get('summary'))}")
        if record.get("source_path"):
            print(f"来源文件: `{sanitize_cell(record.get('source_path'))}`")
        first = detail_snippet(record.get("first_message"))
        last = detail_snippet(record.get("last_message"))
        if first:
            print("首条可读消息:")
            print(f"> {first}")
        if last and last != first:
            print("末条可读消息:")
            print(f"> {last}")
        print()


def detail_snippet(value, limit=360):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return sanitize_cell(text)


def print_action_view(records, *, platform, days, shell, limit, codex_cli, claude_cli):
    selected = select_action_records(records, limit)
    print_action_header("Next Action Queue", platform, days, len(records), len(selected))
    if not selected:
        print("没有找到建议接手的会话。")
        return
    actual_shell = resolve_shell(shell)
    for idx, record in enumerate(selected, 1):
        print_action_item(idx, record, actual_shell, codex_cli, claude_cli)


def print_restore_view(records, *, platform, days, shell, limit, codex_cli, claude_cli):
    selected = select_action_records(records, limit)
    actual_shells = ["powershell", "bash"] if shell == "all" else [resolve_shell(shell)]
    print_action_header("Restore Window View", platform, days, len(records), len(selected))
    if not selected:
        print("没有找到建议恢复的会话。")
        return

    print("选择规则: 最近活跃 + 中断/等待/继续信号 + 工作量；通知/子代理噪音会降权。")
    print()
    for actual_shell in actual_shells:
        print(f"## {shell_label(actual_shell)}：任意位置直接复制")
        for idx, record in enumerate(selected, 1):
            label = record.get("title") or record.get("display_name") or record.get("id")
            print(f"### {idx}. {sanitize_cell(label)}")
            print(f"- {record.get('platform')} / {record.get('last_active')} / {record.get('completion_state')}")
            print("```" + shell_fence(actual_shell))
            print(resume_command(record, actual_shell, codex_cli, claude_cli, include_cd=True))
            print("```")
        print()
        print(f"## {shell_label(actual_shell)}：按目录分组")
        for cwd, items in group_selected_by_cwd(selected).items():
            print(f"### {sanitize_cell(cwd)}")
            print("```" + shell_fence(actual_shell))
            print(cd_command(cwd, actual_shell))
            for record in items:
                print(resume_command(record, actual_shell, codex_cli, claude_cli, include_cd=False))
            print("```")
        if actual_shell == "powershell":
            print()
            print("## Windows Terminal：新标签页命令")
            print("每条命令会在对应工作目录打开一个新标签页并执行 resume。")
            for record in selected:
                print("```powershell")
                print(windows_terminal_command(record, codex_cli, claude_cli))
                print("```")
        print()


def print_action_header(title, platform, days, total_count, selected_count):
    now = datetime.now()
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else "全部"
    scope = "Codex + Claude Code" if platform == "all" else platform
    print(f"=== {title} ({scope}, {start} ~ {now.strftime('%Y-%m-%d')}) ===")
    print(f"=== 候选 {selected_count} / 扫描 {total_count} 条 ===\n")


def print_action_item(idx, record, shell, codex_cli, claude_cli):
    label = record.get("title") or record.get("display_name") or record.get("id")
    reasons = "；".join(record.get("action_reasons") or ["无明显信号"])
    print(f"## {idx}. {sanitize_cell(label)}")
    print(f"- 状态: {record.get('completion_state')} / 分数: {record.get('action_score')}")
    print(f"- 来源: {record.get('platform')} / 最后活跃: {record.get('last_active')}")
    print(f"- 工作目录: `{sanitize_cell(record.get('cwd'))}`")
    print(f"- ID: `{sanitize_cell(record.get('id'))}`")
    print(f"- 理由: {sanitize_cell(reasons)}")
    print(f"- 下一步: {sanitize_cell(record.get('next_action_hint'))}")
    print("```" + shell_fence(shell))
    print(resume_command(record, shell, codex_cli, claude_cli, include_cd=True))
    print("```")
    print()


def select_action_records(records, limit):
    ranked = sorted(
        records,
        key=lambda r: (r.get("action_score", 0), r.get("last_active_ts", 0)),
        reverse=True,
    )
    selected = [r for r in ranked if r.get("action_score", 0) > 0 and r.get("id")]
    if not selected:
        selected = [r for r in ranked if r.get("id")]
    if limit is None:
        limit = DEFAULT_ACTION_LIMIT
    if limit > 0:
        return selected[:limit]
    return selected


def group_selected_by_cwd(records):
    groups = defaultdict(list)
    for record in records:
        groups[record.get("cwd") or "~"].append(record)
    return dict(sorted(groups.items(), key=lambda item: max(r.get("action_score", 0) for r in item[1]), reverse=True))


def resolve_shell(shell):
    if shell != "auto":
        return shell
    if platform_module.system().lower().startswith("win"):
        return "powershell"
    return "bash"


def shell_label(shell):
    return "PowerShell" if shell == "powershell" else "Bash"


def shell_fence(shell):
    return "powershell" if shell == "powershell" else "bash"


def resume_command(record, shell, codex_cli, claude_cli, include_cd=True):
    cli = codex_cli if record.get("platform") == "codex" else claude_cli
    session_id = record.get("id", "")
    if record.get("platform") == "claude":
        resume_part = f"{cli} --resume {session_id}"
    else:
        resume_part = f"{cli} resume {session_id}"
    if not include_cd:
        return resume_part
    cwd = record.get("cwd") or "."
    if shell == "powershell":
        return f"{cd_command(cwd, shell)}; {resume_part}"
    return f"{cd_command(cwd, shell)} && {resume_part}"


def cd_command(cwd, shell):
    if shell == "powershell":
        return f"Set-Location -LiteralPath {quote_powershell(cwd)}"
    return f"cd {quote_bash(cwd)}"


def windows_terminal_command(record, codex_cli, claude_cli):
    cli = codex_cli if record.get("platform") == "codex" else claude_cli
    session_id = record.get("id", "")
    resume_part = f"{cli} --resume {session_id}" if record.get("platform") == "claude" else f"{cli} resume {session_id}"
    title = sanitize_for_wt_title(record.get("title") or record.get("display_name") or session_id)
    cwd = record.get("cwd") or "."
    return (
        f"wt new-tab --title {quote_windows_arg(title)} "
        f"-d {quote_windows_arg(cwd)} powershell -NoExit -Command {quote_windows_arg(resume_part)}"
    )


def quote_powershell(value):
    return "'" + str(value or ".").replace("'", "''") + "'"


def quote_bash(value):
    text = str(value or ".")
    return "'" + text.replace("'", "'\"'\"'") + "'"


def quote_windows_arg(value):
    text = str(value or "").replace('"', '\\"')
    return f'"{text}"'


def sanitize_for_wt_title(value):
    return " ".join(str(value or "session").replace('"', "").split())[:48] or "session"


def build_focus_sections(records):
    lines = []
    hot = [
        r for r in records
        if r.get("days_since_active", 9999) <= 1
        and (r.get("session_count_or_rollout_count", 0) >= 3 or r.get("user_turns", 0) > 30)
    ]
    if hot:
        lines.append("🔥 **热点会话**")
        for r in hot[:5]:
            lines.append(
                f"- **{sanitize_cell(r.get('title') or r.get('display_name'))}** — "
                f"{r.get('platform')}, {r.get('user_turns')} 用户轮, {sanitize_cell(r.get('summary'))}"
            )
    risk = [
        r for r in records
        if r.get("days_since_active", 0) > 5 and r.get("user_turns", 0) > 0
    ]
    if risk:
        lines.append("⚠️ **遗忘风险**")
        for r in risk[:5]:
            lines.append(
                f"- **{sanitize_cell(r.get('title') or r.get('display_name'))}** — "
                f"停了 {r.get('days_since_active')} 天, {r.get('user_turns')} 用户轮"
            )
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scan Codex and Claude Code sessions")
    parser.add_argument("--platform", choices=["all", "codex", "claude"], default="all",
                        help="Filter platform; default scans all platforms")
    parser.add_argument("--days", type=int, default=7, help="Scan recent N days; 0 means all")
    parser.add_argument("--session", help="Cross-platform session/thread UUID lookup")
    parser.add_argument("--uuid", help="Alias for --session")
    parser.add_argument("--project", help="Filter by title/display_name/cwd/id")
    parser.add_argument("--grep", help="Filter by readable user/agent text")
    parser.add_argument("--group-by", choices=["thread", "cwd"], default="thread",
                        help="Default groups by session/thread; cwd groups by workspace")
    parser.add_argument("--detail", action="store_true",
                        help="Print expanded per-record markdown for omission checks")
    parser.add_argument("--action-view", action="store_true",
                        help="Print a ranked next-action queue")
    parser.add_argument("--restore-view", action="store_true",
                        help="Print copy-ready resume commands for active sessions")
    parser.add_argument("--shell", choices=["auto", "powershell", "bash", "all"], default="auto",
                        help="Shell for generated resume commands")
    parser.add_argument("--limit", type=int, default=DEFAULT_ACTION_LIMIT,
                        help="Limit action/restore candidates; 0 means all")
    parser.add_argument("--codex-cli", default="codex",
                        help="Command name/path to use for Codex CLI resume commands")
    parser.add_argument("--claude-cli", default="claude",
                        help="Command name/path to use for Claude Code resume commands")
    parser.add_argument("--json", action="store_true", help="Print JSON array")
    parser.add_argument("--version", action="store_true", help="Print version")
    args = parser.parse_args(argv)

    if args.version:
        print(f"session-review {SCRIPT_VERSION}")
        return 0

    session_id = args.session or args.uuid
    group_by = "thread" if (args.action_view or args.restore_view) else args.group_by
    records = scan_all(
        platform=args.platform,
        days=args.days,
        project=args.project,
        grep=args.grep,
        session_id=session_id,
        group_by=group_by,
    )
    if args.json:
        dump_json(records)
    elif args.action_view:
        print_action_view(records, platform=args.platform, days=args.days,
                          shell=args.shell, limit=args.limit,
                          codex_cli=args.codex_cli, claude_cli=args.claude_cli)
    elif args.restore_view:
        print_restore_view(records, platform=args.platform, days=args.days,
                           shell=args.shell, limit=args.limit,
                           codex_cli=args.codex_cli, claude_cli=args.claude_cli)
    elif args.detail:
        print_detail(records, platform=args.platform, days=args.days,
                     session_id=session_id, group_by=group_by)
    else:
        print_markdown(records, platform=args.platform, days=args.days,
                       session_id=session_id, group_by=group_by)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
