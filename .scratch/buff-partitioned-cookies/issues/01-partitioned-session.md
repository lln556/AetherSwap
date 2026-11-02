# 补齐扩展的 BUFF 分区会话读取

Status: ready-for-human
Progress: implemented-and-verified
Created: 2026-11-02
Baseline: 8a62c17

## 问题与证据

用户明确表示已在网页登录，但0.1.1扩展返回0个Cookie。服务器无法查看用户本机Chrome，不能据此认定用户未登录。

实际Chromium扩展测试复现：在BUFF普通标签页为 `https://163.com` 第一方分区设置session/csrf Cookie，普通getAll返回0，而带精确partitionKey的查询返回2。原扩展未查询分区，导致导入失败。此测试证明检测覆盖存在缺口，不代表已经确认用户本机的唯一原因。

## 边界

- 确认用户导入后，查找与目标服务同一Cookie store的正常BUFF标签页。
- 保留普通Cookie读取，补充精确第一方分区：`topLevelSite=https://163.com, hasCrossSiteAncestor=false`。仅在确认顶层BUFF网页存在时访问此分区，不用空key或其他站点key。
- 保持仅BUFF域的固定权限，不增加163.com门户或其他子域权限。Chrome getPartitionKey本身需要额外站点权限，因此不调用它来扩权。
- 检测不同普通/分区session时拒绝混合，账号一致性仍由后端验证。
- 无BUFF标签页或旧Chrome不支持时明确提示；不把读取0个等同于未登录。
- 扩展版本0.1.2，网站提示旧扩展更新；用户本地需要重新加载扩展。

## 验证

先在真实Chromium创建分区Cookie，确认原查询为空并复现导入失败，再修复；补充其他站点分区、跨站祖先分区、会话冲突及没有BUFF标签页的拒绝用例。全部测试使用合成会话，不操作真实账户。

## 验证结果

相关扩展/页面/真实API联动48 passed；全量1,197 passed（171.69秒）。审查发现不得用Chrome132新增getPartitionKey函数的存在性误判Chrome120的分区读取能力，补充两项失败用例后修复；最终扩展与真实API联动25 passed（38.48秒）。语法与差异检查通过。

用户本机是否属于分区Cookie场景仍需更新扩展后实际导入确认，不宣称已经恢复用户登录。
