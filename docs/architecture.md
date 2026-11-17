# 架构说明 · 住院转科连带处置系统

> 本文件承接 README 的「项目结构」小节：给出**逐文件目录树**、处置项 **7 态状态机**、**审计链**结构与**联动图**节点类型。
> 面向信息科与二次开发者；日常使用看 README 即可。

---

## 1. 逐文件目录树

```
inpatient-transfer-cascade/
├── README.md / README_EN.md / LICENSE / qrcode.jpg
├── requirements.txt / .env.example / .gitignore
├── docs/
│   ├── concepts/visual-target.md      # 视觉目标卡（无图像能力，不造假图）
│   ├── screenshots/                    # 真实运行截图（收尾阶段采集）
│   ├── cli.md                          # 全部子命令参考
│   └── architecture.md                 # 本文件
├── tools/
│   ├── fetch_datasets.py               # 公开数据获取与规模校验（可重跑、断网降级）
│   └── bench_run.py                    # 批量跑样例与性能统计
├── fixtures/                           # 提交的脱敏小样例
│   ├── transfer_sample.json            # 转科单样例
│   ├── orders_sample.json              # 在执行长嘱样例
│   ├── specimens_sample.json           # 待送标本样例
│   └── charges_sample.json             # 床位费明细样例
├── src/itc/
│   ├── __main__.py                     # CLI 入口：doctor / run / ledger / handover
│   ├── app.py                          # 桌面装配：QApplication + 系统托盘常驻
│   ├── config.py                       # 配置面：pydantic-settings，全环境变量注入
│   ├── domain/models.py                # 临床实体：转科单 / 医嘱 / 标本 / 费用 / 处置项
│   ├── graph/                          # ★ M1 联动图执行内核
│   │   ├── model.py                    # Node / Link / Graph；常量输入与连线输入区分；序列化往返
│   │   ├── ports.py                    # 输入端口分类（连线 / 常量）与必填校验
│   │   ├── builder.py                  # 建图、连线断言、构建完成校验
│   │   ├── registry.py                 # 节点类型注册表（与处置类型目录共用）
│   │   ├── scheduler.py                # 依赖就绪顺序、挡数计数、环检测
│   │   ├── dynamic.py                  # 运行时动态插节点、父链回溯、插后依赖重排
│   │   ├── blocker.py                  # 阻断语义：阻断下游并带原因
│   │   ├── external_block.py           # 外部阻塞持久化（中途等人）
│   │   ├── cache.py                    # 落盘缓存、命中判定、失效传播、脏标记
│   │   ├── idempotency.py              # 业务字段哈希幂等键
│   │   ├── engine.py                   # 执行主循环与工作线程、入队校验、终态语义
│   │   ├── events.py                   # 节点级进度事件
│   │   ├── template.py                 # 图模板加载与版本迁移
│   │   ├── audit.py                    # 只增不改审计链与哈希链
│   │   ├── replay.py                   # 运行回放与运行摘要
│   │   └── fetch_nodes.py              # 取数节点工厂与 his/reader 接线
│   ├── his/                            # ★ M2 HIS 出站对接层
│   │   ├── client.py                   # httpx 客户端：超时、连接池、统一鉴权头
│   │   ├── reader.py                   # 读 4 类资源
│   │   ├── writer.py                   # 写 2 类接口：执行地点变更 / 停嘱草稿
│   │   ├── errors.py                   # 状态码分类、退避重试、待重写队列
│   │   └── redact.py                   # 住院号摘要化与日志脱敏
│   ├── rules/                          # ★ M3 规则与守门
│   │   ├── dept_map.py                 # 科室映射表加载
│   │   ├── conflicts.py                # 执行科室硬矛盾判定
│   │   ├── specimen_route.py           # 标本去向唯一性判定与歧义拒绝
│   │   ├── no_stop.py                  # 不可停品类守门
│   │   ├── bed_fee.py                  # 床位费归属一致性提示
│   │   ├── registry.py                 # 规则目录加载、版本与命中计数
│   │   └── catalogs/                   # 院内维护的目录 JSON（映射表、不可停品类）
│   ├── model_gateway/                  # ★ M4 模型网关（单一 OpenAI-compatible）
│   │   ├── gateway.py                  # 客户端与结构化调用
│   │   ├── schema.py                   # 判定结果 JSON Schema 与校验
│   │   ├── prompt.py                   # 判定提示模板
│   │   ├── errors.py                   # 超时 / 连接失败 / 协议错误 / 校验失败语义
│   │   └── audit.py                    # 调用审计元数据与输入摘要哈希
│   ├── store/                          # ★ M5 状态与台账
│   │   ├── db.py                       # SQLite 连接、schema、版本迁移
│   │   ├── state_machine.py            # 处置项 7 态迁移校验
│   │   ├── recovery.py                 # 进程重启恢复
│   │   ├── ledger.py                   # 台账查询与导出
│   │   └── writeback_registry.py       # 写回幂等登记表
│   ├── catalog/                        # ★ M7 处置动作类型目录
│   │   ├── registry.py                 # 类型注册表
│   │   └── custom.py                   # 自定义类型加载与未知类型容错
│   └── ui/                             # ★ M6 桌面界面
│       ├── tokens.py                   # 色板 / 间距栅格 / 字体层级
│       ├── widgets.py                  # 按钮 / 输入框 / 表格基础组件与四态
│       ├── main_window.py              # 主窗口与导入入口
│       ├── graph_view.py               # 联动图视图
│       ├── disposition_panel.py        # 逐条处置清单与确认回写
│       ├── stop_draft.py               # 停嘱草稿呈现与无提交护栏
│       ├── tray.py                     # 托盘常驻与待办提示
│       └── handover.py                 # 交接核对单导出
└── tests/                              # 对照测试 / 契约测试 / 边界测试
```

---

## 2. 处置项 7 态状态机

```
pending_fetch ──► scoped ──► rule_judged ──► model_judged ──► awaiting_confirm
      │             │            │               │                  │
      │             │            │               │                  ├──► written_back
      │             │            │               │                  ├──► blocked
      │             │            │               │                  └──► failed
      └─────────────┴────────────┴───────────────┴──────────────────► failed
```

| 状态 | 含义 | 谁推进 | 可否回退 |
|---|---|---|---|
| `pending_fetch` | 已收单，待取数 | 执行器自动 | 否 |
| `scoped` | 取数完成，已算出牵动范围 | 执行器自动 | 否 |
| `rule_judged` | 规则已判 | 执行器自动 | 否 |
| `model_judged` | 模型已判（或已转人工） | 执行器自动 | 否 |
| `awaiting_confirm` | 等护士长逐条确认 | **人工** | 否 |
| `written_back` | 已写回 HIS | 写回后 | 否 |
| `blocked` | 被阻断（歧义 / 不可停品类 / 需医师判断） | 规则或人工 | 否 |
| `failed` | 失败（取数失败、写回超次、校验失败） | 执行器 | 否 |

**非法迁移抛错且状态不变**，例如：未确认直接到 `written_back`、`written_back` 再改写、`failed` 直接回到 `written_back`。
重跑**另开记录**，不覆盖原记录。

---

## 3. 审计链

- 每条审计事件含：`事件类型`、`节点 id`、`图运行 id`、`处置项 id`、`操作人`、`耗时`、`结果摘要`、`前一条哈希`、`本条哈希`。
- 写入层**只提供追加接口**，不提供 UPDATE / DELETE 审计表的入口。
- 校验时按顺序比对哈希，可定位**断点位置**与**断链条数**。
- 本机库与日志只留**住院号摘要**；姓名、身份证、联系方式既不落盘也不出网。

---

## 4. 联动图节点类型

| 节点类型 | 作用 | 是否可写回 |
|---|---|---|
| `transfer_input` | 接收转科单 | 否 |
| `fetch_orders` | 取回在执行长嘱 | 否 |
| `fetch_specimens` | 取回待送标本 | 否 |
| `fetch_charges` | 取回床位费明细 | 否 |
| `rule_conflict` | 执行科室硬矛盾判定 | 否 |
| `specimen_route` | 标本去向唯一性判定 | 否 |
| `no_stop_guard` | 不可停品类守门 | 否 |
| `bed_fee_hint` | 床位费归属一致性提示 | 否 |
| `model_judge` | 模型语义判定长嘱归属 | 否 |
| `change_execution_site` | 执行地点变更 | **是**（`execution-site`） |
| `stop_order_draft` | 停嘱草稿 | **是**（`orders/drafts`，只到 `DRAFT`） |

- **依赖就绪顺序**：只有全部上游输入就绪的节点才入队执行。
- **运行时动态插节点**：按这位患者当下的医嘱与标本状态决定长哪些节点（没有在执行长嘱就不长 `change_execution_site`）。
- **阻断**：`ExecutionBlocker` 带原因，阻断下游但不拖垮整图。
- **中途等人**：外部阻塞持久化到 SQLite，关进程重开仍停在同一节点。
