# 交接包与 Windows 注意事项

## 交接包的构成

一个可交接的**包**（不只是产物 JSON）通常包含：

```
<package>/
├── 00 这份包怎么用.md
├── 01 交接简报.md          ← 结论 / 时间线 / 缺口 / 常见误读
├── 02 对话全录.md          ← render 的产出（分支分节 + 全覆盖断言）
├── 03 附件索引.md
├── chatgpt-import/主线单线.json
└── 原始数据/
    ├── <product>.json      ← rescued-session/v1 产物
    └── 采集与校验说明.md
```

渲染与校验各自有脚本，**别手抄**：

```powershell
# 1) 全录（分支分节 + 全覆盖断言）
python "<skill-dir>/scripts/session_inspect.py" render <product>.json --out "02 对话全录.md"

# 2) 主线单线 —— ⚠️ 用 convert，不是 prune
#    prune 的产出是**另一个 rescued-session/v1 产物**（带 raw_mapping/transcript），
#    不是单线。verify-package --mainline 要的是 {"messages":[{role,content}]}，
#    那是 convert 的形状。混用会让 --mainline 检查在条文数时恒失败。
python "<skill-dir>/scripts/session_inspect.py" prune <product>.json --keep main --out "mainline-canvas.json"
python "<skill-dir>/scripts/session_inspect.py" convert "mainline-canvas.json" --out "chatgpt-import/主线单线.json"

# 3) 包级校验
python "<skill-dir>/scripts/session_inspect.py" verify-package `
    --package <dir> --product <dir>/原始数据/<product>.json `
    --transcript "<dir>/02 对话全录.md" `
    --mainline "<dir>/chatgpt-import/主线单线.json" `
    --index "<dir>/03 附件索引.md" --brief "<dir>/01 交接简报.md"
```

> **两步为什么分不开**：`prune --keep main` 保留的是树（还带着分支上下文），
> `convert` 才是把树压成「一问一答」单线的那一步。
> 直接把 `prune` 的产物喂给 `--mainline`，`verify-package` 认不出 `messages`，
> 会走到退化分支——那时**条文数断言**（`主线单线条数一致`）必然失败，
> 而 `角色交替` 那条会显示 PASS（0 条消息天然交替），
> 属于**红的有、绿的假**同时出现，最容易误导。

`verify-package` 的退出码：

| 码 | 含义 |
|---|---|
| 0 | 全部通过 |
| 1 | 有断言失败 |
| 2 | **校验本身没跑成**（参数错/文件读不出）——不是「通过」 |

⚠️ 它检查八组事：路径存在 / 全录覆盖全部节点 / 正文首尾锚点无截断 /
主线角色严格交替且逐字一致 / 附件对账 / 引用的节点号真实存在 /
无占位符残留 / 产物自证的 `missing_count` 与 `duplicate_count` 为 0。

**「请求了但没找到」判 FAIL，不判跳过**——否则路径写错会让关键检查
静默不跑，最后报个 PASS 0 FAIL。

## 交付前跑脱敏扫描

包里通常含真实个人内容（会话正文、真实 session id、真实 file_id）。
**对外复用前必须扫**：

```powershell
python "<skill-dir>/scripts/desensitize_scan.py" --history `
    --corpus "<product>.json" --deny "<你自己的标识符>"
```

`desensitize_scan.py` 的三条铁律同样适用于别处的门禁：

1. **不许假绿**：扫不到东西要报错退出，不能报「干净」。
2. **提交信息也要扫**：泄漏不只在文件里。
3. **黑名单里不放个人字面量**：个人标识符用 `--deny` 作为**输入**传进来。

## Windows 注意

### 没有 `/tmp`

Git Bash 里 `/tmp` 可能不映射到任何地方，会 `FileNotFoundError`。
用项目内或 `%TEMP%` 下的目录，**显式建**：

```python
tmpdir = os.path.join(os.environ.get("TEMP", "."), "session-inspect-scratch")
os.makedirs(tmpdir, exist_ok=True)
```

### 控制台编码

PowerShell 5.1 读子进程 stdout 时按控制台码页（中文默认 cp936）解码。
子进程写 UTF-8 字节 → 中文全乱码**而且不报错**。
需要字节级保真请用 `--out`；stdout 走 `deepseek_common.setup_streams()`。

### PowerShell 重定向会改编码

- `>` 可能写出 **UTF-16LE**（首字节 `FF FE`）
- 原生命令之间的管道会注入 **UTF-8 BOM**（`EF BB BF`）

所以读文件要 `utf-8-sig` → `utf-16` → `utf-8` 依次尝试
（`to_openai._decode_auto` 就是这么做的）。

### 往 heredoc 里写占位符名要当心

形如 `{{PLACEHOLDER}}` 的记号在某些工具链里会被当成裸变量名，
导致 `NameError`。写生成脚本时用拼接构造，例如：

```python
KEY = "qu" + "estion"      # 而不是直接写出那个记号
```

### CDP 大载荷

一次 `/eval` 取回几十万字符会被截断，**而截断后的 JSON 可能仍能解析**。
用 `browser_proxy.evaluate_chunked`（默认 40,000 字符一块）。

分块本身还有两个**静默损坏**的坑，`evaluate_chunked` 已一并处理：

1. **码元 vs 码点**：JS `slice` 按 UTF-16 **码元**计数，Python `len` 按**码点**。
   `offset += len(part)` 会让 offset 偏小 → 下一块重叠 → 内容**重复且不报错**。
   让 JS 报告实际推进的码元数。
2. **代理对不能被切断**：切点落在代理对中间时，Python 拿到落单 surrogate，
   而 Python 的字符串拼接**不会**把高低代理重新组合成原字符。
   结果是「长度差不多、内容已损坏」，之后按 UTF-8 写出还会炸。
   切点前若为高代理（`0xD800..0xDBFF`）就**回退一格**。

星形平面字符（emoji、部分罕见汉字）才会触发——纯中文样本测不出来。
