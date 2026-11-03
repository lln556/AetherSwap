# 收货后台 TLS EOF 的定位与只读恢复

Status: ready-for-human
Progress: implemented-and-verified
Created: 2026-11-03

## 现场证据

- 用户收到11:52:21的receive_worker告警。日志记录11:52:18与11:58:09两次ConnectError / UNEXPECTED_EOF；容器healthy，worker仍在运行。
- 原日志未携带失败请求地址，不能从历史日志证明具体是哪一跳主动断开TLS，也不能断言服务器、运营商或代理是唯一根因。
- 调用链中BUFF购买历史请求的ConnectError直接逃逸至worker。Steam库存请求和报价接受路径有各自异常处理；现有历史读取没有局部连接重试。
- 有界只读现场检查：BUFF历史第一页HTTP200，30条；按10个待收货订单查询匹配10条SUCCESS。Steam两个库存context均HTTP200，合计21个资产。BUFF客户端trust_env=False，没有使用Steam代理池或环境代理。
- 网络错误是间歇性的，现场未复现持续故障。未调用真实收货、接受报价、上架或交易写接口。

## 修复范围

- 仅购买历史GET在ConnectError/ConnectTimeout后重试一次，同账号同会话，重新经过原请求限速/熔断检查。
- 不重试429、登录拒绝、读超时或任何POST；持续连接失败携带安全的固定接口及页码，不泄露Cookie、订单号或查询参数。
- 对账层注明失败阶段，失败不应用部分收货匹配。
- 记录无法匹配的唯一特征缺失/歧义原因，worker对相同原因去重，不将其作为任务崩溃反复推送。

## 验证

初始受限GET重试测试3 failed / 3 passed；后续补充失败阶段脱敏、分页失败不返回部分结果、重试前再次限流、不可匹配箱子保持原状与日志去重。

最终验证：相关回归145 passed；补充后的历史读取/worker边界10 passed；全量1,213 passed（178.45秒）。语法和git差异检查通过。测试没有执行真实交易写请求。
