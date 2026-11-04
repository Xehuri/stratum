# Loom claim 数据契约
contract: v1
status: frozen
owner: Stratum
updated: 2026-11-04

本文件定义 Loom v1 报告共用的 claim 标识、字段语法、状态机、关系与校验规则。`loom-daily.md`、`loom-weekly.md`、`loom-biweekly.md` 都必须遵守本文件。批次文件不是报告，不产生 claim，见 `loom-batch.md`。

## 1. 适用边界

- 只适用于 `report_contract: v1` 的 Loom 报告。
- 不适用于旧版叙事报告。旧报告标记 `contract_version: 0` 与检索类型 `legacy`，低权重，不重写。
- 不定义 SQL、数据库表、CLI 实现代码。字段语法和校验规则就是契约。
- 任何实现不得静默改写本契约。契约与实现冲突时，以契约为准。

## 2. 标识符

### 2.1 report_id

报告必须有稳定、全局唯一、可重复生成的 `report_id`。

| 报告类型 | 格式 | 示例 |
|---|---|---|
| 日复盘 | `RPT-D-YYYY-MM-DD` | `RPT-D-2026-01-05` |
| 周报 | `RPT-W-YYYY-MM-DD` | `RPT-W-2026-01-05` |
| 双周报 | `RPT-B-YYYY-MM-DD` | `RPT-B-2026-01-05` |

规则：

- `YYYY-MM-DD` 使用报告周期的 `period_start`。
- 修正版在末尾加 `-rN`，从 `-r2` 开始，例如 `RPT-D-2026-01-05-r2`。
- 同一 workspace 内不得有两个报告共用同一个 `report_id`。
- `report_id` 只含 ASCII 大写字母、数字和连字符。

### 2.2 claim_id

claim 必须由报告作者显式写出 `claim_id`，Stratum 不随机生成。

格式：

```text
<PREFIX>-<SCALE>-<PERIOD_START>-<NN>
```

| 段 | 含义 | 取值 |
|---|---|---|
| `PREFIX` | claim 类型前缀 | `OBS`、`CHG`、`FND`、`PAT`、`HYP` |
| `SCALE` | 报告尺度 | `D`、`W`、`B` |
| `PERIOD_START` | 报告周期起始本地日期 | `YYYY-MM-DD` |
| `NN` | 报告内顺序号 | `01` 到 `99` |

前缀与类型对应：

| 前缀 | claim_type |
|---|---|
| `OBS` | `observation` |
| `CHG` | `change` |
| `FND` | `finding` |
| `PAT` | `pattern` |
| `HYP` | `hypothesis` |

示例：

```text
OBS-D-2026-01-05-01
CHG-D-2026-01-05-01
OBS-W-2026-01-05-01
PAT-B-2026-01-05-01
```

规则：

- `claim_id` 在 workspace 内全局唯一。
- 顺序号按 claim 在 `## 检索条目` 中首次出现的顺序分配，从 `01` 开始。
- 已分配的 `claim_id` 永不复用，即使对应 claim 被拒绝或废止。
- 修订报告时，已有 claim 保留原 `claim_id`，新增 claim 使用下一个未占用的顺序号。
- `claim_id` 只含 ASCII 大写字母、数字和连字符。

## 3. 字段语法

### 3.1 claim 块

每个 claim 是 `## 检索条目` 下的一个三级标题块：

```text
### <claim_id> <标题>
- <key>: <value>
- <key>: <value>
```

规则：

- 三级标题必须以 claim_id 开头，后面跟一个空格和短标题。
- 短标题只用于阅读，不参与身份判断，长度 1 到 40 个字符。
- 字段行紧跟在三级标题之后，直到下一个三级标题或下一个二级标题。
- 字段行必须以 `- ` 开头，即一个 ASCII 连字符加一个 ASCII 空格。
- 键值分隔符必须是 `: `，即一个 ASCII 冒号加一个 ASCII 空格。
- 字段名只含小写 ASCII 字母、数字和下划线。
- 字段值必须是非空、单行、UTF-8 文本，首尾不得有空白。
- 值内部可以包含中文冒号、英文冒号、逗号、括号和空格。第一个 `: ` 之后的内容全部属于值。

### 3.2 列表字段

列表字段通过重复同一个键表达，顺序保留：

```text
- evidence: C-EXAMPLE-0001
- evidence: BLK-EXAMPLE-0001
- derived_from: OBS-D-2026-01-05-01
- derived_from: OBS-D-2026-01-06-01
```

不允许写成逗号数组、方括号数组或 JSON 数组。

### 3.3 禁止的结构

- 禁止 JSON 对象或数组。
- 禁止 YAML 嵌套映射、锚点、别名、flow 语法。
- 禁止多行字段值。
- 禁止表格作为 claim 字段。
- 禁止在 claim 块内使用 HTML。
- 禁止空值，例如 `- evidence:`。
- 禁止在报告中写 `- status:`。状态由 Stratum 管理。

### 3.4 日期与时间

Loom v1 的 claim 使用本地日历日期：

```text
period_start: YYYY-MM-DD
period_end: YYYY-MM-DD
```

语义：

- 区间为半开区间 `[period_start, period_end)`。
- 日复盘的 `period_start` 是被复盘日期，`period_end` 是次日。
- 周报的 `period_start` 是周一，`period_end` 是下周一。
- 双周报的 `period_start` 是第一个周一，`period_end` 是第二个周一之后的周一。
- 日期按报告 frontmatter 的 `timezone` 解释。
- `instant` 粒度保留给未来报告，Loom v1 不使用时间戳作为 claim 周期。

## 4. 通用字段

所有 claim 都必须包含以下字段。

| 字段 | 必需 | 说明 | 校验 |
|---|---|---|---|
| `claim_id` | 是 | claim 唯一标识 | 符合第 2.2 节格式，且在 workspace 内唯一 |
| `claim_type` | 是 | claim 类型 | 取值见第 5 节 |
| `period_start` | 是 | 周期起点，包含 | `YYYY-MM-DD` |
| `period_end` | 是 | 周期终点，不包含 | `YYYY-MM-DD`，必须晚于 `period_start` |
| `granularity` | 是 | 时间粒度 | `day`、`week`、`range` 之一，须与报告类型匹配 |
| `entities` | 是 | 涉及的实体 | 至少一条，每条 1 到 80 个字符 |
| `evidence` | 条件必需 | 原始证据引用 | 至少一条，或提供 `data_gap` |
| `confidence` | 是 | 置信度 | `high`、`medium`、`low` |
| `body` | 是 | 一句话到一段话的 claim 正文 | 单行，不得为空 |

可选通用字段：

| 字段 | 说明 |
|---|---|
| `data_gap` | 显式记录证据缺口，格式为一行原因 |
| `derived_from` | 引用下一层 claim，仅周报和双周报使用 |
| `supersedes` | 声明替换同层旧 claim |
| `contradicts` | 声明与另一 claim 存在矛盾 |
| `metric` | 指标名，Stratum 可解析到 series 时使用 |
| `baseline` | 基线值或基线描述 |
| `delta` | 相对基线的变化 |
| `sample_size` | 样本量，非负整数 |

字段 `status` 不是作者字段。报告中出现 `- status:` 时，lint 报 `STATUS_AUTHORED`。

## 5. claim 类型与专用字段

### 5.1 类型、尺度与产生者

| claim_type | 允许的 SCALE | 产生者 | 说明 |
|---|---|---|---|
| `observation` | `D`、`W` | 日复盘、周报 | 可测量或可观察的事实 |
| `change` | `D` | 日复盘 | 一天内的状态变化 |
| `finding` | `D` | 日复盘 | 有证据支持的当日解释或发现 |
| `pattern` | `B` | 双周报 | 跨两周重复出现的规律 |
| `hypothesis` | `B` | 双周报 | 尚未充分验证的机制假设 |

不允许的类型与尺度组合会被拒绝，例如 `OBS-B-...`、`PAT-W-...`。

### 5.2 granularity 映射

| 报告类型 | claim 类型 | granularity |
|---|---|---|
| 日复盘 | `observation`、`change`、`finding` | `day` |
| 周报 | `observation` | `week` |
| 双周报 | `pattern`、`hypothesis` | `range` |

`instant` 和 `month` 保留给未来报告，Loom v1 不使用。

### 5.3 observation

必需字段：通用字段。

可选字段：

- `metric`
- `baseline`
- `delta`
- `sample_size`

如果写了 `metric`，Stratum 必须能解析到 workspace 中的 series，否则 lint 报 `METRIC_UNRESOLVED`。没有 `metric` 时，`baseline` 和 `delta` 可以是自然语言描述。

示例：

```text
### OBS-D-2026-01-05-01 示例观测
- claim_id: OBS-D-2026-01-05-01
- claim_type: observation
- period_start: 2026-01-05
- period_end: 2026-01-06
- granularity: day
- entities: 示例实体
- evidence: C-EXAMPLE-0001
- confidence: medium
- body: 示例观测内容，仅用于说明格式。
```

### 5.4 change

必需字段：通用字段，加上 `baseline`，以及 `delta` 或 `outcome` 之一。

可选字段：`metric`、`sample_size`。

示例：

```text
### CHG-D-2026-01-05-01 示例变化
- claim_id: CHG-D-2026-01-05-01
- claim_type: change
- period_start: 2026-01-05
- period_end: 2026-01-06
- granularity: day
- entities: 示例实体
- evidence: C-EXAMPLE-0001
- confidence: medium
- baseline: 示例基线
- delta: 示例变化量
- body: 示例状态变化，仅用于说明格式。
```

### 5.5 finding

必需字段：通用字段。

可选字段：`metric`、`baseline`、`delta`、`sample_size`。

`finding` 是当日解释，不是跨周规律。不得在 `finding` 中写 `conditions`、`outcome`、`support_count`、`opportunity_count` 或 `counterexample_count`。这些字段属于双周报。

示例：

```text
### FND-D-2026-01-05-01 示例发现
- claim_id: FND-D-2026-01-05-01
- claim_type: finding
- period_start: 2026-01-05
- period_end: 2026-01-06
- granularity: day
- entities: 示例实体
- evidence: C-EXAMPLE-0001
- confidence: low
- body: 示例发现，仅用于说明格式。
```

### 5.6 pattern

必需字段：通用字段，加上以下字段。

| 字段 | 含义 | 校验 |
|---|---|---|
| `conditions` | 规律成立的条件 | 单行非空 |
| `outcome` | 条件出现后预期出现的结果 | 单行非空 |
| `support_count` | 条件和结果同时出现的次数 | 非负整数，至少为 1 |
| `opportunity_count` | 条件出现的总次数 | 非负整数，且不小于 `support_count` |
| `counterexample_count` | 条件出现但结果未出现的次数 | 非负整数 |

反例字段：

- 当 `counterexample_count` 大于 0 时，必须至少写一条 `counterexample_cards`。
- 当 `counterexample_count` 等于 0 时，必须写 `counterexample_note`，说明检查范围和未发现反例的原因。
- `opportunity_count` 必须不小于 `support_count + counterexample_count`。

可选字段：`lag`。

示例：

```text
### PAT-B-2026-01-05-01 示例规律
- claim_id: PAT-B-2026-01-05-01
- claim_type: pattern
- period_start: 2026-01-05
- period_end: 2026-01-19
- granularity: range
- entities: 示例实体
- derived_from: OBS-W-2026-01-05-01
- derived_from: OBS-W-2026-01-12-01
- evidence: C-EXAMPLE-0001
- evidence: C-EXAMPLE-0002
- conditions: 示例条件成立
- outcome: 示例结果出现
- support_count: 2
- opportunity_count: 3
- counterexample_count: 1
- counterexample_cards: C-EXAMPLE-0003
- confidence: low
- body: 示例规律，仅用于说明格式。
```

### 5.7 hypothesis

必需字段与 `pattern` 相同。

额外规则：

- `confidence` 只能是 `low` 或 `medium`。
- `hypothesis` 不得使用 `high`。高置信度只能给经过复现和人工批准的 `pattern`。
- `hypothesis` 默认保持 `draft`，不得由 `03-biweekly-review` 自行转正。

示例：

```text
### HYP-B-2026-01-05-01 示例假设
- claim_id: HYP-B-2026-01-05-01
- claim_type: hypothesis
- period_start: 2026-01-05
- period_end: 2026-01-19
- granularity: range
- entities: 示例实体
- derived_from: OBS-W-2026-01-05-01
- derived_from: OBS-W-2026-01-12-01
- evidence: C-EXAMPLE-0001
- evidence: C-EXAMPLE-0002
- conditions: 示例条件成立
- outcome: 示例结果可能出现
- support_count: 2
- opportunity_count: 3
- counterexample_count: 0
- counterexample_note: 已检查两周内全部周观测，未发现反例。
- confidence: low
- body: 示例假设，仅用于说明格式。
```

## 6. 状态机

### 6.1 状态

| 状态 | 含义 | 进入方式 |
|---|---|---|
| `draft` | 已入库但未批准 | 报告 ingest 后自动进入 |
| `active` | 可用于查询和上层引用 | promotion |
| `superseded` | 已被同层新 claim 替换 | 新 claim 转 active 时自动进入 |
| `rejected` | 明确拒绝 | review reject |
| `stale` | 证据或下层依赖失效 | 自动检查或人工标记 |

### 6.2 允许的状态迁移

```text
draft -> active        promotion
draft -> rejected      review reject
rejected -> draft      review reopen
active -> stale        证据失效、依赖失效或人工标记
stale -> active        re-promotion
active -> superseded   新的同层 claim 转 active
stale -> superseded    新的同层 claim 转 active
```

其他迁移一律拒绝。

### 6.3 状态规则

- 所有 claim 入库时都是 `draft`。作者不能直接写 `active`。
- `superseded` 是终态，不能回到 `draft` 或 `active`。
- `rejected` 只能通过 review reopen 回到 `draft`。
- `active` claim 的内容不可修改。需要修改时，创建新的 `claim_id`，并在新 claim 上写 `supersedes`。
- `draft` claim 可以在同一 `report_id` 的修订版中更新，前提是该报告的所有 claim 仍是 `draft`。一旦报告中任一 claim 进入 `active`，该 `report_id` 不可再修订。
- 不允许 `draft` 支撑 `active`。一个 `active` claim 的传递 `derived_from` 闭包中不得出现 `draft`、`rejected`、`superseded` 或 `stale`。

## 7. 关系与层级

### 7.1 层序

```text
source block
  -> card
  -> daily claim (OBS/CHG/FND, D)
  -> weekly observation (OBS, W)
  -> biweekly pattern or hypothesis (PAT/HYP, B)
```

层序是硬约束：

- 上层可以引用下层作为证据。
- 下层不得引用上层。
- `derived_from` 只能指向紧邻的下一层 claim。
- `supersedes` 只能指向同一层的 claim。
- `contradicts` 只能指向同层或更下层的 claim。

### 7.2 evidence

`evidence` 指向原始证据，不指向上层 claim。

允许的引用形式：

| 形式 | 指向 | 说明 |
|---|---|---|
| `C-...` | card | 卡片必须存在且未被删除 |
| `BLK-...` | block | 块必须存在 |
| `BLK-...#sha256:<64 hex>` | block + snapshot | 快照哈希必须匹配当前块快照 |
| `snapshot:sha256:<64 hex>` | snapshot | 必须能解析到 workspace 内唯一块快照 |

规则：

- 每个 evidence 引用必须在 ingest 时解析成功，否则报告拒绝。
- 不允许用 `prose` 块作为 evidence。
- 如果某个 `prose` 块需要成为证据，必须先由人工或主会话将其提升为结构化 card，并记录 author 与 reason。提升后的 card 才可以被引用。
- 不允许用 `## 叙事` 或 `## 运维` 中的文本作为 evidence。
- 不允许用上层 claim 作为 evidence。

### 7.3 derived_from

`derived_from` 指向紧邻下一层 claim。

| 上层 | 允许的 `derived_from` |
|---|---|
| 日复盘 claim | 不允许使用 `derived_from`，日层通过 `evidence` 连接卡片与块 |
| 周报 observation | 日复盘 claim，且日期落在本周 `[period_start, period_end)` 内 |
| 双周报 pattern 或 hypothesis | 周报 observation，且周期落在本双周 `[period_start, period_end)` 内 |

规则：

- 周报和双周报至少有一条 `derived_from`。
- `derived_from` 目标必须存在，且不得是 `rejected` 或 `superseded`。
- promotion 时，所有 `derived_from` 目标必须是 `active`。
- 如果某个 `derived_from` 目标变为 `stale`，上层 claim 必须在同一事务中变为 `stale`，并递归向上传播。
- 不允许跨周期引用。周报不得引用本周之外的日 claim。双周报不得引用本双周之外的周 observation。

### 7.4 supersedes

`supersedes` 表示同层新 claim 替换旧 claim。

规则：

- 只能指向同层、同类型族的 claim。类型族按 `observation`、`change`、`finding`、`pattern`、`hypothesis` 区分。
- 目标 `period_start` 不得晚于新 claim 的 `period_start`。
- 目标 `period_end` 不得晚于新 claim 的 `period_end`。
- 允许同周期替换，用于修正同一报告周期内的 active claim。
- 不得指向自己。
- 不得形成环。
- 目标处于 `active` 时，只有新 claim 转 `active` 后，目标才变为 `superseded`。新 claim 保持 `draft` 时，目标状态不变。
- 一个 claim 最多 supersede 一个旧 claim。

### 7.5 contradicts

`contradicts` 表示两个 claim 在语义上冲突。

规则：

- 只能指向同层或更下层的 claim。
- 不得指向自己。
- 不允许层级反向。
- 互相矛盾的一对 claim 可以互相写 `contradicts`，Stratum 将其规范化为一条无向冲突记录。
- `contradicts` 本身不改变状态，只产生 review 标记。未解决的冲突在 `stratum status` 和 `stratum fsck` 中显示为 `CONTRADICTION_OPEN`。
- 不允许通过 `contradicts` 形成派生环。

### 7.6 环检测

- `derived_from` 和 `supersedes` 必须构成有向无环图。
- 发现环时，整个报告 ingest 失败，报 `CYCLE_DETECTED`。
- `contradicts` 允许双向边，但禁止自环。

## 8. 证据下限

- 每个 claim 必须至少有一条 `evidence`，或者一条 `data_gap`。
- `data_gap` 不是证据，只表示作者明确承认缺口。
- 只有 `data_gap` 而没有 `evidence` 的 claim 可以保持 `draft`，但不能 promotion 到 `active`。
- 要 promotion，必须先补齐 evidence 并移除 `data_gap`。
- 证据必须在 ingest 和 promotion 两个时点都能解析。
- 块快照哈希不匹配时：
  - `draft` claim 被拒绝，报 `EVIDENCE_SNAPSHOT_MISMATCH`。
  - `active` claim 变为 `stale`。
- 卡片或块被标记 missing 时，引用它的 claim 变为 `stale`。

## 9. 幂等与冲突

### 9.1 身份与指纹

- 报告身份是 `report_id`。
- claim 身份是 `claim_id`。
- Stratum 对每个 claim 计算内容指纹。指纹基于解析后的字段，按字段名字典序排列，重复键保持文档顺序，使用 UTF-8 和 LF 换行，去除字段值首尾空白。
- 指纹不包含 `status`，因为状态由 Stratum 管理。

### 9.2 报告级幂等

- 相同 `report_id`、相同 claim 集合、相同指纹重复 ingest，视为 no-op，退出码为 0。
- 相同 `report_id`、不同内容，且该报告的所有 claim 仍为 `draft` 时，允许修订。修订会更新 draft，缺失的 draft 标记为 `rejected`，原因记为 `REMOVED_FROM_REPORT`。
- 相同 `report_id`、不同内容，且该报告已有任一 claim 进入 `active` 或更后状态时，拒绝 ingest，报 `REPORT_IMMUTABLE`。修正必须使用新的 `report_id`，并在新 claim 上写 `supersedes`。
- 报告 ingest 是事务性的。任何 claim 校验失败时，整包回滚，不留下部分 claim。

### 9.3 claim 级冲突

- 不同报告之间不得复用同一个 `claim_id`。
- 如果 Stratum 发现 `claim_id` 已存在且属于另一个 `report_id`，拒绝 ingest，报 `CLAIM_ID_CONFLICT`。
- 如果同一 `claim_id` 在同一报告修订中改变类型或尺度，拒绝 ingest，报 `CLAIM_ID_CONFLICT`。

### 9.4 review 幂等

- promotion 一个已经 `active` 的 claim 是 no-op，返回提示，不报错。
- reject 一个已经 `rejected` 的 claim 是 no-op。
- reopen 一个不是 `rejected` 的 claim 报 `STATE_INVALID`。
- 所有 promotion、reject、reopen 都必须记录 author、reason 和时间。

## 10. lint 等级

| 等级 | 行为 |
|---|---|
| `strict` | 任何 ERROR 都拒绝 ingest |
| `advisory` | ERROR 对应的 claim 以 `draft` 入库，标记 `contract_level: 0`，不得 promotion；WARN 允许入库但 promotion 前必须解决或由 human 显式豁免 |
| `legacy` | 不按本契约检查，标记 `contract_version: 0` 和 `retrieval_type=legacy`，低权重，不参与 promotion |

约定：

- Loom v1 报告使用 `--profile loom-daily`、`--profile loom-weekly`、`--profile loom-biweekly`。
- 新 Loom 报告不得使用 `legacy`。
- `advisory` 是默认策略，`strict` 可选。
- 无论哪种策略，`active` 查询只返回通过证据下限和依赖检查的 claim。

## 11. 校验码

以下是契约级校验码，CLI 输出为纯文本。

| 校验码 | 含义 |
|---|---|
| `CONTRACT_VERSION_UNKNOWN` | `report_contract` 不是 `v1` |
| `FRONTMATTER_MISSING` | 缺少 frontmatter |
| `FRONTMATTER_FIELD_MISSING` | 缺少必需 frontmatter 字段 |
| `SECTION_MISSING` | 缺少必需章节 |
| `SECTION_UNKNOWN` | 出现未定义章节 |
| `CLAIM_ID_INVALID` | claim_id 格式错误 |
| `CLAIM_ID_CONFLICT` | claim_id 被其他报告占用 |
| `CLAIM_FIELD_MISSING` | 缺少必需字段 |
| `CLAIM_FIELD_UNKNOWN` | 出现未定义字段 |
| `CLAIM_TYPE_INVALID` | claim_type 非法 |
| `CLAIM_SCALE_INVALID` | claim 类型与尺度不匹配 |
| `STATUS_AUTHORED` | 报告中写了 status |
| `EVIDENCE_MISSING` | 既无 evidence 也无 data_gap |
| `EVIDENCE_UNRESOLVED` | evidence 无法解析 |
| `EVIDENCE_PROSE` | 用 prose 块作为 evidence |
| `EVIDENCE_SNAPSHOT_MISMATCH` | 块快照哈希不匹配 |
| `DERIVED_FROM_MISSING` | 周报或双周报缺少 derived_from |
| `DERIVED_FROM_LAYER` | derived_from 跨层或跨周期 |
| `DERIVED_FROM_NOT_ACTIVE` | promotion 时 derived_from 目标不是 active |
| `COUNTEREXAMPLE_MISSING` | pattern 或 hypothesis 缺少反例字段 |
| `CYCLE_DETECTED` | derived_from 或 supersedes 形成环 |
| `REPORT_IMMUTABLE` | 已激活报告被修改 |
| `CLAIM_IMMUTABLE` | active claim 内容被修改 |
| `SELF_PROMOTION_FORBIDDEN` | 产出者自行转正 pattern 或 hypothesis |
| `OPS_AS_CLAIM` | 运维内容被写入检索条目 |
| `NARRATIVE_AS_CLAIM` | 叙事内容被写入检索条目或作为 evidence |

## 12. 最小完整示例

以下示例只用于说明格式，所有实体和证据均为合成值。

```text
### OBS-W-2026-01-05-01 示例周观测
- claim_id: OBS-W-2026-01-05-01
- claim_type: observation
- period_start: 2026-01-05
- period_end: 2026-01-12
- granularity: week
- entities: 示例实体
- derived_from: OBS-D-2026-01-05-01
- derived_from: CHG-D-2026-01-06-01
- evidence: C-EXAMPLE-0001
- evidence: BLK-EXAMPLE-0001
- confidence: medium
- body: 示例周观测，只用于说明格式。
```

## 13. 冻结与变更

- 本文件是 v1 契约，`status: frozen`。
- 任何字段、状态、关系或校验规则的变更都必须升级到 v2，并附带迁移说明。
- Loom prompt 必须引用本文件与对应报告契约。
- Stratum lint 必须按本文件实现。
- 任何实现不得静默重解释字段含义。
