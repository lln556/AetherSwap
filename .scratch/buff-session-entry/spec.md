# BUFF 登录检测与 Chrome 会话导入

Status: ready-for-human
Progress: implemented-and-verified
Created: 2026-10-30
Baseline: df8f73c

## 范围与已确认决定

以 [design.md](design.md) 为产品与验收依据，用户于2026-10-30确认按完整范围生成任务并实现。提供首页常驻状态卡片、主动在线检测、Chrome扩展导入和手动填写；账号及目标来源匹配、运行/付款保护、失败保留旧凭据、绝不自动启动买入。

## 接口契约

- GET `/api/auth/buff/session`：缓存状态，不发平台请求。
- POST `/api/auth/buff/session/check`：一次只读用户信息检查；重复操作去重，遵循账号限流。
- POST `/api/auth/buff/session/import`：校验、账号比对、保护状态复核后替换凭据；候选失败不覆盖当前会话状态。
- GET `/api/auth/buff/extension.zip`：只打包扩展发布目录，不包含运行配置。
- 所有接口受现有鉴权保护；新写接口要求明确请求意图标头，拒绝跨来源请求。
- 状态区分 unconfigured / unchecked / valid / expired / verification / rate_limited / error / account_mismatch，附检测时间和可否替换说明。

## 浏览器边界

扩展仅在授权服务来源中提供桥接；每次导入使用扩展自己的确认界面。Cookie只在扩展和隔离脚本中短暂处理并传给目标服务，不进入页面消息、DOM、日志或扩展持久存储。撤销授权后不再接受该来源导入。无痕、跨profile、商店发布不在首版范围。

## 验收

先失败测试再实现；公开API、真实Chrome扩展安装与页面行为，以及BUFF请求/支付保护回归。凭据更换/账号切换使旧检测结果失效。风险恢复只允许单次只读用户信息请求，不临时解除全局熔断；429仍阻断。服务器验证成功并保存前后复核身份和交易保护。测试用合成会话，不真实下单。

## 验收记录

2026-10-30：实现、两轴审查与完整回归完成，1,184 passed；详见 [validation.md](validation.md)。待用户安装扩展并人工验收。
