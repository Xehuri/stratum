# Loom 双周报数据契约
contract: v1
status: frozen
owner: Stratum
updated: 2026-11-04

本文件定义 Loom 双周报（`03-biweekly-review`）的输出格式。双周报负责把两周的周观测聚合成 3 到 7 条 pattern 或 hypothesis。双周报默认全部为 `draft`，`03-biweekly-review` 不得自行转正。

## 1. 定位

双周报位于周报之上：

```text
weekly observations -> biweekly patterns or hypotheses
```

双周报只写跨两周的规律和机制假设，不写事件流水，不写日层 claim，不写单周 observation。

报告路径由 workspace 配置决定。本文示例使用 `reports/biweekly/YYYY-MM-DD.md`。

## 2. frontmatter

### 2.1 精确格式

双周报必须以下列 frontmatter 开头：

```yaml
---
report_contract: v1
report_type: biweekly
report_id: RPT-B-2026-01-05
period_start: 2026-01-05
period_end: 2026-01-19
granularity: range
author: 03-biweekly-review
base_commit: CMT-0123456789abcdef
created_at: 2026-01-19T01:00:00+08:00
timezone: Asia/Shanghai
---
```

### 2.2 字段

| 字段 | 必需 | 取值与规则 |
|---|---|---|
| `report_contract` | 是 | 固定为 `v1` |
| `report_type` | 是 | 固定为 `biweekly` |
| `report_id` | 是 | `RPT-B-YYYY-MM-DD`，修正版加 `-rN` |
| `period_start` | 是 | 第一个周一，`YYYY-MM-DD` |
| `period_end` | 是 | `period_start` 加 14 天，`YYYY-MM-DD`，半开区间 |
| `granularity` | 是 | 固定为 `range` |
| `author` | 是 | 固定为 `03-biweekly-review` |
| `base_commit` | 是 | 生成报告时的 Stratum HEAD，格式为 `CMT-` + 16 位小写十六进制；兼容旧报告的 40 位小写十六进制 git commit id；没有 HEAD 时写 `none` |
| `created_at` | 是 | RFC 3339 时间戳，必须带时区偏移 |
| `timezone` | 是 | IANA 时区名 |

规则：

- frontmatter 只允许上述字段。
- 只允许单行标量，不允许嵌套、列表、锚点或 flow 语法。
- `period_start` 必须是周一。
- `period_end` 必须等于 `period_start` 加 14 天。
- `report_id` 中的日期必须等于 `period_start`。

## 3. 正文结构

正文必须且只能包含以下三个二级章节，顺序固定：

```text
## 叙事
## 检索条目
## 运维
```

规则与日复盘、周报一致：

- 三个章节各出现一次。
- 不得增加其他二级章节。
- 三级标题只允许出现在 `## 检索条目` 下。
- `## 叙事` 和 `## 运维` 不参与 claim 解析。

## 4. `## 叙事`

用途：

- 给读者提供这两周的整体背景。
- 可以写机制线索、观察角度和阅读提示。

限制：

- 不得写 claim 字段。
- 不得把叙事当作证据。
- 不得按周或按天罗列事件。
- 叙事不进入 pattern 层，只做低权重全文检索。

## 5. `## 检索条目`

### 5.1 claim 块格式

每条规律或假设是一个三级标题块：

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
- conditions: 示例条件成立
- outcome: 示例结果出现
- support_count: 2
- opportunity_count: 3
- counterexample_count: 1
- counterexample_cards: C-EXAMPLE-0002
- confidence: low
- body: 示例规律，仅用于说明格式。
```

规则：

- 三级标题必须以 `claim_id` 开头，后跟一个空格和短标题。
- 字段语法遵循 `claim-schema.md`。
- 每条 claim 的 `period_start`、`period_end`、`granularity` 必须与报告 frontmatter 一致。
- 报告中不得写 `- status:`。

### 5.2 数量和类型

- 双周报必须包含 3 到 7 条 claim，含 3 和 7。
- 少于 3 条时 lint 报 `CLAIM_COUNT_LOW`，报告不得 ingest。
- 多于 7 条时 lint 报 `CLAIM_COUNT_HIGH`，报告不得 ingest。
- 只允许 `claim_type: pattern` 或 `claim_type: hypothesis`。
- 不允许在双周报中写 `observation`、`change` 或 `finding`。
- pattern 的 `claim_id` 格式为 `PAT-B-YYYY-MM-DD-NN`。
- hypothesis 的 `claim_id` 格式为 `HYP-B-YYYY-MM-DD-NN`。

少于 3 条通常说明周观测不足。周观测不足时，回到周报和日复盘补齐，不得为了满足数量而把事件流水写成 pattern。

### 5.3 通用必需字段

每条 pattern 或 hypothesis 必须包含：

- `claim_id`
- `claim_type`
- `period_start`
- `period_end`
- `granularity`
- `entities`
- `derived_from`
- `evidence`
- `conditions`
- `outcome`
- `support_count`
- `opportunity_count`
- `counterexample_count`
- `confidence`
- `body`

可选字段：

- `lag`
- `metric`
- `baseline`
- `delta`
- `sample_size`

### 5.4 derived_from

双周报必须通过 `derived_from` 引用周观测。

规则：

- 至少两条 `derived_from`。
- 至少一条来自双周的第一个星期，至少一条来自第二个星期。
- 只能引用 `OBS-W-...`。
- 不得引用日层 claim。
- 不得引用双周报 claim。
- 引用的周观测必须落在本双周 `[period_start, period_end)` 内。
- 引用的周观测在双周报 ingest 时必须存在，且不是 `rejected` 或 `superseded`。
- promotion 时，所有 `derived_from` 目标必须是 `active`。
- 如果任一 `derived_from` 目标变为 `stale`，该 pattern 或 hypothesis 必须变为 `stale`。

### 5.5 support、opportunity 与 counterexample

三个计数必须真实反映两周内的证据。

| 字段 | 含义 | 校验 |
|---|---|---|
| `support_count` | 条件出现且结果出现的次数 | 非负整数，至少为 1 |
| `opportunity_count` | 条件出现的总次数 | 非负整数，且不小于 `support_count + counterexample_count` |
| `counterexample_count` | 条件出现但结果未出现的次数 | 非负整数 |

反例规则：

- `counterexample_count` 大于 0 时，必须至少写一条 `counterexample_cards`。
- `counterexample_count` 等于 0 时，必须写 `counterexample_note`，说明检查范围、机会数和未发现反例的原因。
- 没有反例分析的 pattern 或 hypothesis 不得 promotion。
- `counterexample_count` 不得大于 `opportunity_count`。
- `support_count` 不得大于 `opportunity_count`。

### 5.6 置信度

- `pattern` 可以使用 `low`、`medium` 或 `high`。
- `pattern` 使用 `high` 时，`support_count` 必须至少为 3，且必须完成反例检查。
- `hypothesis` 只能使用 `low` 或 `medium`。
- `hypothesis` 不得使用 `high`。

置信度不是转正依据。任何 pattern 或 hypothesis 入库时都是 `draft`。

## 6. 不重复事件流水

双周报描述机制和规律，不是两周事件总结。

必须满足：

- 每条 pattern 或 hypothesis 至少聚合两个不同星期的周观测。
- 不得按周或按天罗列事件。
- 不得把周观测 body 原样复制到双周报。
- 不得把事件卡片标题或事件清单搬进双周报。
- 如果某个结论只适用于一周，它应留在周报，不应升级为双周 pattern。

允许的写法：

```text
- body: 两周内多个机会出现同类条件与结果组合，支持稳定关联。
```

不允许的写法：

```text
- body: 第一周发生事件甲，第二周发生事件乙。
```

## 7. 默认 draft 与转正权限

### 7.1 默认 draft

- 所有 pattern 和 hypothesis 在 ingest 后都是 `draft`。
- 报告作者不得写 `status: active`。
- `03-biweekly-review` 可以在报告中提出规律和假设，但不能决定它们是否有效。

### 7.2 03 不得自审自转

`03-biweekly-review` 不得执行：

```text
stratum review promote
stratum review bulk-promote
stratum review reject
stratum review reopen
```

`03-biweekly-review` 不得：

- 把自己的 author 写成审批人。
- 用 `--author 03-biweekly-review` promotion 自己产出的 claim。
- 绕过 review 直接写 SQLite。
- 通过修改报告内容把 draft 变成 active。

违反时 Stratum 拒绝，报 `SELF_PROMOTION_FORBIDDEN`。

### 7.3 只允许 human 或 main 批准

pattern 和 hypothesis 只能由人类或主会话批准。

人类批准：

```bash
stratum review promote PAT-B-2026-01-05-01 --author human --reason "counterexamples reviewed; two-week support confirmed"
```

主会话批准：

```bash
stratum review promote HYP-B-2026-01-05-01 --author main --reason "hypothesis reviewed; keep active as low confidence"
```

规则：

- `--author` 只能是 `human` 或 `main`。
- 必须提供 `--reason`。
- 批准前必须检查 `support_count`、`opportunity_count`、`counterexample_count` 和所有 `derived_from`。
- 所有 `derived_from` 周观测必须是 `active`。
- 所有 evidence 必须可解析。
- 已经 active 的 claim 是 no-op。
- `bulk-promote` 不得用于 pattern 或 hypothesis。
- 主会话只做审批，不写双周报正文。

## 8. 工作流

### 8.1 查询周观测

```bash
stratum query --type claim --claim-type observation --from 2026-01-05 --to 2026-01-19 --status active
```

要求：

- 只读取 active 周观测。
- 两个星期都应有足够的周观测。
- 如果周观测不足，回到周报和日复盘补齐。
- 不得用 draft 周观测写双周报。draft 可以在 ingest 时存在，但审批前必须全部 active。

### 8.2 写双周报

按第 2 到第 7 节写报告。

### 8.3 ingest

```bash
stratum ingest report reports/biweekly/2026-01-05.md
```

要求：

- ingest 是事务性的。任一 claim 失败时整包回滚。
- ingest 成功后所有 pattern 和 hypothesis 为 `draft`。
- 不得直接写 SQLite。

### 8.4 人工或主会话审批

由 human 或 main 执行 promotion。`03-biweekly-review` 不参与审批。

### 8.5 验证

```bash
stratum query --type claim --claim-type pattern --from 2026-01-05 --to 2026-01-19 --status active
stratum query --type claim --claim-type hypothesis --from 2026-01-05 --to 2026-01-19 --status active
```

确认：

- 3 到 7 条 claim。
- 每条都有至少两条来自两个星期的 `derived_from`。
- 每条都有 support、opportunity、counterexample 计数。
- 没有未处理的反例缺口。
- 没有事件流水复述。

## 9. 完整示例

以下内容全部为合成数据，只用于说明格式。

```text
---
report_contract: v1
report_type: biweekly
report_id: RPT-B-2026-01-05
period_start: 2026-01-05
period_end: 2026-01-19
granularity: range
author: 03-biweekly-review
base_commit: CMT-0123456789abcdef
created_at: 2026-01-19T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

示例双周的整体背景。这里只写机制线索，不按周罗列事件。

## 检索条目

### PAT-B-2026-01-05-01 示例规律一
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
- body: 示例规律一，仅用于说明格式。

### PAT-B-2026-01-05-02 示例规律二
- claim_id: PAT-B-2026-01-05-02
- claim_type: pattern
- period_start: 2026-01-05
- period_end: 2026-01-19
- granularity: range
- entities: 示例实体
- derived_from: OBS-W-2026-01-05-02
- derived_from: OBS-W-2026-01-12-02
- evidence: C-EXAMPLE-0004
- conditions: 示例条件成立
- outcome: 示例结果出现
- support_count: 2
- opportunity_count: 2
- counterexample_count: 0
- counterexample_note: 已检查两周内全部周观测，未发现反例。
- confidence: low
- body: 示例规律二，仅用于说明格式。

### HYP-B-2026-01-05-01 示例假设
- claim_id: HYP-B-2026-01-05-01
- claim_type: hypothesis
- period_start: 2026-01-05
- period_end: 2026-01-19
- granularity: range
- entities: 示例实体
- derived_from: OBS-W-2026-01-05-03
- derived_from: OBS-W-2026-01-12-03
- evidence: C-EXAMPLE-0005
- conditions: 示例条件成立
- outcome: 示例结果可能出现
- support_count: 1
- opportunity_count: 2
- counterexample_count: 1
- counterexample_cards: C-EXAMPLE-0006
- confidence: low
- body: 示例假设，仅用于说明格式。

## 运维

- 命令: stratum status --date 2026-01-05
- 结果: 两周块覆盖完整
- 补数: 无
- 审批: 待 human 或 main 处理
```

## 10. 幂等与失败

- 相同 `report_id`、相同内容重复 ingest 是 no-op。
- 报告所有 claim 仍为 `draft` 时，允许用同一 `report_id` 修订。缺失的 draft 标记为 `rejected`，原因 `REMOVED_FROM_REPORT`。
- 报告已有任一 claim 进入 `active` 或更后状态时，`report_id` 不可修订。修正必须使用新的 `report_id`，并在新 claim 上写 `supersedes`。
- `stratum ingest report` 失败时整包回滚。
- promotion 失败时保持 `draft`，不部分转正。
- 周观测来源失效时，pattern 或 hypothesis 自动变为 `stale`。
- 自审自转一律拒绝，不因报告内容或 author 字段变化而放行。

## 11. 验收

双周报契约通过的条件：

- frontmatter 字段完整且精确。
- 三个章节齐全且顺序正确。
- claim 数量在 3 到 7 之间。
- 只包含 `pattern` 或 `hypothesis`。
- 每条 claim 至少两条 `derived_from`，且来自两个不同星期。
- 每条 claim 都有 support、opportunity、counterexample 计数。
- 反例为 0 时有 `counterexample_note`，反例大于 0 时有 `counterexample_cards`。
- 所有 claim 入库后都是 `draft`。
- `03-biweekly-review` 没有执行 promotion。
- 所有 promotion 的 author 是 `human` 或 `main`，且有 reason。
- 没有直接写 SQLite。

## 12. 冻结与变更

- 本文件是 v1 契约，`status: frozen`。
- claim 数量、反例规则、默认 draft 或审批权限的变更必须升级到 v2。
- Loom `03-biweekly-review` prompt 必须引用本文件和 `claim-schema.md`。
- 任何实现不得静默允许 `03-biweekly-review` 自行转正 pattern 或 hypothesis。
