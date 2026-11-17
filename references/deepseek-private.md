# DeepSeek 登录态私密会话

DeepSeek 有**三种**容器。正文 schema（fragment 类型）三者一致，
但**外层结构完全不同**，混用会得到看起来像「会话不存在」的报错。

| 容器 | 入口 | 外层结构 | 处理模块 |
|---|---|---|---|
| 账号导出包 | `conversations.json` | `mapping` 聊天树 | `extract_deepseek.py` |
| 公开分享链接 | `/share/<id>` | 线性 `messages[]` | `extract_deepseek_share.py` |
| **登录态私密会话** | `/a/chat/s/<uuid>` | `chat_messages[]` + `parent_id` 树 | `deepseek_private.py` |

## 不要把它当分享链接

```
ERROR: 分享链接提取失败: ValueError: not a share id or share url
```

拿它的 uuid 去问分享 API 会得到 `share does not exist`。
这两条报错都指向「这不是分享链接」——但它**也不是**导出包。

判断：

```python
import deepseek_private
deepseek_private.is_private_session_url(url)   # True → 私密会话
deepseek_private.session_id_from_url(url)      # 抠出 uuid
```

`read` 省略 `--platform` 时也会自动认出来。

## 调用

```powershell
python "<skill-dir>/scripts/session_inspect.py" read `
    --platform deepseek-private "https://chat.deepseek.com/a/chat/s/<uuid>" `
    --out product.json --verify
```

需要**已登录的浏览器**与 web-access CDP 代理（默认 `http://localhost:3456`）。

## 为什么必须在页面内 fetch

外部 `curl` 直接请求 `history_messages` 会被 WAF 拦下，返回一个
`Error - Request Blocked` 的 HTML 页——**HTTP 200 的 HTML**，不是 4xx。
看上去像「接口坏了」，其实是被拦。必须在已登录页面内 fetch，
带着正常的浏览器指纹与同源 Cookie 才通。

## 三个已踩过的坑

### 1. token 不是裸字符串

`localStorage.userToken` 存的是 **JSON 对象**，真正的 token 在 `.value`（64 字符）。

取错了会得到：

```json
{"code": 40003, "msg": "INVALID_TOKEN"}
```

⚠️ 这**不是**「会话不存在」，而是 token 取错或登录态过期。
`browser_proxy.read_local_storage(..., value_field="value")` 已经处理。

**凭据边界**：token 只在内存里用于 Authorization 头，**不落盘、不进日志、不进产物**。

### 2. 响应几十万字符，一次性取回会被静默截断

实测单会话 632,411 字符。一次 `/eval` 拿不全，而截断后的 JSON
**可能仍然能解析**——于是下游以为数据完整。必须分块取回：

```python
bp.in_page_fetch_json(target, url, headers={...})   # 内部走 evaluate_chunked
```

### 3. 分叉不能 DFS 全树

账号导出包那种 DFS 会把**兄弟分支拼成自相矛盾的对话**。
私密会话沿 `current_message_id` 回溯出主链，其余节点按 `inserted_at`
补捞并标 `on_main_path: false` + `fork_of`——既不丢数据，
也不假装它们是同一条脉络。

## 附件字节拿不到

私密会话的 `signed_path` 对**匿名 GET 恒 400**（实测 `ty=p/t/d/f` 四种全 400）。
要字节必须走登录态下载控件。产物里如实记：

```json
{"bytes_available": false, "signed_path_present": true,
 "bytes_note": "…要字节必须走登录态下载控件。"}
```

附件清单仍然完整（数量、文件名、尺寸、所属提问都在），只是没有像素。

## 记录下来的部分

产物 `_meta.verification` 自带多重集自证，典型形态：

```json
{"ok": true, "source_fragment_count": 317, "output_fragment_count": 317,
 "missing_count": 0, "duplicate_count": 0, "seq_contiguous": true,
 "raw_mapping_identical": true,
 "main_path_message_count": 54, "off_path_message_count": 68}
```

`main_path_message_count` / `off_path_message_count` 是**分叉的量化**：
主链 54 条、支线 68 条。引用正文时要说清引的是哪条线上的。
