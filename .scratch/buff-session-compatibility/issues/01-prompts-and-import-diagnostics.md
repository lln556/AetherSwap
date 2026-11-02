# 修复旧重登弹窗干扰 BUFF 入口及扩展会话读取诊断

Status: ready-for-human
Progress: implemented-and-verified
Created: 2026-11-02
Baseline: 8b19bab

## 用户反馈

旧的登录弹窗反复要求填 Cookie，阻挡新 BUFF 卡片；扩展反复提示“未检测到 BUFF 登录”。页面同时报告 Steam 与 BUFF 登录失效。

## 已确认原因与证据边界

- `_refreshStatusOnce()` 每轮状态刷新都调用旧重登弹窗。已有新 BUFF 入口未被考虑；Steam 过期弹窗也未去重。
- 用户的导入错误来自扩展，发生在向服务器提交之前，表示没有读到 `session`，并不是服务端拒绝了导入。
- 真实加载扩展的合成 Cookie 测试复现：查询 BUFF 首页 URL 会漏掉 `/account/` 路径下可用于用户信息验证的 Cookie。修复为按真实验证接口 URL 查询，并显式选择当前标签页 Cookie store。
- 用户本地 Chrome Cookie 不可由服务器查看，不能仅凭现有错误确定其原因必然是路径、不同个人资料、缺权限或未在网页登录。新版提供不含敏感值的诊断以进一步定位。

## 实现范围

- 新 BUFF 卡片存在时不再自动弹出旧 BUFF modal；Steam 过期按事件/账号去重，BUFF 导入或填写期间不打断。保留用户主动打开登录窗口。
- 扩展区分 BUFF 权限缺失、无法确定 Cookie store、没有 session、没有 csrf_token；仅报告数量/缺少字段，不记录或回传 Cookie 值。
- 对同名 Cookie 保留最具体路径的匹配值，避免后端解析时被根路径旧值覆盖。
- 扩展版本升级 0.1.1，PONG 返回版本；页面识别旧版并提示下载、覆盖、重新加载扩展以及刷新页面。
- 不自动登录、不跨 profile、不自动启动买入、不绕过服务端验证。

## 验证

- 旧弹窗回归先3 failed/1 passed，再修复；另补导入期间Steam提示不打断的失败用例。
- 路径范围与错误诊断2个失败用例、同名Cookie覆盖1个失败用例、旧版扩展识别1个失败用例已复现后修复。
- 测试使用真实Chromium解压扩展、合成HttpOnly Cookie和隔离API，不读取用户本机真实Cookie。

最终验证：相关扩展/页面/真实API联动 49 passed；完整回归 1,193 passed，163.17秒；脚本版本标识更新后的静态资源回归 3 passed。JavaScript语法与git差异空白检查通过。唯一警告为既有Starlette TestClient/httpx弃用提示。

部署后仍需用户下载扩展0.1.1、覆盖原目录并在Chrome扩展管理页重新加载，最后刷新页面；若仍失败，需根据新诊断核对本地浏览器登录。不能声称已确认用户本机导入成功。

审查补充：同名Cookie优先级显式按路径长度排序，补充乱序返回的失败用例后修复；最终扩展、真实API联动与静态资源复测22 passed，29.57秒。
