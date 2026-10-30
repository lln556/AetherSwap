# Chrome BUFF 会话导入扩展

首版是 Chrome Manifest V3 解压扩展，仅支持同一 Chrome 个人资料的普通窗口，不支持无痕窗口或跨 profile 读取。它只在用户主动发起导入并确认目标来源后读取 BUFF Cookie，不做后台同步。

## 安装

1. 打开 `chrome://extensions`，启用“开发者模式”。
2. 选择“加载已解压的扩展程序”，指向仓库中的 `extensions/buff-session/`。
3. 如果从 AetherSwap 下载 `/api/auth/buff/extension.zip`，解压后选择解压目录，不要选择 zip 文件本身。
4. 在 AetherSwap 页面打开扩展弹窗，点击“授权当前站点”。修改协议、主机或端口后必须重新授权。

扩展权限边界：

- `cookies` 配合固定 `https://buff.163.com/*` 主机权限，只用于读取 BUFF Cookie。
- AetherSwap 来源通过 `optional_host_permissions` 在用户点击时申请；Chrome 匹配模式不区分端口，因此扩展另外保存并严格比较规范化后的 `protocol://host:port`。
- 不支持无痕窗口；服务端导入和确认操作也只接受普通窗口、主 frame、未变化的来源与页面文档。

## 登录检测

首页“检测登录”只在点击时向 BUFF 发起一次验证；页面刷新读取已保存结果。成功检测超过 30 分钟会提示重新检测，不自动请求 BUFF。网络异常、登录失效、安全验证和限流分别显示。

## 使用流程

1. 在 Chrome 中打开 BUFF 并完成登录。
2. 回到 AetherSwap 的当前标签页，点击“从浏览器导入”。
3. 若页面提示扩展未就绪，打开扩展弹窗授权当前站点；必要时刷新页面。
4. 扩展打开独立确认页，显示精确目标来源。此时尚未读取 Cookie。
5. 用户点击“确认导入”后，扩展核对目标 tab、来源、授权和文档标识，读取 BUFF Cookie，并交给该 tab 的第 0 frame isolated content script。
6. content script 同源提交 AetherSwap。页面只收到验证状态，不收到 Cookie。
7. 页面可发送 `CANCEL` 取消等待确认。确认尚未发出时，扩展会关闭旧确认窗口并删除 pending；若 POST 已经发出，则不能声称未保存，只返回结果未确认。

BUFF 未登录时，确认窗口会立即关闭，页面收到 `buff_login_required` 和登录指引。用户先在 `https://buff.163.com/` 完成登录，再从 AetherSwap 重新发起导入；不要求把 Cookie 复制到网页。

## 页面消息协议

页面向扩展：

```js
{
  source: "aetherswap-buff-page",
  type: "PING" | "IMPORT" | "CANCEL",
  requestId: "opaque-id"  // CANCEL 使用原 IMPORT 的 requestId
}
```

扩展向页面：

```js
// PING 响应
{
  source: "aetherswap-buff-extension",
  type: "PONG",
  requestId: "opaque-id",
  ok: true | false,
  error?: "安全错误文本"
}

// IMPORT 响应
{
  source: "aetherswap-buff-extension",
  type: "RESULT",
  requestId: "opaque-id",
  ok: true | false,
  code?: "safe-code",
  error?: "安全错误文本",
  session?: {
    status?: string,
    checked_at?: number | null,       // 仅有限数字或 null
    message?: string,
    retry_after?: number,             // 仅有限数字
    can_replace?: boolean,
    replace_block_reason?: string
  }
}
```

`session` 是固定字段白名单；Cookies、调试字段及其他未知字段不会进入 `postMessage`、页面 DOM 或扩展存储。数字时间戳会保留，前端可再主动 GET `/api/auth/buff/session` 刷新缓存状态。

`CANCEL` 的终态：

- 确认前取消：`ok: false`、`code: "cancelled"`，表示未读取或发送 BUFF 会话；旧确认窗口已关闭，之后点击旧窗口也不会发送 Cookie。
- POST 已发出后取消：`ok: false`、`code: "result_unconfirmed"`，不能断言是否已保存。必须提示用户刷新页面或 GET `/api/auth/buff/session` 核对，不可显示“已取消/未保存”。
- content script 的确认等待上限为 70 秒，与当前页面 UI 超时一致；页面超时也应发送 `CANCEL`。

## 导入 POST 契约

Cookie 由 isolated content script 使用当前页面 fetch 发送：

```http
POST /api/auth/buff/session/import
Content-Type: application/json
X-AetherSwap-Intent: buff-session
```

```js
{
  cookies: "session=...; csrf_token=...",
  user_agent: navigator.userAgent,
  source: "extension",
  recovery: true
}
```

fetch 使用 `credentials: "same-origin"` 和 `redirect: "error"`；浏览器保留 AetherSwap 页面已有的 HTTP Basic 认证。重定向、网络失败或无法解析的响应只返回安全错误，不跟随跳转。

后端成功返回 `{ok: true, session: {...}}`；失败返回 `{ok: false, error, code, session?}`。扩展只把上述白名单状态转给页面。导入成功只表示会话已由服务端验证并保存，不会自动启动买入。

## 安全边界

- Cookie 不进入网页 `postMessage`、DOM、`chrome.storage` 或日志。
- Pending 记录仅保存 request ID、文档 nonce、精确来源、tab/window 标识和时间，不保存 Cookie。
- 页面刷新、导航、关闭 tab、关闭确认窗口或超时会取消 pending。
- 消息 sender 必须是当前扩展的第 0 frame；`sender.tab.url` 与文档来源必须一致，且 tab 位于普通窗口。
- 撤销授权后立即拒绝 PING/IMPORT，即使 Chrome 的站点权限尚未消失。
- 扩展没有后台 Cookie 同步或跨 profile 访问逻辑；唯一的前台计时器用于 70 秒确认超时。

## 排查

- **页面提示扩展未就绪**：确认扩展已加载，并在 AetherSwap 当前来源的扩展弹窗中授权。若 content script 注入失败，刷新页面。
- **页面提示 BUFF 未登录**：在同一 Chrome profile 打开 `https://buff.163.com/` 完成登录，再从 AetherSwap 重新发起导入。
- **AetherSwap 返回 401**：先在发起导入的 AetherSwap tab 中恢复该站点的 HTTP Basic 认证。
- **撤销后 Chrome 仍保留站点访问**：应用层授权已立即撤销；如弹窗提示权限未移除，可在扩展详情中手动移除站点访问。
- **端口变化被拒绝**：这是预期行为。Chrome 主机权限不区分端口，扩展应用层会重新要求该 `protocol://host:port` 的显式授权。

## 自动化测试

```bash
python3 /tmp/aetherswap-preview-test.py -q tests/test_buff_extension.py
```

测试使用 Playwright Chromium persistent context 实际加载解压扩展，并用本地 HTTP 服务提供真实页面、Basic Auth 与导入端点。外部网络被 route 隔离；Cookie 为合成数据，包含 HttpOnly。覆盖授权精确到端口、popup 授权/撤销、未确认与取消不读取 Cookie、同源隔离、BUFF 未登录指引、导航/子 frame 拒绝、成功与失败结果白名单、Cookie 不进入页面/存储，以及重定向拒绝。
