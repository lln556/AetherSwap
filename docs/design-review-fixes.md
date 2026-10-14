# AetherSwap 审查修复与前端整理设计文档

日期：2026-10-14
依据：2026-10-14 四路只读代码审查（后端管线 / 外部集成 / 服务与路由 / 前端）的结论，全部高严重度项已经过代码位置核实。

## 1. 背景与目标

审查共确认约 40 处问题，集中在五类：

- 安全暴露：全部 API 无鉴权且绑定 0.0.0.0；代理凭据与支付链接进入应用日志；前端两处 XSS。
- 资金与数据竞态：邮件确认线程跨订单污染；交易表重建无维护锁；同名绑定 assetid 把个人物品当已购。
- 数据层缺陷：SQLite 无 WAL/busy_timeout；accounts.json / exchange_rate.json 非原子写。
- 性能问题：市场价缓存只写不读；steam_deals checkpoint O(n²) 写盘；分页二次切片 bug；重复下载与重复扫描。
- 可维护性：一批死代码与重复实现；前端工具函数在多个文件各自复制。

目标：在不动整体架构（FastAPI + 原生 JS、无构建步骤）的前提下修复全部审查发现，前端做轻量整理，不引入框架。

## 2. 分支与提交策略

- `deploy/local` 上未提交的已部署改动（item_meta、市场价缓存、仪表盘 UI 等）先提交存档。
- `main` 由 upstream 基线快进合并到 `deploy/local`，之后作为开发分支继续提交（本次修复落在这里）。
- `deploy/local` 保留原有历史，不再作为开发分支；部署仍从工作区构建，流程不变。
- 修复按主题分组提交：安全 / 资金与数据竞态 / 数据层 / 性能 / 前端整理 / 死代码清理。

## 3. 修复设计

### 3.1 安全

**S1 全 API 无鉴权（最高优先级）**
方案：HTTP Basic 鉴权中间件，仅在设置 `AETHERSWAP_AUTH_PASS` 时启用（向后兼容，测试环境不受影响）：

- 保护全部 `/api/*`，豁免 `/api/runtime`（容器健康检查）。
- 用户名取 `AETHERSWAP_AUTH_USER`，默认 `aetherswap`。
- 浏览器同源 fetch 在 Basic 认证后自动携带凭据，前端零改动。
- `docker-compose.yml` 透传两个环境变量；`.env.example` 增加说明；本机 `.env` 写入随机生成的密码。
- 新增测试：未带凭据 401、带凭据 200、`/api/runtime` 豁免、未设置变量时不启用。

**S2 代理凭据与支付链接进日志**
方案：`utils/proxy_manager.py` 增加 `mask_proxy_url()`（去掉 userinfo 只留 host:port），全部日志调用点改用掩码；`buff/buyer.py` 支付链接日志只记订单号。

**S3 前端 XSS**
方案：`web/js/gift.js`（好友昵称 / steamid / 头像 / 版本名）与 `web/js/proxy.js`（ip_detected / error / host / port / username）全部经 `escapeHtml` 输出。

### 3.2 资金与数据竞态

**M1 邮件确认线程竞态**
方案：`app/state.py` 引入确认代次：

- `begin_payment_wait()` 递增代次并重置 `_user_confirmed`，返回代次号。
- `confirm_payment(ok, generation=None)`：带代次时仅代次匹配才生效（邮件线程绑定其创建时的代次）；不带代次（用户在 UI 点击确认）始终生效。
- `_do_payment_notify_and_wait` 主线程等待加超时兜底（`email_timeout_seconds + 15s`）。
- 新增回归测试：上一单的迟到线程不得影响下一单等待。

**M2 交易表重建无锁**
方案：`sync_sold_from_history`、`repair_error_records`、下架后自动同步三个入口包 `exclusive_pipeline_maintenance(...)`；`db_replace_transactions` 保留 `_db_id`（重建时写回 id），避免重排导致待收货对账更新错行。

**M3 同名绑定 assetid**
方案：`_fill_assetid_from_inventory` 跳过 `pending_receipt=True` 的记录；仅当同名且未被占用的候选唯一时绑定。

**M4 位置索引写操作**
方案：`_sale_to_dict` 补 `_db_id`；State 类补 `delete_sale_by_id`；前端删除/编辑优先传 `db_id`（购买与出售行都带 `data-db-id`）。

**M5 售出价解析**
方案：`utils/money.py` 新增 `parse_price_text()`，正确处理 `1,234.56` 与 `1.234,56`；`_currency_code_from_price_text` 补 R$→BRL、S$→SGD、A$→AUD、C$→CAD；`steam_listings.py` 与 `repair_error_records.py` 两处共用。

### 3.3 数据层

**D1 SQLite WAL**
方案：engine connect 事件统一执行 `PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=30000`；`connect_args` 加 `timeout=30`。

**D2 JSON 原子写**
方案：新增 `utils/json_store.py`（`atomic_write_json` + `load_json`）；`accounts.py`（并加模块级 RLock）、`workers._save_exchange_rates`、`item_meta`、`market_price_cache` 改用共享实现。

### 3.4 性能

- **P1 市场价缓存只写不读**：`batch_fetch_prices(names, max_age=...)` 先读缓存，仅补拉过期/缺失的名称；交易页 enrich 与持有报告传入合适 TTL。
- **P2 steam_deals checkpoint 节流**：每完成 25 个或间隔 30 秒保存一次，暂停/结束时强制保存。
- **P3 steam_deals 分页**：`price_diff` / `discount_abs` 排序时取全量内存排序后一次切片；`db_get_steam_deals_count` 支持 `deal_status_filter`。
- **P4 market_orders**：SSR 页面响应中直接提取 `item_nameid` 并缓存，失败回退链免二次下载。
- **P5 sell_pipeline**：同名在售统计改 `Counter` 预计算；`_find_buy_record` 结果在循环内复用。
- **P6 steamdt fetcher**：传输层错误（errorCode 为 None）同样参与重试；非 JSON 响应纳入捕获。
- **P7 receive_flow 批量轮询**：一次基线快照 + 接受全部 offer + 单轮询循环 + 批量匹配，保持"仅新增资产、唯一归属、缺额保持 pending"不变式。
- **P8 缓存模块**：进程内 mtime 缓存、条目淘汰（市场价 30 天 / item_meta 180 天并设上限）、`batch_fetch_prices` 按名称 in-flight 去重；item_meta 回填改单事务批量更新。

### 3.5 前端整理

- **F1 库存列序回归**：库存行加 `data-name` / `data-price` 属性，`refreshMarketPrices` 与 `getInventoryCache` 按属性读取，不再依赖列序。
- **F2 工具收敛**：gift.js / steamDeals.js 的 `$` / `esc` / debounce / 裸 fetch 替换为 utils 的 `el` / `escapeHtml` / `fetchJson` / `debounce`（debounce 补进 utils）；console.js 双份 `fmtTime` 合一；日期格式化统一走 utils。
- **F3 小修复**：重登弹窗竞态（提交中不重置按钮、仅在状态变化时弹出）；`refreshStatus` 加 in-flight 守卫与 `document.hidden` 跳过；切购买页去除双重拉取 transactions；`toast` 支持 TTL 参数（CSS 进度条同步）；settings 的 ui_scale 监听器改 `onchange`；steamDeals 初始化重复请求合并。
- **F4 CSS**：补 `.muted` / `.small` / `.text-muted` 定义；inline style 中未定义的 `--border` / `--card-bg` fallback 改用已定义 token；`.toast` 重复定义合并（gift 区块限定作用域）。
- **F5 strategies.js 拆分**：按职责拆为多个文件（原生 script 顺序加载），同步更新 index.html 与 test_static_routes 断言。

### 3.6 死代码清理

- 删除：`utils/throttle.py`、`steam/client.py::_extract_line1`、`app/purchase_limit.py`、`state.reload_transactions`（含调用点）、`workers.py` 两常量与 `_detect_account_currency_from_history`、`steam_listings.py` 重复常量、`buyer.get_and_buy` 死熔断（连同其 legacy 测试）。
- 合并：`_load_exchange_rates`（用 `utils.money` 版）、`_enrich_purchases_with_current_prices`（收敛到 shared_market）、buff/buyer 两套下单实现（统一到 `lock_and_get_pay_url` 路径）、`parse_retry_after` 与 cookie 解析抽到 utils。
- `deal_cards` 汇率改为生成时读取，不再导入时固化。

## 4. 验证

- 容器内全量 pytest（基线：3 个 tzdata 环境失败，属容器缺包）。
- 新增测试：鉴权中间件、邮件确认代次隔离、售出价解析、资产绑定收紧、steam_deals 分页、缓存读取。
- 前端 Playwright 用例随改动同步更新。
- 不自动部署：镜像重建与流水线启动由用户决定。

## 5. 明确不做

- 不引入前端框架或构建步骤（评估结论：当前规模与定位不需要）。
- 不删除旧镜像与 config 备份目录（回滚依赖）。
- 暂不引入 CI（个人仓库，后续可选）。
