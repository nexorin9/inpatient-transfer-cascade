# CLI 全参考 · 住院转科连带处置系统

> 桌面窗口 + 系统托盘是日常入口；`python -m itc` 只作运维自检与排障通道。
> README 里只留速查表，本文件给出全部子命令、参数、退出码与可重复验证流程。

## 1. 启动方式

```bash
python -m itc                 # 启动桌面程序（窗口 + 托盘常驻）
python -m itc doctor          # 配置与出站自检
python -m itc run ...         # 跑一张转科单
python -m itc ledger list ... # 查处置台账
python -m itc handover ...    # 导出交接核对单
python -m itc replay ...      # 按运行 id 重放
```

不带子命令时进入桌面装配：创建 `QApplication`、打开本地 SQLite 状态库、先恢复未完成的外部阻塞 / 待确认项 / 待重写队列，再显示主窗口与托盘图标。关窗后托盘常驻（`setQuitOnLastWindowClosed(False)`），退出需走托盘菜单。

## 2. 子命令与参数

| 子命令 | 参数 | 用途 | 退出码 |
|---|---|---|---|
| `doctor` | 无 | 打印 `doctor_report()`：6 个配置项的齐备性、状态库路径、HIS / 模型是否可用；`HIS_BASE_URL` 与 `HIS_API_TOKEN` 齐备时额外探测端点可达性 | `0` |
| `run` | `--transfer <file>`（必填） | 读一张转科单 JSON，跑完整链路 | `0` 成功 / `2` 配置缺失 / `1` 运行失败 |
| | `--confirm` | 逐条确认后才发 HIS 写请求；不带则写请求数为 0 | |
| | `--sample-data` | 显式使用 `fixtures/` 脱敏样例跑最小链路，不出网、不发写请求；**不是** mock 开关 | |
| | `--out <dir>` | 产物目录，默认 `outputs/` | |
| | `--db <path>` | 覆盖 `ITC_STATE_DB` 状态库路径 | |
| `ledger list` | `--transfer` / `--status` / `--dept` / `--from` / `--to` / `--db` | 按转科单号、状态、科室、时间范围筛选台账（只读，最多显示 200 行） | `0` |
| `handover` | `--transfer <file>`（必填）/ `--out <path>` / `--db` | 从台账导出脱敏交接核对单（`.csv` 或 `.html`） | `0` |
| `replay` | `--run <run_id>`（必填）/ `--db` | 按图运行 id 只读重放，输出运行摘要（节点数、耗时、缓存命中、阻断、写回、待人工） | `0` |

## 3. 配置面（全部环境变量，无默认密钥）

| 变量 | 说明 |
|---|---|
| `HIS_BASE_URL` | 院内 HIS / ESB 基地址，出站读写的目标 |
| `HIS_API_TOKEN` | 出站令牌，统一 `Authorization: Bearer <token>` |
| `LLM_BASE_URL` | OpenAI-compatible 模型服务基地址；留空则模型判定降级 |
| `LLM_API_KEY` | 模型服务令牌 |
| `LLM_MODEL` | 判定长嘱归属的模型名 |
| `LLM_TIMEOUT_SECONDS` | 单次模型调用超时（默认 30） |
| `ITC_STATE_DB` | 本地 SQLite 状态库路径（默认 `~/.itc/state.sqlite3`） |
| `ITC_RULES_DIR` | 院内规则目录（科室映射、不可停品类目录等） |

真实凭据只从环境变量或 `.env` 读；`.env` 已在 `.gitignore` 中排除。产品默认路径指向真实 HIS 端点；`httpx.MockTransport` 只在测试层注入。

## 4. 出站接口

读 4 类：`GET /api/v1/transfers/{transfer_id}`、`GET /api/v1/transfers?ward=&date=`、`GET /api/v1/inpatients/{inpatient_no}/orders?status=executing`、`GET /api/v1/inpatients/{inpatient_no}/specimens?status=pending`、`GET /api/v1/inpatients/{inpatient_no}/charges?category=bed`。

写 2 类：`POST /api/v1/orders/{order_id}/execution-site`（带 `Idempotency-Key: sha256(transfer_no|order_no|execute_dept_code|effective_from)`）与 `POST /api/v1/orders/drafts`（`draft_type=STOP`、`status=DRAFT`）。代码层白名单只有这两条路径；不存在提交 / 签名医嘱的调用路径。

## 5. 产物

`run` 在 `--out` 目录（默认 `outputs/`）产出：

| 文件 | 内容 |
|---|---|
| `scope_list.json` | 牵动范围清单：`transfer_no` / `inpatient_no` / `items[]` / `node_order` |
| `disposition_list.json` | 逐条处置清单（脱敏）：判定、状态、证据字段、置信度、幂等键 |
| `stop_drafts.json` | 停嘱草稿，标注「草稿·需医师复核」，状态恒为 `DRAFT` |
| `handover.csv` | 转科交接核对单（脱敏可打印表格），条目数与台账一致 |

## 6. 可重复验证流程

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m itc doctor
python -m pytest tests -q
python -m itc run --transfer fixtures/transfer_sample.json --sample-data
python tools/fetch_datasets.py --check
python tools/bench_run.py --samples 200
python tools/capture_screenshots.py
```

前三条在无凭据的干净环境也能跑通：`doctor` 明确报告「HIS 出站未配置」；`--sample-data` 用提交的脱敏样例产出三份可核对产物，写请求数为 0。接真实 HIS 时去掉 `--sample-data`，并先配置好凭据。

## 7. 错误与降级对照

| 现象 | 处理 |
|---|---|
| `run` 未加 `--sample-data` 且缺 HIS 凭据 | 退出码 2，打印配置错误与 `--sample-data` 提示，不发任何请求 |
| HIS 不可达 / 取数失败 | 取数节点失败、图不推进、零写请求 |
| 模型未配置 / 超时 / 返回不可解析 | 语义项标「待人工判定」，停嘱草稿写请求数为 0，状态不误推进 |
| 写回 5xx | 指数退避 ≤2 次；超次置 `pending_writeback` 并在托盘提示 |
| 写回 4xx | 不重试，直接失败并给可读原因 |
| 重复提交同一转科单 | 幂等键命中登记表，跳过第二次 POST |
| 进程重启 | 外部阻塞、未完成处置项与待重写队列从 SQLite 恢复到同一节点 |
