# BUFF 网页已登录但扩展读取 0 个：获取客户端证据

Status: ready-for-human
Progress: implemented-and-verified
Created: 2026-11-02

## 用户反馈

用户确认网页版已经登录。升级0.1.2后错误仍为：找到BUFF标签页，检查普通与第一方分区后读取0个适用于验证接口的Cookie，尚未向服务器发送。

## 当前结论

前两轮修复覆盖了合成浏览器测试中的路径/分区缺口，但未确认用户实际根因；不能继续据此认定用户未登录或已修复实际导入。

当前查询仍受验证URL路径、Cookie store、host permission和精确partition key过滤。需用用户本机Chrome DevTools中可见的Cookie元数据对照，才能区分域/路径不匹配、未覆盖的分区和扩展读取限制。

## 所需证据

用户已登录BUFF网页中的Application > Storage > Cookies表格，仅Name、Domain、Path、HttpOnly、Partition Key。Value整列及下方值预览必须遮盖，不索取真实Cookie值、账户密码或令牌。

此阶段不改动导入规则、不替换服务器凭据、不要求退出已有登录，也不再发布推测性兼容版本。


## 根因复现（2026-11-02）

用户提供的表格显示BUFF域的session/remember_me为HttpOnly，Secure未勾选。未将任何用户Cookie值复制到代码、日志或测试。

以相同Domain、Path、Secure=False属性构造合成session/csrf Cookie：浏览器HTTPS页面可见这些Cookie，而扩展仅有HTTPS主机权限时getAll读取0，导入失败。仅加入精确 `http://buff.163.com/*` 主机权限后同一测试转绿，定位为权限声明遗漏。

0.1.3同时要求BUFF的HTTP/HTTPS权限，查询与BUFF网络访问仍使用HTTPS；没有扩大到163.com父域。导出还按Cookie Domain精确过滤BUFF域，避免用户授权其他来源时误导出父域SSO Cookie。缺少HTTP权限明确提示，避免继续误报未登录。

## 验收

合成非Secure Cookie导入先失败后通过；额外授予测试用父域权限时，父域Cookie泄露防护先失败后通过；未授予HTTP权限时必须在读取前拒绝。实际用户会话须由其重新登录后通过扩展导入验证，不使用聊天里暴露的凭据进行平台请求。

最终结果：完整回归1,201 passed（178.07秒）；审查补充旧schema缺少祖先元数据时拒绝分区导出的失败用例并修复，最终扩展与真实API联动28 passed（44.45秒）。语法及差异检查通过。修复使用完全合成Cookie，不保存用户提供的值。
