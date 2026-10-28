# 统一仪表盘与交易中心的成交、持仓判定

Status: ready-for-human
Progress: implemented-and-verified

## 问题与根因

仪表盘 `/api/stats` 通过旧 `Purchase.sale_price > 0` 判定售出；交易中心通过
`TradeSettlement` 判定。非人民币成交不会回填旧 sale_price，因而同一物品在两个页面
分别显示持仓和已售。此外，仪表盘原来的购买查询排除所有归档记录，导致已售记录归档后
从历史统计消失，而交易中心仍保留该成交。

## 反馈循环

通过临时 SQLite 调用真实 `record_settlements`、`api_stats`、`trading_overview`。
相同购买、相同到账，只把 CNY 改为 USD 即可复现。首次 1 failed / 1 passed；
补充清空旧 sale_price、归档及零/未知到账金额后为 7 failed / 5 passed。
这证明分歧来自成交证据和查询范围，而非价格解析或金额换算本身。

## 验收范围

- 两处共享状态规则：有成交记录即已售，不依赖币种、旧 sale_price 或到账是否已知。
- 已归档成交仍计入历史已售；未售归档不再计入持仓，关闭订单不计入最近买入。
- 待收货及未核实上架不算成交，仍属于未售成本。
- 所有已售物品的原始 BUFF 成本按人民币合计；净到账按币种分别汇总，不做汇率换算。
- 收益和折扣比率仅使用已知的 CNY 净到账及其对应购买成本，保留整数分汇总。
- 历史估算明确标注；未知到账不是零金额，不参与收益与比率计算。
- 人工记账及撤销记账后，两处状态仍一致。
- 浏览器渲染确认币种、估算、未知金额和归档状态说明，无真实 API 请求。

## API 兼容

`total_purchased` 仍表示所有已售购买成本（CNY）。`total_sold` 保留旧的人民币
税前兼容汇总，可能无法表示新成交流水，仅供旧客户端兼容；新仪表盘不再展示或反推该值。
`proceeds_by_currency` 才是新界面的净到账汇总，含估算数量、未知金额数量；
`total_profit`、`discount_ratio` 从成交表的已知人民币到账计算。
只有外币或未知到账成交时，人民币收益为 null；无成交时为 0。

## Comments

- 2026-10-28：建立真实数据库回归并复现；实现共享状态判定、仪表盘成交表汇总与前端标记。
- 2026-10-28：首轮相关回归 63 passed，继续验证新增边界及浏览器渲染。

- 2026-10-28：包含浏览器测试的相关回归 67 passed；全量测试 952 passed in 34.24s。
- 2026-10-28：浏览器测试进一步加载真实 index.html；调整后的浏览器及静态资源测试 5 passed。
- 2026-10-28：node --check web/js/dashboard.js 和 git diff --check 通过。

## 验证环境与复现命令

使用既有应用镜像 `aetherswap:local-steam-renewal-fix-20261020` 和临时 pytest 依赖，
将 Git 管理的当前源码及新增 UI 测试复制到临时目录；容器 `--network none`，
不挂载实际运行配置、凭据、数据库。数据库用例使用 tmp_path SQLite，浏览器拦截外部请求。

隔离副本中的等效命令：

```bash
PYTHONPATH=.:tests python -m pytest -p no:cacheprovider -q tests/test_dashboard_summary.py -k dashboard_and_trading_agree
PYTHONPATH=.:tests python -m pytest -p no:cacheprovider -q tests
PYTHONPATH=.:tests python -m pytest -p no:cacheprovider -q tests/test_dashboard_stats_ui.py tests/test_static_routes.py
```

根因定位、修复和回归完成；未部署或操作真实账户。
