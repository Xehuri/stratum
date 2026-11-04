# Loom 日复盘数据契约
contract: v1
status: frozen
owner: Stratum
updated: 2026-11-04

本文件定义 Loom 日复盘（`02-daily-review`）的输出格式。日复盘负责把批次 source blocks 转成卡片与日层 claim。日复盘不生产周观测、pattern 或 hypothesis。

## 1. 定位

日复盘是 source 之后的第一层语义报告：

```text
批次 blocks -> cards + daily claims
```

日复盘输出：

- `## 叙事`：给人读的上下文，低权重全文检索。
- `## 检索条目`：原子化日层 claim，高质量检索。
- `## 运维`：命令、失败、补数、异常，不进入 claim。

报告路径由 workspace 配置决定。本文示例使用 `reports/daily/YYYY-MM-DD.md`。

## 2. frontmatter

### 2.1 精确格式

日复盘必须以下列 frontmatter 开头：

```yaml
---
report_contract: v1
report_type: daily
report_id: RPT-D-2026-01-05
period_start: 2026-01-05
period_end: 2026-01-06
granularity: day
author: 02-daily-review
base_commit: CMT-0123456789abcdef
created_at: 2026-01-06T00:30:00+08:00
timezone: Asia/Shanghai
---
```

### 2.2 字段

| 字段 | 必需 | 取值与规则 |
|---|---|---|
| `report_contract` | 是 | 固定为 `v1` |
| `report_type` | 是 | 固定为 `daily` |
| `report_id` | 是 | `RPT-D-YYYY-MM-DD`，修正版加 `-rN` |
| `period_start` | 是 | 被复盘本地日期，`YYYY-MM-DD` |
| `period_end` | 是 | 次日，`YYYY-MM-DD`，半开区间 |
| `granularity` | 是 | 固定为 `day` |
| `author` | 是 | 固定为 `02-daily-review` |
| `base_commit` | 是 | 生成报告时的 Stratum HEAD，格式为 `CMT-` + 16 位小写十六进制；兼容旧报告的 40 位小写十六进制 git commit id；没有 HEAD 时写 `none` |
| `created_at` | 是 | RFC 3339 时间戳，必须带时区偏移 |
| `timezone` | 是 | IANA 时区名，例如 `Asia/Shanghai` |

规则：

- frontmatter 只允许上述字段，不得增加自定义键。
- 只允许单行标量，不允许嵌套、列表、锚点或 flow 语法。
- `period_start` 必须早于 `period_end`。
- `report_id` 中的日期必须等于 `period_start`。
- 同一日期只能有一个当前有效 `report_id`。修正版使用 `-r2`、`-r3` 递增。

## 3. 正文结构

正文必须且只能包含以下三个二级章节，顺序固定：

```text
## 叙事
## 检索条目
## 运维
```

规则：

- 三个章节各出现一次。
- 不得增加其他二级章节。
- `## 叙事` 和 `## 运维` 内不得使用三级标题。
- 三级标题只允许出现在 `## 检索条目` 下。
- 章节标题必须逐字匹配，包括中文和空格。

## 4. `## 叙事`

用途：

- 给人读的当日上下文。
- 可以写段落、列表、粗体、引用。
- 可以解释事件之间的叙事关系。

限制：

- 不得写 claim 字段，例如 `claim_id`、`claim_type`、`period_start`、`evidence`、`confidence`。
- 不得把叙事段落当作证据。
- 叙事不进入 claim 索引，只做低权重全文检索。
- 叙事中的结论不能替代 `## 检索条目`。如果结论需要被检索、引用或进入周报，必须写成 claim。

## 5. `## 检索条目`

### 5.1 claim 块格式

每个 claim 是一个三级标题块：

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

规则：

- 三级标题必须以 `claim_id` 开头，后跟一个空格和短标题。
- 字段语法遵循 `claim-schema.md`。
- 一个 claim 只表达一个原子事实。不要把多个事实用“并且”“同时”“另外”塞进同一个 claim。
- 同一 claim 的字段行之间不得有空行。claim 之间可以有空行。
- 每个 claim 的 `period_start`、`period_end`、`granularity` 必须与报告 frontmatter 一致。
- 日层 claim 不得使用 `derived_from`。日层通过 `evidence` 连接卡片与块。
- 日层 claim 不得使用 `conditions`、`outcome`、`support_count`、`opportunity_count`、`counterexample_count`。这些字段属于双周报。
- 报告中不得写 `- status:`。状态由 Stratum 管理。

### 5.2 日层 claim 类型

日复盘只允许三种 claim 类型：

| 类型 | 前缀 | 用途 | 专用必需字段 |
|---|---|---|---|
| `observation` | `OBS` | 当日可观察事实 | 无 |
| `change` | `CHG` | 当日状态变化 | `baseline`，以及 `delta` 或 `outcome` 之一 |
| `finding` | `FND` | 有证据支持的当日解释 | 无 |

不允许在日复盘中写 `pattern` 或 `hypothesis`。

### 5.3 通用必需字段

每个日层 claim 必须包含：

- `claim_id`
- `claim_type`
- `period_start`
- `period_end`
- `granularity`
- `entities`
- `evidence`
- `confidence`
- `body`

`evidence` 至少一条，或写一条 `data_gap`。只有 `data_gap` 的 claim 可以保持 `draft`，不能 promotion 到 `active`。

### 5.4 类型示例

observation：

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

change：

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

finding：

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

### 5.5 claim 数量

- 有可用证据的日期，建议写 1 到 10 条 claim。
- 不设硬上限，但每条都必须通过证据下限。
- 没有任何可用证据时，可以写 0 条 claim，但必须在 `## 运维` 中写明 `data_gap` 原因。
- 不得为了凑数量把运维事实、命令结果或叙事结论写成 claim。

## 6. 卡片

### 6.1 卡片来源

日复盘可以从批次块创建卡片。卡片必须引用至少一个块作为证据。

允许的卡片类型：

```text
event
observation
finding
state
decision
```

卡片 ID 由 Stratum 分配，作者不手写卡片 ID。

### 6.2 卡片创建

卡片通过 CLI 创建，不直接写数据库。命令形式由 CLI 契约定义，语义要求如下：

```bash
stratum add card --block BLK-EXAMPLE-0001 --type event --body "示例卡片内容"
```

要求：

- 至少一个 `--block`。
- 卡片正文不包含 claim 字段。
- 卡片创建后默认为 `draft`。
- 同一个块可以创建多张卡片，但不得重复创建完全相同正文的卡片。

### 6.3 卡片 promotion

日复盘负责把当日卡片从 `draft` 提升为 `active`。

批量 promotion：

```bash
stratum review bulk-promote --type card --date 2026-01-05 --author 02-daily-review --reason "daily card review"
```

规则：

- `--type card` 只处理指定日期的卡片。
- 所有被选中的卡片必须证据可解析、正文非空、没有 stale 块。
- 批量操作对选中集合是事务性的。任一卡片失败，整批回滚，退出码 1。
- 已经 active 的卡片是 no-op。
- 卡片 promotion 记录 author、reason 和时间。
- 不得用 `bulk-promote` 提升周报 observation 或双周报 pattern。

## 7. `## 运维`

用途：

- 记录命令、退出码、失败、补数、workspace 状态、工具异常。
- 记录当日 source 覆盖检查和修复。
- 记录 `data_gap` 原因。

限制：

- 运维内容不是 claim。
- 运维内容不是 evidence。
- 运维内容不进入 observation 或 pattern 层。
- 运维章节不参与 claim 解析，即使其中出现 `- key: value` 形式的行。
- 不得在运维章节写 `claim_type`、`claim_id`、`derived_from`、`support_count` 或 `counterexample_count`。
- 如果运维异常影响了当日结论，可以在 `## 叙事` 中解释，但仍然不能把运维事实写成 claim。

示例：

```text
## 运维

- 命令: stratum status --date 2026-01-05
- 结果: 块覆盖完整
- 补数: 无
- 异常: 无
```

## 8. 工作流

日复盘按以下顺序执行。

### 8.1 检查 source 覆盖

```bash
stratum status --date 2026-01-05
```

检查：

- 当日文档是否存在。
- 当日块是否缺失。
- 是否有 stale 块。
- 是否有未 ingest 的批次文件。

### 8.2 补 source

发现缺失块时：

1. 修正批次源文件。
2. 重新执行 `stratum ingest source <file>`。
3. 再次运行 `stratum status --date 2026-01-05`。
4. 覆盖完整后才写 claim。

不得在块缺失时写依赖该块的 claim。

### 8.3 建卡

按第 6 节创建卡片。

### 8.4 写报告

按第 2 到第 7 节写日报。写完后执行：

```bash
stratum ingest report reports/daily/2026-01-05.md
```

要求：

- ingest 是事务性的。任一 claim 失败时整包回滚。
- ingest 成功后所有 claim 为 `draft`。
- ingest 失败时修正报告并重新执行。
- 不得直接写 SQLite。

### 8.5 提升卡片

```bash
stratum review bulk-promote --type card --date 2026-01-05 --author 02-daily-review --reason "daily card review"
```

### 8.6 提升日层 claim

单条：

```bash
stratum review promote OBS-D-2026-01-05-01 --author 02-daily-review --reason "evidence checked"
```

当日批量：

```bash
stratum review bulk-promote --type claim --date 2026-01-05 --author 02-daily-review --reason "daily claim review"
```

规则：

- 只有通过证据下限和字段校验的 claim 才能 promotion。
- 带 `data_gap` 的 claim 不得 promotion。
- 依赖 draft 卡片的 claim 不得 promotion。需要先把卡片 promotion 为 active。
- 批量操作对选中集合是事务性的。任一 claim 失败，整批回滚。
- 已经 active 的 claim 是 no-op。
- promotion 只允许 card 和日层 claim。不得提升周报或双周报 claim。

### 8.7 验证

```bash
stratum query --type claim --from 2026-01-05 --to 2026-01-06 --status active
```

确认：

- 当日 active claim 可检索。
- 每条 active claim 都有 evidence。
- 没有运维事实进入 claim。

## 9. 完整示例

以下内容全部为合成数据，只用于说明格式。

```text
---
report_contract: v1
report_type: daily
report_id: RPT-D-2026-01-05
period_start: 2026-01-05
period_end: 2026-01-06
granularity: day
author: 02-daily-review
base_commit: CMT-0123456789abcdef
created_at: 2026-01-06T00:30:00+08:00
timezone: Asia/Shanghai
---

## 叙事

示例日期的上下文说明。这里只写给人读的背景，不写 claim 字段。

## 检索条目

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

## 运维

- 命令: stratum status --date 2026-01-05
- 结果: 块覆盖完整
- 补数: 无
- 异常: 无
```

## 10. 幂等与失败

- 相同 `report_id`、相同内容重复 ingest 是 no-op。
- 报告所有 claim 仍为 `draft` 时，允许用同一 `report_id` 修订。修订会更新 draft，缺失的 draft 标记为 `rejected`，原因 `REMOVED_FROM_REPORT`。
- 报告已有任一 claim 进入 `active` 或更后状态时，`report_id` 不可修订。修正必须使用新的 `report_id`，并在新 claim 上写 `supersedes`。
- `stratum ingest report` 失败时整包回滚，不留下部分 claim。
- `stratum review bulk-promote` 失败时整批回滚，不留下部分 promotion。
- 源文件修复优先于报告修订。块缺失时不得用新 claim 掩盖。

## 11. 验收

日复盘契约通过的条件：

- frontmatter 字段完整且精确。
- 三个章节齐全且顺序正确。
- claim 必填字段 100%。
- claim evidence 100%，或显式 `data_gap`。
- 运维内容进入 claim 的数量为 0。
- 日层 claim 类型只包含 `observation`、`change`、`finding`。
- 日层 claim 没有 `derived_from`。
- 所有 promotion 都有 author 和 reason。
- 没有直接写 SQLite。

## 12. 冻结与变更

- 本文件是 v1 契约，`status: frozen`。
- 章节、frontmatter、claim 类型、promotion 权限的变更必须升级到 v2。
- Loom `02-daily-review` prompt 必须引用本文件和 `claim-schema.md`。
- 任何实现不得静默改变日复盘的字段含义或章节顺序。
