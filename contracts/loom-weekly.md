# Loom 周报数据契约
contract: v1
status: frozen
owner: Stratum
updated: 2026-11-04

本文件定义 Loom 周报（`04-weekly-review`）的输出格式。周报负责把一周的日层 claim 聚合成 3 到 7 条周观测。周报不重复事件流水，不生产 pattern 或 hypothesis。

## 1. 定位

周报位于日层之上：

```text
daily claims -> weekly observations
```

周报只写 `observation`，每条 observation 必须来自日层 claim，并补充原始证据。周报不是一周事件的清单，而是跨日聚合、对比和趋势观察。

报告路径由 workspace 配置决定。本文示例使用 `reports/weekly/YYYY-MM-DD.md`。

## 2. frontmatter

### 2.1 精确格式

周报必须以下列 frontmatter 开头：

```yaml
---
report_contract: v1
report_type: weekly
report_id: RPT-W-2026-01-05
period_start: 2026-01-05
period_end: 2026-01-12
granularity: week
author: 04-weekly-review
base_commit: 0123456789abcdef0123456789abcdef01234567
created_at: 2026-01-12T01:00:00+08:00
timezone: Asia/Shanghai
---
```

### 2.2 字段

| 字段 | 必需 | 取值与规则 |
|---|---|---|
| `report_contract` | 是 | 固定为 `v1` |
| `report_type` | 是 | 固定为 `weekly` |
| `report_id` | 是 | `RPT-W-YYYY-MM-DD`，修正版加 `-rN` |
| `period_start` | 是 | 本周周一，`YYYY-MM-DD` |
| `period_end` | 是 | 下周一，`YYYY-MM-DD`，半开区间 |
| `granularity` | 是 | 固定为 `week` |
| `author` | 是 | 固定为 `04-weekly-review` |
| `base_commit` | 是 | 生成报告时 Stratum HEAD 的 40 位小写十六进制 commit id；没有 HEAD 时写 `none` |
| `created_at` | 是 | RFC 3339 时间戳，必须带时区偏移 |
| `timezone` | 是 | IANA 时区名 |

规则：

- frontmatter 只允许上述字段。
- 只允许单行标量，不允许嵌套、列表、锚点或 flow 语法。
- `period_start` 必须是周一。
- `period_end` 必须等于 `period_start` 加 7 天。
- `report_id` 中的日期必须等于 `period_start`。

## 3. 正文结构

正文必须且只能包含以下三个二级章节，顺序固定：

```text
## 叙事
## 检索条目
## 运维
```

规则与日复盘一致：

- 三个章节各出现一次。
- 不得增加其他二级章节。
- 三级标题只允许出现在 `## 检索条目` 下。
- `## 叙事` 和 `## 运维` 不参与 claim 解析。

## 4. `## 叙事`

用途：

- 给读者提供这一周的整体背景。
- 可以写趋势、转折、上下文和阅读提示。

限制：

- 不得写 claim 字段。
- 不得把叙事当作证据。
- 不得把一周事件按天罗列成流水账。
- 叙事不进入 observation 层，只做低权重全文检索。

## 5. `## 检索条目`

### 5.1 claim 块格式

每条周观测是一个三级标题块：

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
- confidence: medium
- body: 示例周观测，跨日聚合，不重复单日事件。
```

规则：

- 三级标题必须以 `claim_id` 开头，后跟一个空格和短标题。
- 字段语法遵循 `claim-schema.md`。
- 每条 observation 的 `period_start`、`period_end`、`granularity` 必须与报告 frontmatter 一致。
- 报告中不得写 `- status:`。

### 5.2 数量和类型

- 周报必须包含 3 到 7 条 observation，含 3 和 7。
- 少于 3 条时 lint 报 `OBS_COUNT_LOW`，报告不得 ingest。
- 多于 7 条时 lint 报 `OBS_COUNT_HIGH`，报告不得 ingest。
- 只允许 `claim_type: observation`。
- 不允许在周报中写 `change`、`finding`、`pattern` 或 `hypothesis`。
- 周报 observation 的 `claim_id` 格式为 `OBS-W-YYYY-MM-DD-NN`。

少于 3 条通常说明数据不足。数据不足时，不写周报，回到日复盘补齐 source 和 daily claims。不得为了满足数量而把单日事件或运维事实写成周观测。

### 5.3 通用必需字段

每条周观测必须包含：

- `claim_id`
- `claim_type`
- `period_start`
- `period_end`
- `granularity`
- `entities`
- `derived_from`
- `evidence`
- `confidence`
- `body`

可选字段：

- `metric`
- `baseline`
- `delta`
- `sample_size`

### 5.4 derived_from

周报必须通过 `derived_from` 引用日层 claim。

规则：

- 至少两条 `derived_from`。
- 至少来自两个不同的日期。两条 claim 不能都来自同一天。
- 只能引用 `OBS-D-...`、`CHG-D-...` 或 `FND-D-...`。
- 不得引用周报或双周报 claim。
- 引用的日 claim 必须落在本周 `[period_start, period_end)` 内。
- 引用的日 claim 在周报 ingest 时必须存在，且不是 `rejected` 或 `superseded`。
- 周报 promotion 时，所有 `derived_from` 目标必须是 `active`。
- 如果任一 `derived_from` 目标变为 `stale`，该周观测必须变为 `stale`。

### 5.5 evidence

每条周观测必须至少有一条原始证据：

- `C-...` 卡片。
- `BLK-...` 块。
- `BLK-...#sha256:<64 hex>` 带快照哈希的块。
- `snapshot:sha256:<64 hex>` 快照引用。

规则：

- `derived_from` 不能替代 `evidence`。周观测必须同时有日层来源和原始证据。
- 证据必须在 ingest 和 promotion 时都能解析。
- 不得使用 `prose` 块、叙事章节或运维章节作为证据。
- 如果证据来自卡片，卡片在 promotion 时必须是 `active`。

## 6. 不重复事件流水

周报的核心质量要求是跨日聚合。

必须满足：

- 每条周观测至少聚合两个不同日期的日 claim。
- 周观测描述的是跨日趋势、重复模式、对比、累积或状态变化，不是单日事件的复述。
- 不得按天罗列一周发生的事件。
- 不得把日 claim 的 body 原样复制到周观测。
- 不得把事件卡片标题或事件清单搬进周报。
- 如果某个结论只适用于一天，它应留在日层，不应升级为周观测。

允许的写法：

```text
- body: 本周有三个不同日期出现同类变化，变化方向一致。
```

不允许的写法：

```text
- body: 周一发生事件甲，周二发生事件乙，周三发生事件丙。
```

## 7. promotion

周报由周复盘（`04-weekly-review`）负责 promotion。

单条 promotion：

```bash
stratum review promote OBS-W-2026-01-05-01 --author 04-weekly-review --reason "derived_from active; evidence checked"
```

规则：

- 所有 `derived_from` 目标必须是 `active`。
- 所有 `evidence` 必须可解析。
- 周观测数量必须是 3 到 7。
- 必须通过不重复事件流水检查。
- 已经 active 的 observation 是 no-op。
- 周复盘只能 promotion 周观测，不得 promotion 日层 claim 或双周报 pattern。
- 周复盘不得使用 `bulk-promote`。`bulk-promote` 只允许 card 和日层 claim。
- promotion 记录 author、reason 和时间。

## 8. 工作流

### 8.1 查询日层

```bash
stratum query --type claim --from 2026-01-05 --to 2026-01-12 --status active
```

也可以按类型查询：

```bash
stratum query --type claim --claim-type observation --from 2026-01-05 --to 2026-01-12 --status active
stratum query --type claim --claim-type change --from 2026-01-05 --to 2026-01-12 --status active
```

要求：

- 只读取 active 日层 claim。
- 如果日层 claim 不足或 coverage 不完整，回到日复盘补齐。
- 不得用 draft 日 claim 写周报。draft 可以在 ingest 时存在，但 promotion 前必须全部 active。

### 8.2 写周报

按第 2 到第 7 节写报告。

### 8.3 ingest

```bash
stratum ingest report reports/weekly/2026-01-05.md
```

要求：

- ingest 是事务性的。任一 observation 失败时整包回滚。
- ingest 成功后所有 observation 为 `draft`。
- 不得直接写 SQLite。

### 8.4 promotion

逐条执行：

```bash
stratum review promote OBS-W-2026-01-05-01 --author 04-weekly-review --reason "derived_from active; evidence checked"
stratum review promote OBS-W-2026-01-05-02 --author 04-weekly-review --reason "derived_from active; evidence checked"
stratum review promote OBS-W-2026-01-05-03 --author 04-weekly-review --reason "derived_from active; evidence checked"
```

### 8.5 验证

```bash
stratum query --type observation --from 2026-01-05 --to 2026-01-12 --status active
```

确认：

- 3 到 7 条周观测。
- 每条都有至少两条来自不同日期的 `derived_from`。
- 每条都有原始 evidence。
- 没有事件流水复述。

## 9. 完整示例

以下内容全部为合成数据，只用于说明格式。

```text
---
report_contract: v1
report_type: weekly
report_id: RPT-W-2026-01-05
period_start: 2026-01-05
period_end: 2026-01-12
granularity: week
author: 04-weekly-review
base_commit: 0123456789abcdef0123456789abcdef01234567
created_at: 2026-01-12T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

示例周的上下文说明。这里只写整体背景，不按天罗列事件。

## 检索条目

### OBS-W-2026-01-05-01 示例周观测一
- claim_id: OBS-W-2026-01-05-01
- claim_type: observation
- period_start: 2026-01-05
- period_end: 2026-01-12
- granularity: week
- entities: 示例实体
- derived_from: OBS-D-2026-01-05-01
- derived_from: CHG-D-2026-01-06-01
- evidence: C-EXAMPLE-0001
- confidence: medium
- body: 示例周观测一，跨两个日期聚合，不重复单日事件。

### OBS-W-2026-01-05-02 示例周观测二
- claim_id: OBS-W-2026-01-05-02
- claim_type: observation
- period_start: 2026-01-05
- period_end: 2026-01-12
- granularity: week
- entities: 示例实体
- derived_from: OBS-D-2026-01-07-01
- derived_from: OBS-D-2026-01-09-01
- evidence: C-EXAMPLE-0002
- confidence: low
- body: 示例周观测二，跨两个日期对比，不重复单日事件。

### OBS-W-2026-01-05-03 示例周观测三
- claim_id: OBS-W-2026-01-05-03
- claim_type: observation
- period_start: 2026-01-05
- period_end: 2026-01-12
- granularity: week
- entities: 示例实体
- derived_from: FND-D-2026-01-08-01
- derived_from: OBS-D-2026-01-10-01
- evidence: BLK-EXAMPLE-0001
- confidence: low
- body: 示例周观测三，跨两个日期累积观察，不重复单日事件。

## 运维

- 命令: stratum status --date 2026-01-05
- 结果: 本周块覆盖完整
- 补数: 无
- 异常: 无
```

## 10. 幂等与失败

- 相同 `report_id`、相同内容重复 ingest 是 no-op。
- 报告所有 observation 仍为 `draft` 时，允许用同一 `report_id` 修订。缺失的 draft 标记为 `rejected`，原因 `REMOVED_FROM_REPORT`。
- 报告已有任一 observation 进入 `active` 或更后状态时，`report_id` 不可修订。修正必须使用新的 `report_id`，并在新 observation 上写 `supersedes`。
- `stratum ingest report` 失败时整包回滚。
- promotion 失败时保持 `draft`，不部分转正。
- 日层来源失效时，周观测自动变为 `stale`。

## 11. 验收

周报契约通过的条件：

- frontmatter 字段完整且精确。
- 三个章节齐全且顺序正确。
- observation 数量在 3 到 7 之间。
- 只包含 `observation`，不包含 `pattern` 或 `hypothesis`。
- 每条 observation 至少两条 `derived_from`，且来自至少两个不同日期。
- 每条 observation 至少一条原始 evidence。
- 周报重复事件流水数量为 0。
- 所有 promotion 都有 author 和 reason。
- 没有直接写 SQLite。

## 12. 冻结与变更

- 本文件是 v1 契约，`status: frozen`。
- observation 数量、derived_from 规则、事件流水限制或 promotion 权限的变更必须升级到 v2。
- Loom `04-weekly-review` prompt 必须引用本文件和 `claim-schema.md`。
- 任何实现不得静默降低 3 到 7 条的数量要求或允许周报直接引用日层以外的事实。
