# 多权益联程票制最优计费

纯 Python 标准库实现。给定乘车事件与票制版，为每名乘客求**合法最低总费用**，并列逐程
金额与所用权益；支持多套餐、多权益额度、跨午夜夜班、同名异站、补传重算与不可变账版。

## 为什么不能逐程先到先占

一个套餐会同时占用 2~3 程并消耗一次权益。按事件顺序贪心（“先扫到先占”）会让早出现的
套餐拿走额度，而它省下的钱未必最多；权益还可能是多名乘客共享的全局池。本实现把问题建模
为**带资源约束的集合划分精确优化**：

- 每程恰好二选一：按单程计费，或属于一个套餐；
- 同时选中的套餐腿集合不得重叠；
- 每次套餐消耗 1 个权益实例，记在**首程服务日**（跨日套餐同此规则）；
- 个人×服务日、个人×跨日、共享×服务日、共享×跨日四类额度同时生效；
- 目标：全局总费用最小（整数分，全程整数运算，确定性并列裁决）。

## 算法

1. **摄入**：按稳定 `event_id` 去重；同 ID 字段不一致即冲突拒绝，旧事件不被覆盖。
2. **服务日**：带 UTC 偏移的绝对时刻落入半开区间 `[start, end)`；夜班可跨自然日；
   不属任何日历的事件标记 `no_service_day` 待核，不猜测归属、不参与计费。
3. **候选生成**：每名乘客按 `(时刻, event_id)` 排序，枚举长度 2~3 的**连续程窗口**，
   逐对校验有向换乘（前一程下车站 → 后一程上车站，可限线路）、方向、票种、首末最大
   实际经过时长、是否同一服务日。
4. **单乘客 DFS**：记忆化搜索 + 按“第一个未覆盖程”分支（单程或覆盖它的套餐），
   个人额度随状态走；对 `(费用, 共享消费向量)` 保留 Pareto 前沿。
5. **全局合并**：存在共享额度时，用资源 DP 合并各乘客前沿，在共享池上限内取全局最优；
   无共享额度时退化为各乘客独立最优。
6. **账版**：补传批次只产生新版本 `ledger-vN.json`（`supersedes` 指向前版），旧版原样
   保留；工作状态 `state.json` 与不可变账版分离。

对拍测试（`tests/test_bruteforce.py`）用独立暴力枚举器在 300 组随机票制（含 2/3 程模板、
个人/共享额度、随机票价）上验证求解器结果一致。

## 输入结构

```json
{
  "calendars": [{"service_day": "2026-11-24",
                 "start": "2026-11-24T03:00:00+08:00",
                 "end":   "2026-11-25T02:59:59+08:00"}],
  "stations":  [{"station_id": "C1", "name": "中心站"}],
  "transfers": [{"from_station": "B", "to_station": "B",
                 "from_line": null, "to_line": null}],
  "templates": [{"template_id": "T2", "size": 2,
                 "max_elapsed_minutes": 120,
                 "transfers": [{"from_station": "__ANY__", "to_station": "__ANY__"}],
                 "directions": [null, null],
                 "ticket_types": [null, null],
                 "same_service_day": true,
                 "entitlement": "E"}],
  "packages":  [{"package_id": "X", "template_id": "T2",
                 "price": 500, "allocations": [250, 250]}],
  "quotas":    [{"passenger_id": "P1", "service_day": "2026-11-24",
                 "entitlement": "E", "limit": 1}],
  "events":    [{"event_id": "e1", "passenger_id": "P1",
                 "ticket_type": "regular",
                 "time": "2026-11-24T08:00:00+08:00",
                 "line": "L1", "direction": "up",
                 "board_station": "A", "alight_station": "B", "fare": 300}]
}
```

约定：

- 所有时间为带 UTC 偏移的 ISO-8601；金额与分摊均为整数分，分摊和必须等于套餐总价。
- `transfers` 中 `{"from_station":"__ANY__","to_station":"__ANY__"}` 是哨兵：模板只声明
  “此处必须换乘”，实际站对须存在于全局 `transfers` 表。固定站对规则直接写具体 ID。
- 同名异站只靠稳定 `station_id` 区分，名称永不参与匹配。
- 额度键：`passenger_id=null` 为跨乘客共享池；`service_day=null` 为跨服务日总额度；
  一次套餐消耗同时扣减所有适用键，没有任何适用额度键的候选不可行。

## 输出

每名乘客给出 `total_fare`、逐程 `legs`（`billing=single|package`、`amount`、套餐内
`position`、权益扣减明细）、`packages`（含 `first_service_day` 与 `entitlement_charges`）
以及 `pending_events`；顶层给 `grand_total_fare` 与共享额度维度。

## 命令行

```bash
# 一次性计费
python -m transit_fare.cli price input.json -o bill.json
# 补传批次（账版目录只增不改）
python -m transit_fare.cli batch --ledger ./ledger_dir --input batch.json --note 备注
```

（隔离环境若用 `python -I`，请把项目根加入 `sys.path`，例如包一层启动脚本。）

## 四个手算例

数据见 `tests/examples_data.py`，断言见 `tests/test_examples.py`，可直接跑的 JSON 见
`examples/`。

### 例 1：共享权益竞争（`examples/ex1_shared.json`）

P1 四程 A→B→C→D→E，单程 300/400/300/500（合计 1500）；套餐 X 固定 500（分摊 250/250），
耗权益 E，当天 E 仅 1 次。合法连续窗口：

| 套餐 | 替掉单程 | 节省 |
|---|---|---|
| X1=(e1,e2) | 700 | 200 |
| X2=(e2,e3) | 700 | 200（与 X1 互斥） |
| X3=(e3,e4) | 800 | **300** |

- 逐程先到先占：扫到 e1 即占 X1，总费 1500−200=**1300**；
- 全局最优：唯一额度给 X3，总费 1500−300=**1200**（e1/e2 走单程，e3/e4 各摊 250）。
- 额度 2 时可同时选互不重叠的 X1+X3，总费 1000；额度 0 时全单程 1500。
- 跨乘客版（`example1_global_pool`）：两名乘客竞争同一个全局 E 池（2 次），资源 DP
  保证全系统恰好两个套餐、总费 2400，而不是被先处理的乘客占满。

### 例 2：跨午夜（`examples/ex2_midnight.json`）

服务日 2026-11-24 的日历为当天 03:00 至次日 02:59:59。e1 在 23:30（X→Y，400），
e2 在**次日** 00:20（Y→Z，300），绝对经过 50 分钟 ≤ 模板上限 90，且同属一个服务日，
套餐 N（600，分摊 300/300）合法，NQ 扣首程服务日 2026-11-24 一次。e3 在 03:30，落在两个
日历的空档，无法解释归属 → 待核、不计费（不猜站名、不猜服务日）。

### 例 3：同名异站（`examples/ex3_samename.json`）

C1、C2 两个稳定站都叫“中心站”。e1 为 A→C1，e2 为 **C2**→D；模板要求在 C1 同站换乘，
实际相邻程站对是 C2→D，稳定 ID 不匹配 → 套餐非法，两程全单程共 600，不耗权益。
对照组把 e2 上车站改为 C1 后套餐立即成立（500），证明匹配只认真实 ID 不看名称。

### 例 4：补传改变最优组合（`examples/ex4_base.json` / `ex4_supplement.json`）

首批三程方向 up/down/down。票制 P2（2 程 300，要求 [up,down]）、P3（3 程 480，方向不限）。

- **账版 v1**：P3=(e1,e2,e3)=480 严格优于 P2+e3 单程=500 → 总费 480，分摊 170/170/140。
- 补传 e4（up，D→E，350）后重算 **账版 v2**：
  - P2=(e1,e2)+e3+e4 单程 = 850；
  - P3=(e1,e2,e3)+e4 单程 = 830；
  - **P3=(e2,e3,e4)=480 + e1 单程 200 = 680（最优）**。
- v1 的逐程分摊被整体推翻，但 `ledger-v1.json` 原样保留，`ledger-v2.json` 以
  `supersedes=1` 落盘；补传批次中的完全重传去重、改价重传冲突拒绝均有记录。

## 测试

```bash
python -I -m unittest discover -s tests -v
```

17 个测试：四个手算例的逐步金额/权益断言、摄入去重与冲突、日历重叠/裸时间/分摊和校验、
每程恰好一次计费的内部断言，以及 300 组随机票制的暴力对拍。
