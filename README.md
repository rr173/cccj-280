# 批次可执行分配（规则版本 / 业务时点驱动）

按**指定规则版本**和**业务时点**，为待发订单生成可执行批次分配、未满足原因与改版差异，
并完整保留历史发布方案。纯标准库实现，无第三方依赖。

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

## 2. 目录结构

```
src/batch_alloc/
  model.py      领域模型、JSON 解析与结构校验（强制带时区时间、正整数数量等）
  engine.py     事件账本（去重/退回/负库存）、质量状态、规则选择、失效日、可用性
  planner.py    分支定界精确求解 + 独立穷举核验 + 阻断诊断
  registry.py   方案发布/撤回/重算、JSON 原子持久化、独立核验 verify_plan
  report.py     终端可读报告
src/cli.py      命令行入口
sample_inputs/  7 个演示数据集（不可信输入，独立目录）
tests/          23 项单元测试 + 100 组随机性质测试
demo/run_demo.py 端到端演示（含错误 FCFS 对照）
output/         运行产物（方案 JSON、登记册、日志）
```

## 3. 运行方式（隔离模式，防输入目录投毒）

```bash
# 端到端演示（全部断言通过才退出 0）
python3 -I demo/run_demo.py

# 测试
python3 -I tests/test_batch_alloc.py
python3 -I tests/test_random_property.py

# CLI
python3 -I src/cli.py plan   --input sample_inputs/demo_wide_narrow.json \
                             --as-of 2026-06-01T00:00:00Z \
                             --registry output/registry.json --out output/p.json
python3 -I src/cli.py recalc --input sample_inputs/demo_shelf.json \
                             --as-of 2026-03-20T00:00:00Z --rule-version v2 \
                             --plan-id P2 --registry output/registry.json
python3 -I src/cli.py history --registry output/registry.json
python3 -I src/cli.py ledger  --input sample_inputs/demo_returns.json \
                             --as-of 2026-06-01T00:00:00Z
```

无效历史退出码为 `3`；核验失败退出码为 `2`。

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
