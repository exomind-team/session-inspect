# -*- coding: utf-8 -*-
"""
DeepSeek 聊天**树**的通用原语。

DeepSeek 的会话是树不是列表：编辑提问 / 重新生成答案都会 fork 出新分支。
三种容器（账号导出包 / 分享链接 / 登录态私密会话）里，前两种在 skill 里
已有各自的提取器，但**树的读取、裁剪、渲染**这套操作一直没有原语——
结果是每遇到一次「要按分支看会话」就手搓一遍，而且**手搓的版本会静默出错**。

为什么必须做成原语（一次真实事故）：
    手写渲染器时按 `children[0]` 走「主链」，在第一个分叉点
    （n6 → [7, 9]）挑中了**被改写前的旧草稿 n7**，于是 n8 就到底了——
    静默丢掉 80/122 个节点、正文短 18%。**不报错，只是少了一大截**。
    这类错误只有「全覆盖断言」能抓：算出的分节必须恰好覆盖全部节点。

本模块提供四件事：
    1. main_path_ids   —— 从 current_message_id 沿 parent 回溯，得网页显示的那条链
    2. branch_index    —— 主链上每个节点挂出去的支线（含子树），供裁剪/保留
    3. select_branches —— 按分支 id/序号裁剪或保留，产出「只含选定分支」的树
    4. render_markdown —— 分支感知的可读 Markdown + **全覆盖断言**

节点形状（三种容器归一后一致，见 build_mapping / normalise）：
    {id, parent, children[], message}       message 可为 None
"""


# ============================================================
# 主链
# ============================================================

def main_path_ids(mapping, current_id=None):
    """
    主链节点 id 列表（从根到 current），**网页上实际显示的那条**。

    优先用 `current_message_id`（DeepSeek 前端记录的「当前所在节点」）
    沿 parent 回溯；没有就退回「从 root 沿 children 深度优先的第一条路径」，
    但那只是**猜测**——猜错会静默丢掉兄弟分支，所以调用方应尽量给 current_id。

    返回顺序：从根到叶（与阅读顺序一致）。
    """
    mapping = mapping or {}
    if current_id is not None:
        chain = []
        seen = set()
        nid = str(current_id)
        while nid and nid not in seen:
            seen.add(nid)
            node = mapping.get(nid)
            if node is None:
                break
            chain.append(nid)
            parent = node.get("parent")
            nid = str(parent) if parent else None
        chain.reverse()
        # 去掉哨兵 root（它通常没有 message）
        return [n for n in chain if n != "root"]

    # 退化路径：从 root 沿第一个孩子一路走到底（**可能不是用户实际那条**）
    out = []
    node = mapping.get("root") or {}
    children = list(node.get("children") or [])
    while children:
        nid = str(children[0])
        if nid in out:
            break
        out.append(nid)
        node = mapping.get(nid) or {}
        children = list(node.get("children") or [])
    return out


def subtree(mapping, root_id):
    """DFS 收集一棵子树（含自己）的节点 id，深度优先顺序。"""
    mapping = mapping or {}
    out = []

    def walk(nid):
        if nid in out:  # 防御：畸形树不死循环
            return
        out.append(nid)
        for ch in (mapping.get(nid) or {}).get("children") or []:
            walk(str(ch))

    walk(str(root_id))
    return out


def num_key(x):
    """节点 id 排序键：数字字符串按数值排，非数字按字符串排。

    ⚠️ DeepSeek 的 node id 是数字字符串，但**不是所有容器都保证**是数字。
    直接 `int()` 会在混合 id 上抛错；按 (是否数字, 值) 双段排序两种都能排。
    """
    s = str(x)
    return (0, int(s)) if s.isdigit() else (1, s)


# ============================================================
# 支线索引
# ============================================================

def branch_index(mapping, main_ids=None, current_id=None):
    """
    支线索引：主链上每个节点挂出去的、**不在主链里**的子树。

    返回 [(fork_node, [node_ids...]), ...]，按主链顺序。
    ⚠️ 键是**分叉点**（挂在主链哪个节点上），不是支线起点——
    同一个分叉点可能挂多条支线（多次重生成）。
    """
    mapping = mapping or {}
    if main_ids is None:
        main_ids = main_path_ids(mapping, current_id)
    main_set = set(main_ids)

    branches = []
    # ⚠️ **root 也是分叉点**：改「第一个问题」会得到两个顶层节点，
    # 第二个不在主链上、也不是任何主链节点的孩子——若只扫主链节点
    # 的 children，它会既不在主链、也不在任何支线，静默从覆盖率里消失
    # （`select_branches(..., "all")` 会因此悄悄丢掉半棵树）。
    # 所以先把 root 当作分叉点扫一遍。
    for nid in ["root"] + list(main_ids):
        for ch in (mapping.get(nid) or {}).get("children") or []:
            ch = str(ch)
            if ch in main_set:
                continue
            nodes = subtree(mapping, ch)
            if all(n not in main_set for n in nodes):
                branches.append((nid, nodes))
    return branches


def branch_start_nodes(mapping, main_ids=None, current_id=None):
    """每条支线的**起点节点**（= 该支线子树的根），按主链顺序。"""
    return [nodes[0] for _fork, nodes in branch_index(mapping, main_ids, current_id)]


def coverage(mapping, main_ids, branches):
    """
    覆盖率检查：主链 + 全部支线是否**恰好**覆盖所有非 root 节点。

    返回 (covered, all_nodes, missing, extra)。
    ⚠️ 这是本模块存在的核心价值——手搓渲染器丢掉 18% 正文那次，
    就是缺了这个断言。渲染前必须过这一关。

    ⚠️ 另有一条容易被漏掉的不一致：`parent` 声称挂在 root 上、
    但 root 的 `children` 里**没有它**（畸形树）。这种节点既不在主链、
    也不在任何支线，会从覆盖率里漏过去。所以这里额外核对
    **`parent` / `children` 的双向一致性**。
    """
    mapping = mapping or {}
    covered = set(str(n) for n in main_ids)
    for _fork, nodes in branches:
        covered |= set(str(n) for n in nodes)
    all_nodes = {str(n) for n in mapping if str(n) != "root"}

    # 双向一致性：找出**既不在主干路径、也无法从 root 走到**的节点。
    #
    # ⚠️ 这里有**两种合法形态**，不能把第二种误判成损坏：
    #   (a) 顶层节点 `parent = "root"`，root.children 收录它 —— 新适配器/导出包
    #   (b) 顶层节点 `parent = None`，但 root.children 仍收录它 —— 历史产物就是这样
    # 所以判据不是「parent 是否为 root」，而是**能不能从 root 走到**。
    # 真正的病态形态（必须报出来）：节点 parent 是 None 且 root.children 里没有它
    # （或 parent 指向一个不存在的节点）——它既不在主链也不在支线，会静默漏渲染。
    def _reachable_from_root():
        seen = set()
        stack = [str(c) for c in (mapping.get("root") or {}).get("children") or []]
        while stack:
            nid = stack.pop()
            if nid in seen or nid not in mapping:
                continue
            seen.add(nid)
            stack.extend(str(c) for c in (mapping.get(nid) or {}).get("children") or [])
        return seen

    reachable = _reachable_from_root()
    inconsistent = []
    for nid, node in mapping.items():
        nid = str(nid)
        if nid == "root":
            continue
        parent = (node or {}).get("parent")
        if parent is not None and str(parent) not in mapping:
            inconsistent.append(nid)      # parent 悬空
        elif nid not in reachable:
            inconsistent.append(nid)      # 从 root 走不到
    missing = sorted(all_nodes - covered)
    for nid in inconsistent:
        if nid in all_nodes and nid not in missing:
            missing.append(nid)
    return covered, all_nodes, sorted(set(missing)), sorted(covered - all_nodes)


# ============================================================
# 裁剪 / 保留
# ============================================================

def select_branches(mapping, spec, current_id=None):
    """
    按分支选择产出「裁剪后的树」。

    spec 的三种形态：
      - None 或 "all"        → 全部保留（主链 + 所有支线）
      - "main"               → 只留主链（裁掉所有支线）
      - ["main", 0, 2]       → 主链 + 第 0、2 条支线
      - ["<node_id>", ...]   → 按**支线起点节点 id** 选择（更稳，不怕重排）

    返回 (kept_mapping, report)：
      kept_mapping —— 只含被保留节点的 mapping（parent/children 已按保留集重连）
      report       —— {main:[...], branches:[{index,start,fork,node_ids}], dropped:[...]}

    ⚠️ 裁剪**不改动源**：返回的是新 dict，原 mapping 一字不动。
    """
    mapping = mapping or {}
    main_ids = main_path_ids(mapping, current_id)
    branches = branch_index(mapping, main_ids, current_id)

    if spec is None or spec == "all":
        keep_main, keep_branch_idx = True, list(range(len(branches)))
    elif spec == "main":
        keep_main, keep_branch_idx = True, []
    else:
        if isinstance(spec, (str, int)):
            spec = [spec]
        keep_main = "main" in spec
        wanted = [s for s in spec if s != "main"]
        keep_branch_idx = []
        for w in wanted:
            # ⚠️ DeepSeek 的节点 id 本身就是**数字字符串**，所以 `"5"` 既可能是
            # 「第 5 条支线」也可能是「起点节点 n5」。规则：**节点 id 优先**——
            # 先看它是不是某条支线的起点节点；不是才当序号解释。
            # （int 类型则明确是序号，不走这条。）
            if not isinstance(w, int):
                hit = [i for i, (_f, nodes) in enumerate(branches)
                       if str(nodes[0]) == str(w)]
                if hit:
                    keep_branch_idx.append(hit[0])
                    continue
            if isinstance(w, int) or (isinstance(w, str) and w.isdigit()):
                idx = int(w)
                if 0 <= idx < len(branches):
                    keep_branch_idx.append(idx)
                else:
                    starts = [str(nodes[0]) for _f, nodes in branches]
                    raise ValueError(
                        "no branch matches %r：既不是序号（有效 0..%d），"
                        "也不是支线起点节点（起点为 %s）"
                        % (w, len(branches) - 1, starts or "无"))
            else:
                starts = [str(nodes[0]) for _f, nodes in branches]
                raise ValueError("no branch starts at node %s (起点为 %s)"
                                 % (w, starts or "无"))

    kept = set(main_ids) if keep_main else set()
    for i in keep_branch_idx:
        kept |= set(branches[i][1])

    # 断言：**保留集必须覆盖整棵树**，除非是刻意裁剪。
    # ⚠️ `spec is None or "all"` 的分支不允许丢节点——那正是「all」的语义。
    # 以前这里从不核对，于是「all」在根级分叉的树上静默丢一半节点还退出 0。
    if spec is None or spec == "all":
        _cov, all_nodes, missing, _extra = coverage(
            mapping, main_ids, branches)
        if missing:
            raise ValueError(
                "树本身不完整（覆盖率缺 %s），无法「全部保留」。"
                "请先修树或改用 main / 指定分支。" % missing[:20])

    # 重连：保留集里的节点按原父连；父不在保留集里就**挂到合成的 root 上**。
    #
    # ⚠️ 这里曾经写成「parent = None」，结果裁出来的树没有 root 哨兵，
    # `coverage()` 从 root.children 出发找不到任何节点 → 每个节点都被判
    # 「不可达」，**prune 的产物永远无法再 render**（另一个新模式直接死路）。
    # 合成 root 既能保住可达性，也让「被裁掉的父」在报告里是可解释的断点。
    new_mapping = {
        "root": {"id": "root", "parent": None, "children": [],
                 "message": None},
    }
    orphans = []
    for nid in sorted(kept, key=num_key):
        node = mapping.get(nid) or {}
        parent = node.get("parent")
        parent = str(parent) if parent else None
        if parent not in kept:
            # 断链：原来的父被裁掉了（或本来就是顶层节点）
            if parent not in (None, "root") and not keep_main:
                orphans.append(nid)
            parent = "root"
        children = [str(c) for c in (node.get("children") or []) if str(c) in kept]
        new_mapping[nid] = {
            "id": nid,
            "parent": parent,
            "children": children,
            "message": node.get("message"),
        }
        if parent == "root":
            new_mapping["root"]["children"].append(nid)

    report = {
        "main": list(main_ids) if keep_main else [],
        "branches": [
            {"index": i, "start": branches[i][1][0] if branches[i][1] else None,
             "fork": branches[i][0], "node_ids": list(branches[i][1])}
            for i in keep_branch_idx
        ],
        "dropped_branch_indexes": [
            i for i in range(len(branches)) if i not in keep_branch_idx
        ],
        "kept_node_count": len(kept),
        "total_node_count": len([n for n in mapping if str(n) != "root"]),
        # 原父被裁掉、因此改挂到 root 的节点：这是**有意的断点**，不是丢数据
        "reattached_to_root": sorted(orphans, key=num_key),
    }
    return new_mapping, report


# ============================================================
# 分支感知渲染
# ============================================================

# 渲染时跳过的 fragment 类型（思考链 / AI 生成提示）；JSON 里一条不少。
DEFAULT_SKIP_TYPES = ("THINK", "TIP")

_ROLE_LABEL = {
    "user": "👤 用户",
    "assistant": "🤖 助手",
    "tool": "🛠 工具",
}


def _render_fragment(frag, skip_types):
    """把一个 fragment 渲染成 Markdown 行；跳过/空返回 None。"""
    ftype = frag.get("type")
    if ftype in skip_types:
        return None
    if ftype == "FILE":
        names = [f.get("file_name") for f in (frag.get("files") or [])
                 if isinstance(f, dict) and f.get("file_name")]
        return "- 📎 附件：" + "；".join("`%s`" % n for n in names) if names else None
    if ftype in ("TOOL_SEARCH", "TOOL_OPEN", "SEARCH", "TOOL_FIND"):
        urls = []
        for r in (frag.get("results") or []):
            if isinstance(r, dict) and r.get("url"):
                urls.append(r["url"])
        res = frag.get("result")
        if isinstance(res, dict) and res.get("url"):
            urls.append(res["url"])
        queries = [x.get("query") if isinstance(x, dict) else str(x)
                   for x in (frag.get("queries") or [])]
        queries = [q for q in queries if q]
        head = "🔍 联网搜索" if ftype in ("TOOL_SEARCH", "SEARCH") else "🌐 打开网页"
        if queries:
            head += "：" + "；".join(queries)
        if urls:
            head += " → " + "；".join(urls[:4]) + ("…" if len(urls) > 4 else "")
        return "> " + head
    body = (frag.get("content") or "").strip()
    return body or None


def render_markdown(mapping, current_id=None, skip_types=DEFAULT_SKIP_TYPES,
                    labels=None, title=None, note=None, strict=True):
    """
    把聊天树渲染成人类可读的 Markdown（分支分节 + 全覆盖断言）。

    结构约定（刻意如此，避免与正文冲突）：
      - 分节标题用 `#`
      - 每条消息用 `**▸ n<node_id> · <role> · <时间>**` 起头
      - 正文里自带的 `#` / `##` **是原文的一部分**，不属于本文档结构

    labels —— {支线起点节点: 人工标签}；键是**支线起点**不是分叉点
              （同一分叉点可能挂多条支线，用分叉点做键会串台）。
    strict —— True 时覆盖率不完整就抛错（默认）。**别关**，除非你在调试。

    返回 Markdown 字符串。
    """
    mapping = mapping or {}
    labels = labels or {}
    main_ids = main_path_ids(mapping, current_id)
    branches = branch_index(mapping, main_ids, current_id)

    covered, all_nodes, missing, extra = coverage(mapping, main_ids, branches)
    if strict and (missing or extra):
        raise AssertionError(
            "树渲染覆盖率不完整：缺 %s，多 %s（主链 %d + 支线 %d 条）"
            % (missing[:20], extra[:20], len(main_ids), len(branches)))

    def node_msgs(nid):
        node = mapping.get(nid) or {}
        msg = node.get("message") or {}
        return msg

    def emit_section(nodes, heading, note_line, out):
        out.append("\n\n---\n\n# %s\n" % heading)
        if note_line:
            out.append("\n> %s\n" % note_line)
        last = None
        for nid in nodes:
            msg = node_msgs(nid)
            if not msg:
                continue
            stamp = str(msg.get("inserted_at") or "")[:16].replace("T", " ")
            for frag in msg.get("fragments") or []:
                ftype = frag.get("type")
                role = _ROLE_LABEL.get(
                    {"REQUEST": "user", "RESPONSE": "assistant",
                     "THINK": "assistant"}.get(ftype, "tool"), "🛠 工具")
                if nid != last:
                    out.append("\n**▸ n%s · %s · %s**\n" % (nid, role, stamp))
                    last = nid
                rendered = _render_fragment(frag, skip_types)
                if rendered:
                    out.append(rendered + "\n")

    out = []
    if title:
        out.append("# %s\n" % title)
    if note:
        out.append("\n> %s\n" % note)
    out.append("\n> **结构约定**：分节标题用 `#`，每条消息用 `**▸ n…**` 起头；"
               "正文里自带的 `#` 是原文的一部分。\n")

    emit_section(main_ids, "主链 · 网页当前显示的那条线",
                 "从当前节点沿 parent 回溯得到。其余各节都是在这条主链上分叉出去的支线。",
                 out)

    for fork, nodes in branches:
        start = nodes[0]
        msg = node_msgs(start)
        stamp = str(msg.get("inserted_at") or "")[:16].replace("T", " ")
        heading = "支线 · n%s 起（%s）" % (start, stamp)
        if start in labels:
            heading += " · " + labels[start]
        note_line = "这条支线挂在主链的 n%s 上" % fork
        note_line += ("，共 %d 个消息节点。" % len(nodes)) if len(nodes) > 1 else "。"
        emit_section(nodes, heading, note_line, out)

    return "".join(out)


def render_report(mapping, current_id=None):
    """渲染前的体检报告：主链长度、支线条数、覆盖率是否完整。"""
    main_ids = main_path_ids(mapping, current_id)
    branches = branch_index(mapping, main_ids, current_id)
    covered, all_nodes, missing, extra = coverage(mapping, main_ids, branches)
    return {
        "main_node_count": len(main_ids),
        "branch_count": len(branches),
        "branch_node_count": sum(len(n) for _f, n in branches),
        "total_node_count": len(all_nodes),
        "complete": not missing and not extra,
        "missing": missing,
        "extra": extra,
        "branches": [
            {"index": i, "fork": fork, "start": nodes[0], "size": len(nodes)}
            for i, (fork, nodes) in enumerate(branches)
        ],
    }
