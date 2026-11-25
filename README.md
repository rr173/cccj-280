# 批次可执行分配与拣发核销（规则版本 / 业务时点驱动）

按**指定规则版本**和**业务时点**，为待发订单生成可执行批次分配、未满足原因与改版差异，
并完整保留历史发布方案；在此之上支持**拣发核销**：仓库登记实际出库，据此查看订单剩余
可执行量、更新实际库存，并在重新规划时把已确认出库作为事实恰好计入一次。
纯标准库实现，无第三方依赖。

## 1. 业务规则（实现口径）

### 账面余量（事件账本）
- 事件分两类：**数量事件**（入库 `RECEIPT` / 领用 `ISSUE` / 退回 `RETURN` / 报废 `SCRAP`）
  与**状态事件**（放行 `RELEASED` / 隔离 `QUARANTINED` / 召回 `RECALLED`）。
- 数量按**事件身份 `event_id` 去重**，同一身份只计一次；事件给出生效时点，账本只累计
  `effective_at <= 业务时点` 的事件。
- **退回仅未拆封（`sealed=true`）且验收合格（`accepted=true`）才入账**；其余逐笔拒绝，
  在账本与报告中保留拒绝原因，绝不计入余量。
- 状态事件只改可用性，**永不改账面量**；解除隔离不增加账面量。
- 任何事件使批次余额为负（含非正数量、领用/报废超过账面）即抛 `InvalidHistory`，
  **拒绝整个历史，不用负库存补方案**。

### 可分配性（按顺序核验，任一不过即阻断）
1. **用途**：订单用途必须包含在批次 `purposes` 内；
2. **质量证据**：放行状态必须带 `evidence_id`，否则 `QUALITY_EVIDENCE_MISSING`；
3. **召回** `RECALLED` → 不可分配；
4. **隔离** `QUARANTINED` → 不可分配；
5. **保质期**：按订单用途选择生效规则，
   `失效日 = 生产日期 + shelf_life_days 天`，在**规则指定时区**当天 `00:00`；
   `业务时点 >= 失效日` 即过期（**失效日当天即过期**），时区由输入明确；
6. **余量**：单批余量必须 `>=` 订单整数需求量（不可拆批、不可部分满足）。

### 分配与优化目标
- 每单必须**全部来自一个**可用于该用途的批次；同批可供多单，但累计分配不得超余量。
- 目标（严格字典序最大化，不是逐单先到先占）：
  1. 按优先级从高到低，逐层最大化**已满足订单数**；
  2. 再最大化**满足总量**；
  3. 同值并列任意（实现按 id 排序给出确定性结果）。
- 求解器为带乐观上界剪枝的**分支定界**；`brute_force_verify` 用独立的
  `itertools.product` 全枚举给出可独立核验的最优值（≤20 万选项空间时启用）。
- 未满足订单给出阻断依据：无任何可用批次时附**逐批次原因**；有可行批次但整体最优下
  让位时标记 `DISPLACED_BY_HIGHER_PRIORITY`。

### 改版与版本管理
- 规则/质量修订后用 `recalc`：**先撤回**同数据集/时点的旧 `ACTIVE` 方案，再发布新方案；
- 新方案只从事件账本重算余量，与旧方案无关，因此**量不重复**；
- 所有方案（含 `WITHDRAWN`）落盘保留，`history` 可查新旧两套及取代关系；
- 同一数据集同一时点最多一个 `ACTIVE`，重复发布会被登记册拒绝。

### 拣发核销（实际出库登记）
- 核销单含**唯一单号** `writeoff_id`、**方案编号** `plan_id`、**出库时点** `outbound_at`
  与按订单列明的**实际批次和数量**行。
- 只接受**当前有效（ACTIVE）方案**中的订单；行内批次必须与该订单在方案中的分配批次
  **完全一致**（`WO_BATCH_MISMATCH`）；数量为正整数，可分次出库，但同一订单累计出库
  **不得超过原分配量**（`WO_EXCEEDS_ALLOCATED`）。
- 一张核销单**整体接受或整体拒绝**：任一行不合法，整单不产生任何扣减。
- **幂等**：相同单号且内容（方案/出库时点/行集合的规范哈希）相同的重送，回放首次结果且
  **绝不重复扣减**；单号相同而内容不同，拒绝 `WO_IDEMPOTENT_CONFLICT`。拒绝单也留痕，
  其单号不得再用于不同内容（更正后请换新单号）。
- 已接受核销按出库时点生成为合成 `ISSUE` 事件（事件身份
  `WRITEOFF:<单号>:<批次>`，天然按单号去重）并入事件账本；若接受本单会使任一时点账面
  为负（**非法库存历史**），整单拒绝 `WO_NEGATIVE_HISTORY`，不用负库存补账。
- 方案**撤回后不得**再凭其未执行部分办理新核销（`WO_PLAN_NOT_ACTIVE`）；但撤回不抹去
  已确认出库——它们作为**实际数量事实**永久保留，在后续库存计算与重新规划中**恰好计入
  一次**。
- **并发口径**：核销提交、方案发布/撤回/重算都在同一把排他锁内完成（进程内 `RLock`
  可重入 + 跨进程 `fcntl.flock`，JSON 原子替换落盘）。每个操作基于锁内读取的一致方案
  状态与库存结果完成，因此并发的最终结果等价于某一种完整的先后串行顺序：不能超额出库，
  方案撤回后也不可能再接受新核销。
- 查询能力：核销记录（含拒绝原因）、订单**已出库量/剩余可执行量**（`execution`）、
  批次**账面/已出库/实际库存**（`stock`，可按业务时点做时点视图）。

## 2. 目录结构

```
src/batch_alloc/
  model.py      领域模型、JSON 解析与结构校验（强制带时区时间、正整数数量等）
  engine.py     事件账本（去重/退回/负库存）、质量状态、规则选择、失效日、可用性
  planner.py    分支定界精确求解 + 独立穷举核验 + 阻断诊断
  registry.py   方案发布/撤回/重算、JSON 原子持久化、独立核验 verify_plan
  writeoff.py   拣发核销：幂等存储、排他锁（RLock+flock）、WarehouseService 串行化、
                合成 ISSUE 出库事实、订单执行量与库存视图、失败原因码
  recall.py     只读召回清单：来源追溯（区间式 FIFO，不按单件展开）、加工关联、
                退回关联、受影响在库/未收回订单/在途/已收回、错误码
  report.py     终端可读报告（方案 + 核销记录/执行量/库存 + 召回清单）
src/cli.py      命令行入口（plan/recalc/history/ledger/writeoff/writeoffs/execution/stock/recall）
sample_inputs/  7 个演示数据集 + 核销单样例 + recall/ 召回演示输入（不可信输入，独立目录）
tests/          23 项单元测试 + 24 项核销测试 + 100 组随机性质 + 40 组核销随机性质
                + 27 项召回测试（tests/test_recall.py）
demo/run_demo.py 端到端演示（含错误 FCFS 对照、核销全生命周期、并发可串行化）
demo/recall_demo.py 只读召回演示（FIFO/加工/退回/只读性/十亿级数量）
output/         运行产物（方案 JSON、登记册、核销存储、日志）
```

## 3. 运行方式（隔离模式，防输入目录投毒）

```bash
# 端到端演示（全部断言通过才退出 0）
python3 -I demo/run_demo.py

# 测试
python3 -I tests/test_batch_alloc.py
python3 -I tests/test_writeoff.py
python3 -I tests/test_random_property.py
python3 -I tests/test_recall.py

# CLI：分配
python3 -I src/cli.py plan   --input sample_inputs/demo_wide_narrow.json \
                             --as-of 2026-06-01T00:00:00Z \
                             --registry output/registry.json --out output/p.json
python3 -I src/cli.py recalc --input sample_inputs/demo_shelf.json \
                             --as-of 2026-03-20T00:00:00Z --rule-version v2 \
                             --plan-id P2 --registry output/registry.json
python3 -I src/cli.py history --registry output/registry.json
python3 -I src/cli.py ledger  --input sample_inputs/demo_returns.json \
                             --as-of 2026-06-01T00:00:00Z

# CLI：拣发核销（--store 默认取登记册同目录 writeoffs.json）
python3 -I src/cli.py writeoff  --input sample_inputs/demo_shelf.json \
                                --doc sample_inputs/writeoff_shelf_001.json \
                                --registry output/registry.json
python3 -I src/cli.py writeoffs --registry output/registry.json [--status ACCEPTED|REJECTED]
python3 -I src/cli.py execution --input sample_inputs/demo_shelf.json \
                                --registry output/registry.json
python3 -I src/cli.py stock     --input sample_inputs/demo_shelf.json \
                                --registry output/registry.json \
                                [--as-of 2026-03-21T09:00:00Z]

# CLI：只读召回清单（不写登记册/核销存储，--out 只写清单文件）
python3 -I demo/recall_demo.py
python3 -I src/cli.py recall    --input sample_inputs/recall/recall_dataset.json \
                                --as-of 2026-03-01T00:00:00Z \
                                --recall sample_inputs/recall/recall_input.json \
                                --registry /tmp/recdemo/registry.json \
                                [--out recall.json]
```

无效历史退出码为 `3`；核验失败退出码为 `2`；核销单被拒绝退出码为 `4`；
登记册错误退出码为 `5`。核销接受（含幂等重送命中）退出码为 `0`。
召回清单只读：发现关联无效/缺退回关联/身份冲突/超退/负余额等一律退出码 `3`，
不输出不完整清单。

### 核销单 JSON 结构

```json
{
  "writeoff_id": "WO-SH-001",
  "plan_id": "RC-V1",
  "outbound_at": "2026-03-21T09:00:00Z",
  "lines": [
    {"order_id": "O-F", "batch_id": "B-SH", "qty": 4},
    {"order_id": "O-G", "batch_id": "B-SH2", "qty": 6}
  ]
}
```

- 时间同样强制带时区；`qty` 为正整数；同一单据内每个订单只能出现一行。
- 提交时 `--input` 数据集的名称与输入哈希必须与方案发布口径一致，否则
  `WO_DATASET_MISMATCH`。
- 核销存储为独立 JSON（默认 `writeoffs.json`，配套 `.lock` 锁文件），与方案登记册
  分开落盘、协同加锁。

## 4. 输入 JSON 结构

```json
{
  "name": "数据集名",
  "batches": [{"batch_id": "B1", "production_date": "2026-01-01",
               "purposes": ["A", "B"], "initial_status": "QUARANTINED"}],
  "quantity_events": [{"event_id": "EV1", "batch_id": "B1", "type": "RECEIPT",
                       "qty": 100, "effective_at": "2026-01-03T08:00:00Z",
                       "sealed": true, "accepted": true}],
  "status_events": [{"event_id": "ES1", "batch_id": "B1", "status": "RELEASED",
                     "effective_at": "2026-01-04T00:00:00Z", "evidence_id": "Q-1"}],
  "shelf_rules": [{"rule_id": "R1", "purpose": null, "version": "v1",
                   "effective_at": "2025-01-01T00:00:00Z",
                   "shelf_life_days": 720, "timezone": "UTC"}],
  "orders": [{"order_id": "O1", "purpose": "A", "qty": 10, "priority": 10}]
}
```

- 时间均为带时区 ISO-8601（`Z` 或偏移）；无时区时间直接拒绝。
- `shelf_rules.purpose = null` 是兜底规则；规则按 `version` + `effective_at` 版本化，
  可用 `--rule-version` 锁定。
- 退回事件必须显式给出 `sealed` 与 `accepted`。

## 5. 关键示例（演示输出对应数据集）

### 5.1 宽用途订单占错批次（`demo_wide_narrow.json`）
- `B-WIDE` 可用于 A/B，`B-A` 仅可用于 A；两单 O-A(A)、O-B(B) 各需 100。
- 错误的逐单先到先占：O-A 先占 `B-WIDE` → O-B 无批可用，只满足 1 单；
- 本求解器：O-A→`B-A`、O-B→`B-WIDE`，满足 2 单/200 件，穷举核验目标向量 `(2, 200)`。

### 5.2 召回 / 隔离 / 证据 / 过期（`demo_quality.json`）
召回、隔离中、放行无证据、过期四类批次各自阻断专属订单；隔离后重新放行的 `B-Q2`
恢复可用但账面量仍为入库的 100（状态事件不改量）。

### 5.3 退回（`demo_returns.json`）
`B-R1 = 100 入库 - 30 领用 + 10 合格退回 = 80`；已拆封、验收不合格两笔退回被拒。
需求 80 的订单全额满足；需求 51 的订单因最大单批仅 50 且禁止拆批而阻断。

### 5.4 保质期边界与时区（`demo_shelf.json`）
- F 用途规则时区 `Asia/Shanghai`，30 天：失效日 `2026-03-31T00:00+08:00`
  = UTC `2026-03-30T16:00Z`；该时刻 F 单立即阻断，前一秒仍可分配。
- G 用途规则时区 `UTC`：失效日 `2026-03-31T00:00Z`，同一日历日对应不同绝对时刻。
- 改版 v2（F 保质期 30→15 天）后同一时点 F 单由可分配变为过期阻断。

### 5.5 无效历史（`demo_invalid_qty.json` / `demo_negative_stock.json`）
负数量入库、领用超账面均被 `InvalidHistory` 拒绝，不产生任何方案。

### 5.6 召回后重算（登记册 `output/registry.json`）
`RC-V1(ACTIVE) → RC-V2(WITHDRAWN，规则改版) → RC-V3(ACTIVE，召回后)`，
全程仅一个 ACTIVE，旧记录可查，占用合计始终不超账本余量。

### 5.7 拣发核销（`writeoff_shelf_*.json`，演示场景 8/9）
- `RC-V1` 为 O-F(B-SH,10)、O-G(B-SH2,10) 分配后：`WO-SH-001` 一次出 O-F=4、O-G=6
  整体接受；同内容重送幂等命中不重复扣减；同号异内容拒绝。
- 错批次单（O-G 报 B-SH）与超分配单（O-F 累计 11>10）均**整单拒绝且零扣减**；
  更正后换新单号出 6，O-F 累计恰好 10、剩余可执行量为 0。
- 撤回 `RC-V1` 后新核销被 `WO_PLAN_NOT_ACTIVE` 拒绝，但已确认的 O-F=10、O-G=6
  作为事实保留；`recalc` 新方案基于含合成 `ISSUE` 的账本求解，B-SH 实际库存 90、
  B-SH2 实际库存 94，出库事实恰好计入一次。
- 并发演示：12 线程核销叠加 3 线程重算，最终仅 10 张出库被接受（分配量上限），
  仅一个 ACTIVE 方案，库存恒非负——结果等价于某种完整串行顺序。

## 6. 只读召回清单（`recall`）

供应商通知某几笔原料入库需要召回时，用现有数据集 + 已有核销记录，回答“截至某个
业务时点，哪些**已出库订单**还没收回受影响货物、哪些**批次**还有受影响库存，各多少”。

本功能**只读**：不改事件账本、核销记录或已发布方案（CLI 只额外写 `--out` 指定的清单文件）。

### 6.1 口径

1. **数量沿账本生效顺序**：同一时点按事件 ID；仅计本数据集、截至时点实际发生的核销，
   不论其方案后来是否撤回（沿用 `WRITEOFF:<单号>:<批次>` 事件身份）。
2. **同批先进先出**：领用/报废/核销出库只消耗“当时已入库且尚未消耗”的最早货物，
   **不会把同批后来入库的货物算进较早的发出**；账本顺序下超领即非法历史，报错。
   同一核销单同批多条订单行按**订单 ID 顺序**取货。
3. **召回起点**：输入的原料入库事件必须截至时点已发生、且不是任何加工的产出。
4. **加工关联**：一组领用 + 一组产成品入库标识同一次加工（可多领用/多产出）。
   每个事件至多归属一次加工；按账本顺序所有领用须早于所有产出。关联不另记库存，
   只把“被领用货物携带的召回来源”传给该次加工**全部产出**。只要实际领用过一个单位
   召回来源，全部产出都受影响；未领用的库存与更早的产出不受这次加工影响。
   产出携带来源**集合**，同一来源经多路加工汇合只记一次。
5. **退回关联**：每笔按现有规则入账（未拆封+验收合格）的退回必须指向账本顺序中已发生
   的一条**已接受核销订单行**，且批次相同。分次退回按该行当初取货先后认定收回部分，
   累计不得超出该行数量；收回货物保留来源、在退回时点重新入仓。未入账退回不计为收回。
6. **错误即停**：缺少必需退回关联，或关联无效、事件身份冲突、非法领用/产出顺序、
   超退、负余额时，抛 `RecallError`（错误码 + 明细），退出码 `3`，不输出不完整清单。
7. **数量规模**：全程按区间与整数运算，不按单件展开，十亿件可秒级计算。

### 6.2 输入结构

召回输入与数据集、核销存储分离（`--recall` 指向）：

```json
{
  "recall_source_event_ids": ["R-1"],
  "processes": [
    {"link_id": "L1", "issue_events": ["I-1"], "output_events": ["P-1", "P-2"]}
  ],
  "returns": [
    {"return_event_id": "RT-1", "writeoff_id": "WO-1", "order_id": "O-1", "batch_id": "B-RAW"}
  ]
}
```

### 6.3 输出结构（JSON）

```json
{
  "as_of": "...", "dataset": "...", "recall_sources": ["R-1"],
  "affected_orders": [
    {"writeoff_id","order_id","batch_id","outbound_at",
     "qty","by_source",
     "unrecovered_qty","unrecovered_by_source"}
  ],
  "affected_stock": [
    {"batch_id","on_hand","affected_qty","by_source"}
  ],
  "in_transit": [ {"process_id","issue_events","qty","by_source"} ],
  "recovered":  [ {"return_event_id","writeoff_id","order_id","batch_id",
                   "effective_at","qty","by_source"} ],
  "totals": {"affected_stock","affected_unrecovered","affected_in_transit",
             "affected_outstanding","affected_shipped","affected_recovered"}
}
```

- `affected_orders`：已出库、尚未收回的受影响订单行；`qty` 为出库受影响总量，
  `unrecovered_qty` 为扣除已收回后的未收回量。
- `affected_stock`：仍有受影响库存的批次；`affected_qty` 只算一次，
  `by_source` 把该段货物记到它命中的每个来源名下（多来源汇合时
  `sum(by_source) > affected_qty`，这是“各来源对应数量”与“同一货物只算一次”的差别）。
- `totals.affected_outstanding = 在库 + 未收回 + 加工中在途`（同一件只出现一次）；
  已收回货物已重新计入在库，`totals.affected_recovered` 只是流量，不重复相加。
- 错误码：`RC_MISSING_RETURN_LINK`、`RC_UNKNOWN_EVENT`、`RC_DUPLICATE_EVENT`、
  `RC_EVENT_CONFLICT`、`RC_INVALID_LINK`、`RC_EVENT_MISMATCH`、`RC_RETURN_NO_TARGET`、
  `RC_RETURN_BATCH_MISMATCH`、`RC_RETURN_OVER`、`RC_NEGATIVE_BALANCE`。

### 6.4 演示（`sample_inputs/recall/`）

- `B-RAW`：`R-1=100`（受召回）+ `R-2=50`（后入库、不受召回）；`B-RAW2`：`R-3=200`。
- 加工 L1 领用 `I-1=30`（全部来自 R-1）→ 产出 `P-1=25`；L2 领用 `I-3=20` → `P-2=15`。
- 核销 `WO-1` 出库 `O-1=20`（FIFO 落在 R-1 段）；退回 `RT-1=10` 收回该行最早 10 件。
- 结果：未收回 `O-1=10`；受影响在库 `B-RAW=35`、`B-FG=40`；已收回 10；
  当前仍受影响总量 `85`（=在库 75 + 未收回 10，收回件已在在库中，不重复相加）。
- 运行 `python3 -I demo/recall_demo.py` 会打印清单并断言上述数值、只读性与十亿级数量。
