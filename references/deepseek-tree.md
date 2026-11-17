# DeepSeek 聊天树的操作原语

DeepSeek 的会话是**树**不是列表：编辑提问 / 重新生成答案都会 fork 出新分支。
三种容器（导出包 / 分享链接 / 私密会话）归一后节点形状一致：

```
{id, parent, children[], message}
```

`scripts/deepseek_tree.py` 提供四个原语。**不要手搓**——手搓版会静默出错。

## 一次真实事故

手写渲染器按 `children[0]` 走「主链」，在第一个分叉点（n6 → [7, 9]）
挑中了**被改写前的旧草稿 n7**，于是 n8 就到底了：

```
naive children[0] → [1, 3, 6, 7]        ← 静默丢掉 9/11/13
正确主链         → [1, 3, 6, 9, 11, 13]
```

**不报错，只是少了一大截**（实测丢 80/122 节点、正文短 18%）。
这类错误只有「全覆盖断言」能抓。

## 四个原语

### `main_path_ids(mapping, current_id)`

从 `current_message_id` 沿 `parent` 回溯，得**网页上实际显示的那条链**。
不给 `current_id` 会退回 DFS 第一条路径——那只是**猜测**，会静默丢兄弟分支。

### `branch_index(mapping, main_ids, current_id)`

主链上每个节点挂出去的、不在主链里的子树：`[(fork_node, [node_ids...]), ...]`。

⚠️ 键是**分叉点**（挂在主链哪个节点上），不是支线起点。
同一个分叉点可能挂多条支线（多次重生成）——实测一棵 122 节点的树里
n88 挂了 4 条、n68 挂了 3 条。用分叉点做标签键会串台。

### `select_branches(mapping, spec, current_id)`

裁剪 / 保留。`spec` 三种形态：

| spec | 含义 |
|---|---|
| `None` / `"all"` | 全部保留 |
| `"main"` | 只留主链 |
| `["main", 0, 2]` | 主链 + 第 0、2 条支线（序号） |
| `["main", "95"]` | 主链 + 起点为 n95 的支线（节点 id） |

返回 `(kept_mapping, report)`。**不改动源**。

⚠️ DeepSeek 的节点 id 是**数字字符串**，所以 `"7"` 既可能是「第 7 条支线」
也可能是「起点节点 n7」。规则：**节点 id 优先**——先当起点节点查，
查不到才当序号。要明确指定序号请传 `int`。

### `render_markdown(mapping, current_id, ...)`

分支感知的 Markdown + **全覆盖断言**。

结构约定（刻意如此）：分节标题用 `#`，每条消息用 `**▸ n<node_id> · <role> · <时间>**`
起头。这样正文里自带的 `#` / `##` 是**原文的一部分**，不会与文档结构混淆。

`strict=True`（默认）时覆盖率不完整就抛 `AssertionError`。**别关**。

## 命令行

```powershell
# 结构体检：主链 / 支线 / 覆盖率
python "<skill-dir>/scripts/session_inspect.py" render product.json --report

# 渲染全录
python "<skill-dir>/scripts/session_inspect.py" render product.json --out transcript.md

# 只看主链（产出仍是**一份完整产物**，可直接再 render）
python "<skill-dir>/scripts/session_inspect.py" prune product.json --keep main --out mainline-product.json

# 主链 + 一条支线
python "<skill-dir>/scripts/session_inspect.py" prune product.json --keep main,95 --out partial.json

# 先看会丢弃什么，不落盘
python "<skill-dir>/scripts/session_inspect.py" prune product.json --keep main --report
```

`--report` 的 `complete: false` 说明主链 + 支线没覆盖全部节点——
**先查清为什么缺**，再渲染。

⚠️ 三条容易踩的口径：

1. **`prune` 的产出是产物，不是单线。** 要 `chatgpt-import/v1`
   的一问一答数组，得再接一步 `convert`（见 `handoff-package.md`）。
2. **`--keep` 里的数字优先当节点 id。** `--keep 95` 指「支线起点 n95」，
   不是「第 95 条支线」（DeepSeek 的 id 本身就是数字串）。
   只有当它不是任何支线起点时，才退化成序号解释。
3. **裁出的树自带 `root` 哨兵。** 原父被裁掉的节点会改挂到 root 上，
   并在 `report.reattached_to_root` 里列出来——那是有意的断点，不是丢数据。
   因为带了 root，`prune` 的产物可以直接喂回 `render`。

`--keep all` 在**本身就不完整**的树上会直接报错而不是"全部保留"：
那种树有节点从 root 走不到，声称全保留只会把丢数据伪装成成功。
