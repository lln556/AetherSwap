# AetherSwap 审查修复与前端整理设计文档（含实施记录）

日期：2026-10-14
状态：已实施并验证（main 分支）
依据：2026-10-14 四路只读代码审查（后端管线 / 外部集成 / 服务与路由 / 前端）的结论，全部高严重度项均经过代码位置核实。

## 1. 背景与目标

审查共确认约 40 处问题，集中在五类：

- 安全暴露：全部 API 无鉴权且绑定 0.0.0.0；代理凭据与支付链接进入应用日志；前端两处 XSS。
- 资金与数据竞态：邮件确认线程跨订单污染；交易表重建无维护锁；同名绑定 assetid 把个人物品当已购。
- 数据层缺陷：SQLite 无 WAL/busy_timeout；accounts.json / exchange_rate.json 非原子写。
- 性能问题：市场价缓存只写不读；steam_deals checkpoint O(n²) 写盘；分页二次切片 bug；重复下载与重复扫描。
- 可维护性：一批死代码与重复实现；前端工具函数在多个文件各自复制。

目标：在不动整体架构（FastAPI + 原生 JS、无构建步骤）的前提下修复全部审查发现，前端做轻量整理，不引入框架。

## 2. 分支与提交策略（已执行）

- `deploy/local` 上未提交的已部署改动（item_meta、市场价缓存、仪表盘 UI 等）已提交为 `41a493c`。
- `main` 由 upstream 基线快进合并至 `41a493c`，之后作为开发分支；本次全部修复提交都在 `main`。
- `deploy/local` 保留原有历史，不再作为开发分支；部署仍从工作区构建，流程不变。

## 3. 修复设计（实施情况）

### 3.1 安全 —— 全部实施

- **S1 鉴权**：新增 `app/api_auth.py` + HTTP Basic 中间件，由 `AETHERSWAP_AUTH_PASS` 启用（用户名 `AETHERSWAP_AUTH_USER`，默认 `aetherswap`）；`/api/runtime` 豁免健康检查；`docker-compose.yml` 透传；`.env` 已写入随机生成的密码；新增 `tests/test_api_auth.py`。未重建镜像前不生效。
- **S2 凭据掩码**：`utils/proxy_manager.mask_proxy_url()`，proxy_manager / gift_engine / workers / steam_listings / market_orders 全部日志调用点改用；`buff/buyer.py` 支付链接不再写入日志。
- **S3 前端 XSS**：`gift.js`（好友昵称/steamid/头像/版本名）与 `proxy.js`（ip_detected/error/host/username）全部经 `escapeHtml` 输出。

### 3.2 资金与数据竞态 —— 全部实施

- **M1 邮件确认线程**：`app/state.py` 引入确认代次：`begin_payment_wait()` 返回代次，`confirm_payment(ok, generation=None)` 仅代次匹配生效（UI 点击不带代次始终生效）；邮件线程绑定创建时的代次；主线程等待带超时兜底（`email_timeout_seconds + 15s`）。新增 `tests/test_state_payment_generation.py`。
- **M2 交易表重建**：`sync_sold_from_history` / `repair_error_records` / 下架后自动同步三个入口包 `exclusive_pipeline_maintenance`；`db_replace_transactions` 保留 `_db_id`（`_purchase_from_dict`/`_sale_from_dict` 写回 id）；`db_append_purchase`/`db_append_sale` 剥离 `_db_id`（追加始终分配新 id）。
- **M3 同名绑定**：`_fill_assetid_from_inventory` 跳过 `pending_receipt=True` 的记录，且仅当同名未占用候选唯一时绑定。新增 `tests/test_sync_sold_binding.py`。
- **M4 位置索引写操作**：`_sale_to_dict` 补 `_db_id`；State 类补 `delete_sale_by_id` / `update_sale_by_id`（及 db 层）；路由与前端（单删/编辑/批量删除/售出）优先传 `db_id`。
- **M5 售出价解析**：`utils/money.parse_price_text()`（兼容 `1,234.56` 与 `1.234,56`）与 `currency_code_from_price_text()`（补 R$→BRL、S$→SGD、A$→AUD、C$→CAD）；`steam_listings.py` 与 `repair_error_records.py` 共用。新增 `tests/test_price_parsing.py`。

### 3.3 数据层 —— 全部实施

- **D1 SQLite**：engine connect 事件执行 `PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=30000`，`connect_args` 加 `timeout=30`。
- **D2 原子写**：新增 `utils/json_store.py`（`load_json` / `atomic_write_json`）；`accounts.py` 整体重写（模块级 RLock 覆盖读改写 + 原子写）；`workers._save_exchange_rates`、`item_meta`、`market_price_cache` 改用共享实现。

### 3.4 性能 —— 实施（P7 采用保守方案）

- **P1 市场价缓存复用**：`batch_fetch_prices(names, max_age=...)` 先读缓存只补拉过期/缺失；按名称 single-flight 共享在途请求；`fill_current_market_prices()` 收敛 transactions/workers 两处重复实现，TTL 取 `pipeline.current_price_refresh_minutes`（下限 60s）。
- **P2 checkpoint 节流**：每完成 25 个或间隔 30 秒保存一次，暂停/结束强制保存。
- **P3 分页**：`price_diff`/`discount_abs` 排序且带搜索/状态过滤时取全量内存排序后一次切片；`db_get_steam_deals_count` 支持 `deal_status_filter`。
- **P4 item_nameid**：SSR 列表页响应直接提取并缓存 `item_nameid`（`_remember_item_nameid`），失败回退链免二次下载。
- **P5 sell_pipeline**：同名在售数改 `Counter` 预计算；`_find_buy_record` 结果循环内复用。
- **P6 steamdt**：传输层错误（errorCode 为 None）参与重试；非 JSON 响应纳入捕获。
- **P7 receive_flow**：采用懒加载单基线——基线在本次 pass 第一次 accept 前拍一次快照；本地映射不完整的 offer 不会被接受也不会触发扫描（保留原有不变式），后续 offer 复用同一基线并通过 `already_used` 排除已认领资产。完整批量重写评估后认为收益有限、风险偏高，未采用。
- **P8 缓存模块**：`item_meta` / `market_price_cache` 增加进程内 mtime 缓存与条目淘汰（180 天/上限 2 万；行情 30 天）；item_meta 回填改单事务批量更新（`db_update_purchases_meta`）。

### 3.5 前端整理 —— F1-F4 实施，F5 推迟

- **F1 列序回归**：库存行增加 `data-name` / `inv-price`，`refreshMarketPrices` 与 `getInventoryCache` 按属性读取，不再依赖列序。
- **F2 工具收敛**：`gift.js` / `steamDeals.js` 的 `$` / `esc` / 本地 debounce 替换为 utils 的 `el` / `escapeHtml` / `debounce`（debounce 新增到 utils）；`console.js` 双份 `fmtTime` 合一；日期格式化统一 `utils.formatDateTime`。
- **F3 小修复**：`toast` 支持 TTL 参数（CSS 进度条同步 `--toast-ttl`）；重登弹窗仅在首次显示时重置按钮（轮询不再打断提交）；`refreshStatus` 加 in-flight 守卫与 `document.hidden` 跳过；切购买页不再二次拉取 transactions（`renderTransactionsFromCache`）；settings 的 ui_scale 监听器改 `onchange`。
- **F4 CSS**：补 `.muted` / `.small` / `.text-muted`；index.html 内联样式未定义的 `--border`/`--card-bg` 改用 `--border-color`/`--bg-surface`；gift 区块 `.toast` 规则限定为 `.toast[class*="toast-"]`。
- **F5 推迟**：strategies.js 拆分。该文件是单一功能域（策略编辑器），拆分收益有限且引入加载顺序耦合；文件继续明显增长时再评估。

### 3.6 死代码清理 —— 实施

- 删除：`utils/throttle.py`（且并发下失效）、`app/purchase_limit.py`、`steam/client.py::_extract_line1`、`state.reload_transactions`（含调用点与导入）、workers 两个死常量与 `_detect_account_currency_from_history`（连同其本地币种函数）、`market_orders` 重复的汇率加载（改用 `utils.money.load_exchange_rates`）、重复的 `_enrich_purchases_with_current_prices`（收敛到 `shared_market`）、`buff/buyer.py` 的 `get_and_buy` 与 `_execute_post_buy`（死代码，连同其测试用例移除）。
- `deal_cards` 汇率改为懒加载（mtime 缓存），不再导入时固化。
- 评估后不做：两个 `request_policy` 的 `parse_retry_after` 抽取（两者语义不同：默认值、下限、时钟各不相同，合并风险大于收益）；`_fetch_pay_url` 与 `_get_alipay_url` 合并（两者都在活跃路径上，避免改动支付代码）。

## 4. 验证

- 最终全量（容器内）：**694 通过，3 失败**——3 个失败均为容器缺 tzdata 的既有环境性失败（与历史基线一致）。
- 新增测试 40 项：鉴权 5、支付确认代次 5、资产绑定 5、价格解析 23、数据库 roundtrip 2。
- 移除测试 14 项：随 buyer 死代码删除（`get_and_buy` / `_execute_post_buy` 及其参数化用例）。
- JS 语法：容器内 `node --check` 全部通过。
- 未自动部署：镜像重建与流水线启动由用户决定。

## 5. 明确不做

- 不引入前端框架或构建步骤（评估结论：当前规模与定位不需要）。
- 不删除旧镜像与 config 备份目录（回滚依赖）。
- 暂不引入 CI（个人仓库，后续可选）。
- F5 strategies.js 拆分推迟；request_policy 抽取与支付 URL 合并评估后不做（见 3.6）。
