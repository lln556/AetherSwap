# 策略预演验收记录

Baseline: 25b9bfb081fcc7f7753bf66683e0756ea6fc52be
Status: implemented-and-verified

## 验证环境

测试使用现有应用镜像 `aetherswap:local-steam-renewal-fix-20261020`，将 Git 管理的源码及本次新增的 app/tests/web 文件复制到临时目录，再以 `/work` 挂载。临时目录不复制运行配置、账户凭据或实际数据库。pytest 依赖来自单独的测试目录。容器不联网，不启动应用 worker。

SQLite 用例使用临时数据库。PostgreSQL 用例使用独立 `postgres:18-alpine` 容器，测试容器共享其无外网网络空间；连接参数仅指向该临时测试库。`tests/test_decision_records.py` 支持通过 `TEST_DECISION_DATABASE_URL` 切换这两种后端。

浏览器用例加载真实 HTML、JavaScript 和 CSS，以合成 API 响应驱动，拦截外部请求。视口包括桌面 1440px 和手机 390px。

## 验收边界

- 记录与参数接口：`tests/test_decision_records.py`
- 买入真实流程与固定输入规则：`tests/test_decision_buy.py`、`tests/test_decision_buy_capture.py`
- 普通上架真实流程与规则：`tests/test_decision_sell.py`
- 自动调价真实检查与解释：`tests/test_decision_repricing.py`
- 只读行情刷新与后台任务：`tests/test_decision_refresh.py`
- 真实页面交互与图表：`tests/test_decision_preview_ui.py`

## 已处理的审查问题

- 记录脱敏覆盖 API 密钥、认证字段以及藏在普通文本中的链接；实际采集端只构造规则所需业务字段。
- 原始记录绑定检查开始时的账号。刷新校验记录账号与当前会话，并在写入版本前重新核实。
- 保存规则实现指纹；部署后的规则实现变化时，旧记录仍可阅读，但预演、刷新与另存副本明确拒绝不兼容版本。
- 刷新保留 Steam 实际日期字符串，出售按完整带时间行情重新计算原有趋势算法。
- BUFF 卖单按每条一件处理，不要求平台返回不存在的数量字段。
- 所有步骤保留明确状态，原始解释首先显示首个阻断原因。
- 刷新版本随原记录生命周期清理，不另设 spec 之外的版本数量上限。

## 运行命令与最终结果

最终版本运行结果：

| 检查 | 结果 |
| --- | --- |
| SQLite 隔离环境完整 `tests` | **1,094 passed**，104.01 秒 |
| PostgreSQL `tests/test_decision_records.py` | **21 passed**，10.17 秒 |
| 新增决策功能用例（包含在完整回归中） | **142 passed** |
| 真实页面浏览器用例（包含在完整回归中） | **23 passed** |
| 修改/新增的 26 个 Python 文件 AST 语法检查 | 通过 |
| `node --check web/js/decision-preview.js` | 通过 |
| `node --check web/js/strategies.js` | 通过 |
| `git diff --check` | 通过 |

仓库没有配置 mypy/pyright，未将 Python 语法检查表述为静态类型检查。测试有一个现有 FastAPI/Starlette TestClient 对 httpx 的弃用警告，无失败或跳过。

隔离目录中的等效命令：

```bash
PYTHONPATH=.:tests:/testdeps python -m pytest -p no:cacheprovider -q tests
TEST_DECISION_DATABASE_URL='<临时 PostgreSQL SQLAlchemy URL>' PYTHONPATH=.:tests:/testdeps python -m pytest -p no:cacheprovider -q tests/test_decision_records.py
node --check web/js/decision-preview.js
node --check web/js/strategies.js
```

首次全量回归为 1,090 passed / 2 failed，均为声明式出售上下文接线中的未定义常量；修复后复跑完整测试得到上述最终结果。不得把首轮结果视为最终验收。

两轴审查的主要修复还包括：刷新请求固定凭据并在各数据源之间复核账号；新数据版本比较失败时清除旧结果；数量阈值不按百分比显示；买入声明式模块与正式筛选共用卖压计算；原检查失败的日内行情不借用后来取得的数据；普通上架展示买家价格、预计净到账和币种。

## 发布边界

此次只实现、测试并提交代码，不自动推送、部署或启动真实交易任务。刷新任务在进程重启后不自动续跑；页面提示任务不可用，用户可以重新发起。记录和已保存的数据版本持久化保留。
