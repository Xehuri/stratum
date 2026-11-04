# Loom 批次数据契约
contract: v1
status: frozen
owner: Stratum
updated: 2026-11-04

本文件定义 Loom 批次（`01-data-check`）与 Stratum 的边界。批次只负责把当次原始记录写成 Markdown 源文件，并调用 `stratum ingest source` 建立块。批次不产生卡片、claim、observation、pattern 或 hypothesis。

## 1. 定位

批次是 source 层，不是 report 层。

```text
批次 Markdown -> stratum ingest source -> documents + blocks
```

批次输出的唯一结构化产物是文档与块。语义提取、建卡、claim、周报和双周报都由后续层完成。

## 2. 批次文件

### 2.1 文件要求

- 使用 UTF-8 编码。允许 LF 或 CRLF，Stratum 统一按 LF 解析。
- 一个批次一个文件，文件名由 Loom 现有目录约定决定。
- 文件路径示例：

```text
data/daily/2026-01-05/理解-2026-01-05-批次-01.md
```

- 批次文件可以被任意 Markdown 解析器读取，不要求固定标题结构。
- 推荐使用时间标题组织内容：

```text
## 09:15
## 12:40
## 21:05
```

这样 `time_heading` selector 可以稳定切块。没有时间标题时，Stratum 按 profile 回退到普通标题或整篇切块。

### 2.2 可选 frontmatter

批次文件可以没有 frontmatter。若写 frontmatter，只允许以下字段：

```yaml
---
doc_type: batch
doc_date: 2026-01-05
batch_id: batch-01
author: 01-data-check
---
```

| 字段 | 必需 | 取值 |
|---|---|---|
| `doc_type` | 是，若写 frontmatter | 固定为 `batch` |
| `doc_date` | 是，若写 frontmatter | 本地日期 `YYYY-MM-DD` |
| `batch_id` | 是，若写 frontmatter | 当日稳定编号，例如 `batch-01` |
| `author` | 是，若写 frontmatter | 固定为 `01-data-check` |

规则：

- frontmatter 只允许单行标量，不允许嵌套、列表、锚点或 flow 语法。
- 不得写 `report_contract`、`report_type`、`report_id`、`period_start`、`period_end`。
- 没有 frontmatter 时，Stratum 从路径和标题推断文档身份。推断结果必须稳定。

### 2.3 禁止的报告结构

批次文件不得包含：

- `## 检索条目`
- `## 运维`
- `claim_id`
- `claim_type`
- `derived_from`
- `counterexample_count`
- `support_count`
- `opportunity_count`

Stratum 不会把批次当作 report 解析。出现这些内容时，`stratum ingest source` 仍按普通文本入库，但契约检查报 `BATCH_REPORT_SECTION`。批次作者必须删除这些内容，而不是依赖后续层修正。

## 3. 只允许的写入

批次对 Stratum 的唯一写操作是：

```bash
stratum ingest source <file>
```

语义：

- 创建或更新 document。
- 创建、更新或匹配 block。
- 记录 source hash 与 snapshot hash。
- 重复执行是幂等的。

示例：

```bash
stratum ingest source data/daily/2026-01-05/理解-2026-01-05-批次-01.md
```

允许在一次批次中多次调用，每个文件一次。允许多个文件按顺序 ingest。

## 4. 禁止的写入

批次不得执行：

```text
stratum ingest report
stratum add card
stratum add claim
stratum review promote
stratum review reject
stratum review bulk-promote
stratum commit
stratum restore
```

批次不得：

- 直接写 SQLite 或任何 `.stratum/` 下的文件。
- 创建卡片、claim、observation、pattern 或 hypothesis。
- 把批次内容写成 `## 检索条目`。
- 为了让 ingest 通过而修改源文件之外的任何索引。
- 用批次文件名充当 claim_id 或 report_id。

## 5. 失败处理

批次 ingest 失败不阻塞批次。

失败定义：

- `stratum ingest source` 返回非零退出码。
- Stratum 不在 PATH。
- workspace 未初始化或锁定。
- 文件读取失败或编码错误。

处理顺序：

1. 记录命令、退出码和错误首行。
2. 可以重试一次。
3. 第二次仍失败时，记录失败并继续后续批次。
4. 不等待人工修复。
5. 不创建替代卡片或 claim。
6. 不跳过后续批次文件。

失败记录进入 Loom 运行日志，并由日复盘写入当日报告的 `## 运维`。失败本身不是 claim，不得写成 `claim_type`。

修复责任：

- 日复盘运行 `stratum status --date <date>` 检查块覆盖。
- 发现缺失块时，修正批次源文件。
- 重新执行 `stratum ingest source <file>`。
- 修复完成后，日复盘才写入依赖这些块的 claim。

## 6. 可选只读查询

批次可以在写入前查询已有 active 内容作为上下文。查询是只读的，不是必需步骤。

允许：

```bash
stratum query --type claim --from 2026-01-05 --to 2026-01-06 --status active
stratum query --type card --from 2026-01-05 --to 2026-01-06
```

规则：

- 查询不得写 Stratum。
- 查询失败不阻塞批次。
- 查询结果只用于理解上下文，不得直接复制成 claim。
- 不得用查询结果替代源记录。

## 7. 命令示例

完整批次收尾示例：

```bash
stratum ingest source data/daily/2026-01-05/理解-2026-01-05-批次-01.md
stratum ingest source data/daily/2026-01-05/理解-2026-01-05-批次-02.md
```

带上下文查询的示例：

```bash
stratum query --type claim --from 2026-01-05 --to 2026-01-06 --status active
stratum ingest source data/daily/2026-01-05/理解-2026-01-05-批次-01.md
```

失败记录示例，纯文本，不是 claim：

```text
批次: 2026-01-05 批次-01
命令: stratum ingest source data/daily/2026-01-05/理解-2026-01-05-批次-01.md
退出码: 2
错误: workspace not initialized
处理: 已记录，不阻塞后续批次
```

## 8. 幂等与修复

- 同一文件、同一内容重复 ingest 是 no-op，不产生重复块。
- 同一文件内容变化时，Stratum 保留稳定块身份，更新快照，并把依赖旧快照的卡片或 claim 标记为 `stale`。
- 文件重命名或移动时，由 Stratum 的块匹配规则处理，不由批次手工改块 ID。
- 批次不得删除已入库的块。删除由日复盘的 `stratum status` 和 `stratum fsck` 发现，再由人工或主会话决定。
- 批次失败后重新 ingest 同一文件是安全操作。

## 9. 验收

批次契约通过的条件：

- 每个批次文件都已执行 `stratum ingest source`。
- 试点日期块覆盖率 100%。
- 批次没有创建任何卡片、claim、observation、pattern 或 hypothesis。
- 批次 ingest 失败已记录，且没有阻塞后续批次。
- 没有直接写 SQLite。
- 没有把运维内容或叙事内容写成 claim。

## 10. 冻结与变更

- 本文件是 v1 契约，`status: frozen`。
- 批次边界、允许命令或失败语义的变更必须升级到 v2。
- Loom `01-data-check` prompt 必须引用本文件。
- 任何实现不得把批次扩展成 report 写入通道。
