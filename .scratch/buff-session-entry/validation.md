# BUFF 登录入口与 Chrome 扩展验收

Status: implemented-and-verified
Baseline: df8f73c6962e5d6158e1a94467cd3130283a1524

## 环境与测试边界

测试使用应用依赖镜像和临时源码副本，在 `--network none` 的隔离容器内运行；不挂载运行配置、真实 Cookie 或生产数据库。BUFF 平台响应仅在 HTTP 边界替换。浏览器测试使用真实页面脚本、真实加载的解压扩展和合成 HttpOnly Cookie。

扩展安装态测试启动 persistent Chromium。自动测试副本预先授予本地测试主机权限，以避开无法自动操作的 Chrome 原生权限提示；扩展的实际 popup、来源精确匹配、撤销、独立确认窗口与内容脚本仍正常执行。用户安装时仍需在 Chrome 原生提示中确认授权。

## 验收用例

- `tests/test_buff_session_api.py`：缓存读取、凭据/账号绑定、失败分类、受控恢复、运行/付款保护、并发去重、跨来源拒绝、鉴权、下载包和持久化失败。
- `tests/test_buff_session_ui.py`：常驻入口、桌面与手机状态、操作反馈、手动填写/取消、扩展缺失与安装指引、结果过期、缓存与显式请求竞态。
- `tests/test_buff_extension.py`：真实扩展加载、精确来源/端口、授权撤销、HttpOnly读取、取消与延迟响应、导航/子frame保护、无凭据消息泄漏、重定向拒绝。
- `tests/test_buff_session_extension_integration.py`：真实扩展经 Basic Auth 本地HTTP服务提交到真实FastAPI导入接口，再执行一次合成BUFF用户信息请求并保存会话。
- 既有认证、请求限流、checkout保护、购买、出售与其他页面回归。

## 两轴审查结果及修复

Standards轴发现：浏览器重登重复验证；状态写盘失败后结果不明确；扩展并发撤销丢更新；旧请求响应冒充新请求成功。均补充失败用例后修复。输入规范化已提取共享模块，账号本地更新使用公开的账号保护上下文。

Spec轴发现：旧成功状态未老化；旧错误标志覆盖新网络错误；可变账号对象未固定；空UA验证与保存不一致；失败回滚使旧检测记录失效；取消/确认竞态继续提交。均补充失败用例后修复。

成功检测在30分钟后仅提示重新检测，不自动访问BUFF。显示当前检测异常不等于解除交易认证保护。恢复探测不临时清除风险限制，429冷却始终阻断。

## 验证结果

第一轮完整回归：1,181 passed。新增扩展竞态回归并修复后，最终完整回归：**1,184 passed，153.54 秒**，无失败或跳过。仅有一个既有 Starlette TestClient/httpx 弃用警告。

修改的 Python 文件通过 AST 语法检查；前端与扩展 JavaScript 的 `node --check` 全部通过；`git diff --check` 通过。仓库未配置 mypy/pyright，未将语法检查表述为静态类型检查。

阶段验证：后端与既有认证组合79 passed；扩展15 passed；UI/API/真实扩展到API联动55 passed。页面文案收尾后额外复跑UI及静态资源：27 passed，29.11秒。各批次有用例重叠，不相加作为总数。

## 交付边界

仅支持同一Chrome个人资料的普通窗口，手动加载解压扩展。应用提供下载；不发布Chrome网上应用店，不后台同步，不自动切换账号或启动买入。

用户仍需在自己的Chrome安装扩展、授权自己的AetherSwap来源，并自行登录BUFF。测试和部署不会替用户读取其本地Chrome或操作实际订单。
