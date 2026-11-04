# Stratum 设计（2026-11-04）

- 状态：设计草案
- 日期：2026-11-04
- 名称：Stratum（时间地层）
- 命令行命名空间：`stratum`
- 当前阶段：仅设计，不进入实现

---

## 1. 定位

Stratum 是 Loom 的时间数据面。

Loom 负责对话、解释、叙事和提出假设；Stratum 负责把长期 Markdown 记录变成可查询、可回滚、可审计的结构。

一句话：

> 把 300 天沉积成可查询、可回滚、可审计的地层。

它不是：

- Markdown 编辑器
- Git 的替代品
- 向量数据库
- 面向人的笔记 UI
- 只服务 Kinship 的私有索引脚本

Kinship 只是 Stratum 的第一个适配器。

---

## 2. 已拍板决策

以下决策不再重新讨论：

1. Stratum 同时包含索引层和观测层。
2. Stratum 支持多个 Markdown 知识库 workspace。
3. 代码仓库公开，`demo/` 只放合成数据。
4. History 是 Stratum 自身数据的溯源，不追踪 Markdown 源文件变更。
5. History 不实现 staging、branch、merge。
6. History 记录 author。
7. v1 只做 CLI，不做 HTTP/UI。
8. 数据库必须；SQLite 作为 v1 的索引与历史存储。
9. 块模型采用 selector + snapshot + anchor。
10. 切块引擎与当前 Kinship 数据格式解耦。
11. 任意 Markdown 可索引；不同质量的 Markdown 检索权重不同。
12. 旧 `B-/L-/R-` JSONL 索引必须可迁移，旧 ID 保留；迁移是一次性快照，观察期保留外部回退窗口。
13. 新增检索类型 `prose`（散文），用于没有稳定标题、claim、时间字段的无规律 Markdown。

---

## 3. 非目标

v1 明确不做：

- HTTP API
- Web UI
- 桌面 UI
- 多用户权限
- 云端同步
- 向量检索
- 图数据库
- Markdown 源文件版本控制
- 自动改写 Loom 报告
- 自动把叙事文本推断成高置信 claim
- 跨 workspace 联合查询

---

## 4. 核心判断：兼容性不等于可检索性

Stratum 必须支持任意 Markdown，但不能假装所有 Markdown 一样适合检索。

| 目标 | 含义 | 处理 |
|---|---|---|
| 兼容性 | 任意 Markdown 都能进入系统 | 支持 |
| 可检索性 | 能稳定、准确、可证据化地检索 | 需要契约 |

因此：

```text
任意 Markdown        -> 可以索引
契约化 Markdown      -> 可以高质量检索
叙事型 Markdown      -> 可以入库，但标记 legacy / 低权重
无规律 Markdown      -> 标记 prose，提供段落级全文检索
```

这是 Stratum 的第一原则。

---

## 5. Retrieval Readiness Contract

Loom 当前的复盘、周报、双周报主要是叙事文本，不是检索单元。  
直接索引会把低质量文本固化进索引，后续优化报告还要重建语义。

因此先定义产出契约，再做高质量索引。

### 5.1 契约等级

| 等级 | 名称 | 特征 | 检索质量 |
|---|---|---|---|
| L0 | Legacy narrative | 叙事标题、长段落、多 claim 混合 | 低 |
| L1 | Structured section | 固定章节、时间/实体/证据字段 | 中 |
| L2 | Atomic claim | 一个块一个 claim，带证据/置信度/反例 | 高 |
| L3 | Cross-scale claim | 跨时间尺度的趋势、状态、机制假设 | 高 |

### 5.2 L2 claim 最小字段

```text
claim_id
claim_type: observation | finding | pattern | change | hypothesis
period_start
period_end
granularity: instant | day | week | month | range
entities
evidence: card_id / block_id / snapshot_sha
confidence: high | medium | low
body
```

按 claim 类型可选：

```text
metrics
baseline
delta
support_count
opportunity_count
counterexample_cards
conditions
outcome
lag
supersedes
```

### 5.3 叙事与 claim 分离

报告可以保留叙事，但叙事不是主检索单元。

建议结构：

```text
## 叙事
给人读的上下文。

## 检索条目
### OBS-... 睡眠债累积
- claim_type: observation
- period: ...
- evidence: ...
- body: ...
```

Stratum 优先索引「检索条目」；叙事降权。

### 5.4 lint 是索引前置门

```bash
stratum lint --profile loom-weekly docs/reports/...
```

输出纯文本：

```text
contract: v1
level: 1
ready: false

issues:
  [WARN] section "睡眠模式深度分析" contains 3 claims
  [ERROR] claim OBS-... missing period_start
  [ERROR] claim OBS-... missing evidence
```

策略：

- `strict`：不通过不进入高质量索引
- `advisory`：警告并降级（v0.1.1 暂不存储，strict 为当前实现）
- `legacy`：不检查，低权重

### 5.5 契约归属

建议由 Stratum 定义数据契约，Loom 遵守：

```text
stratum/contracts/loom-daily.md
stratum/contracts/loom-weekly.md
stratum/contracts/loom-biweekly.md
```

契约带版本号。Loom prompt 引用契约，Stratum lint 验证契约。

### 5.6 检索类型

检索类型与契约等级是两个维度：

- **契约等级**描述“结构化程度”
- **检索类型**描述“这段文档应该按什么方式被检索”

| 检索类型 | 含义 | 默认切块 | 检索能力 |
|---|---|---|---|
| `structured` | 契约化观测/claim | 标题或 claim 段 | 字段过滤、证据链、关系、规律 |
| `prose` | 无规律 Markdown，散文、随笔、外部文章、随手笔记 | 段落；必要时整篇 | 全文检索、段落命中、上下文回看 |
| `legacy` | 已知旧版叙事报告 | 标题段；退化为整篇 | 低权重全文检索，不参与高置信规律 |

`prose` 不是错误类型，而是一等公民：

- 没有标题，仍然可以按空行切段落
- 没有 claim 字段，不做 claim 校验
- 不自动生成卡片、关系、规律
- 可通过手动选块升级为证据或 claim 候选
- 查询时返回段落和上下文，不返回伪 claim

检索示例：

```bash
stratum query --type prose --grep "潮间带"
stratum query --type prose --from 2026-01-01 --context 2
```

`prose` 的检索权重低于 `structured`，但不为零。

---

## 6. 架构

```text
Markdown 源文件（唯一真值）
        |
        v
Workspace / Document registry
        |
        v
Segmentation（selector + snapshot + anchor）
        |
        +--> blocks
        +--> claims / observations
        +--> metrics
        |
        v
SQLite（索引 + 观测 + 历史）
        |
        +--> CLI（v1）
        +--> Python API（内部）
        +--> HTTP/UI（后续）
```

分层：

| 层 | 职责 |
|---|---|
| `core` | 数据模型、时间、身份、校验 |
| `workspace` | 多知识库注册、发现、锁 |
| `segmentation` | selector、profile、anchor、snapshot |
| `index` | 文档、块、概念、卡片、关系、报告 |
| `observation` | 指标、观测、状态、转折点、规律 |
| `history` | 线性提交、差异、恢复、审计 |
| `cli` | 文本命令与渲染 |
| `adapters` | Kinship / Loom / 任意 Markdown |

约束：

- 核心不 import CLI。
- CLI 不直接写 SQLite。
- UI 后置，不进入核心。
- 源 Markdown 永远不被 Stratum 修改。

---

## 7. 多知识库 Workspace

### 7.1 模型

```text
全局 registry
  workspace_id
  name
  root
  db_path
  created_at
  last_opened_at
  schema_version
```

每个 workspace 独立：

```text
<root>/.stratum/
  workspace.toml
  stratum.db
  profiles/
  cache/
```

### 7.2 默认与覆盖

默认方案 A：状态放在知识库根：

```text
<root>/.stratum/stratum.db
```

支持方案 B：状态目录外置：

```bash
stratum init --root ~/notes --db-path ~/.stratum/notes.db
```

适用于：

- 知识库在 Git 仓库中
- 知识库在同步盘
- 多个知识库共享状态盘

### 7.3 发现顺序

```text
--workspace
> STRATUM_WORKSPACE
> 当前目录向上查找 .stratum/workspace.toml
> registry 默认 workspace
```

v1 一次命令只操作一个 workspace。  
跨 workspace 联合查询后置。

### 7.4 复制与移动

- 移动：`stratum workspace relocate`
- 复制：复制出的 workspace 视为新 workspace，重新生成 UUID，旧 UUID 记为 `copied_from`
- 同步盘：`.stratum/` 默认 gitignore；检测到 OneDrive/Dropbox 等路径时给 warning

---

## 8. 块模型

### 8.1 定义

> 块 = 文档中由某个 selector 选出的、带快照和锚点的一段内容。

不是固定的 `## HH:MM` 段落。

### 8.2 三件套

```text
selector   怎么选出这段
snapshot   选出时的文本快照
anchor     以后怎么重新找到这段
```

### 8.3 Selector 类型

| 类型 | 用途 |
|---|---|
| `heading` | 按 Markdown 标题 |
| `time_heading` | 按时间标题 |
| `paragraph` | 按空行切段落，供 `prose` 使用 |
| `separator` | 按分隔线 |
| `regex` | 按自定义正则 |
| `manual_range` | 手动行号 |
| `manual_anchor` | 手动起止文本 |
| `whole_document` | 整篇一个块 |
| `frontmatter` | YAML frontmatter |
| `fenced_code` | 代码块 |
| `table` | 表格 |
| `composite` | 组合规则 |

### 8.4 Markdown 解析边界

必须处理：

- ATX `#` / `##` / `###`
- Setext `===` / `---`
- fenced code 里的 `##` 不切
- frontmatter
- HTML 块
- 表格
- 嵌套列表
- CRLF
- UTF-8 BOM
- 中文
- 重复标题

未知格式 fallback：

```text
frontmatter.index_profile
> path glob profile
> generic-heading
> prose-paragraph
> whole-document
```

### 8.5 手动块

手动选行必须同时保存：

```text
line_start
line_end
start_anchor
end_anchor
snapshot_text
snapshot_sha256
source_sha256
```

规则：

- 行号是初始提示，不是身份
- 源文件变化时先重新解析锚点
- 锚点找不到标 `stale`，不硬套旧行号
- 允许重叠块
- 允许通过 `parent_block_id` 表达嵌套

### 8.6 Prose profile

`prose` 文档的默认策略：

```text
selector: paragraph
separator: 一个或多个空行
min_chars: 80
max_chars: 4000
fallback: whole_document
```

规则：

- 空行分段；超长段落按句子/字符上限再切
- 段落过短时与下一段合并，避免碎片
- 没有时间字段时不推断时间
- 没有 claim 字段时不生成 claim
- 块 `retrieval_type=prose`
- 查询返回段落和前后上下文，不伪装成事件卡

`prose` 块仍然具备：

```text
doc_id
block_id
snapshot_text
snapshot_sha256
anchor
line_start / line_end
```

所以后续可以手动把某个 prose 块提升为证据或 claim 候选。

---

## 9. 块身份与重切

### 9.1 身份

```text
doc_id = frontmatter.id > 规范化相对路径
anchor = 规范化标题 / 时间 / 起止文本
block_id = "BLK-" + sha256(doc_id + profile_id + selector_type + anchor + occurrence)[:16]
snapshot_sha256 = sha256(snapshot_text)
```

行号不是身份。  
`occurrence` 只用于同文档同锚点重复。

旧 ID：

```text
legacy_block_id = B-/L-/R-...
```

永久保留，不复用。

### 9.2 重切匹配

```text
相同锚点 + 相同快照 -> 保留
相同锚点 + 不同快照 -> 保留 ID，更新快照，引用标 stale
不同锚点 + 相同快照 -> 生成迁移 old -> new
拆分/合并 -> 生成多条迁移
找不到 -> 旧块标 missing，不删除
```

禁止：

> 旧 block_id 还在，但已经指向另一个段。

### 9.3 迁移表

```text
block_migrations
  old_block_id
  new_block_id
  reason: split | merge | rename | move | profile_change
  method: anchor | snapshot | manual
  confidence
  created_at
```

---

## 10. SQLite 作为 v1 主存储

### 10.1 原则

- Markdown 是唯一真值。
- SQLite 是索引、观测、历史的强制存储。
- JSONL 只作为导入/导出格式，不再作为运行时主存储。
- SQLite 文件可重建；History 和人工标注不可丢失。

### 10.2 表组

| 组 | 表 | 作用 |
|---|---|---|
| 元数据 | `schema_migrations`, `workspace_meta`, `settings`, `profiles`, `segmentation_runs` | 版本、配置、可复现切块 |
| 内容 | `blobs`, `documents`, `document_versions`, `blocks`, `block_lineage` | 源锚点、快照、证据 |
| 索引 | `concepts`, `cards`, `card_concepts`, `card_blocks`, `card_relations`, `reports`, `report_blocks`, `report_cards`, `report_relations` | 事件/观察/发现/决定目录 |
| 观测 | `series`, `metric_points`, `observations`, `states`, `state_transitions`, `change_points`, `patterns`, `pattern_instances`, `evidence_links` | 跨时间尺度结构 |
| 历史 | `commits`, `changes`, `entity_versions`, `workspace_state` | 线性溯源 |
| 运维 | `import_runs`, `legacy_id_map`, `fsck_runs`, `fsck_findings` | 迁移、诊断 |
| 搜索 | `blocks_fts`, `cards_fts`, `observations_fts` | FTS5 派生索引 |

不使用 JSON1 作为强依赖；JSON 以 TEXT 存储并由应用校验。

### 10.3 PRAGMA

```text
foreign_keys = ON
journal_mode = WAL（本地）
synchronous = NORMAL
busy_timeout = 5000
```

同步盘/网络盘：

- WAL 不保证安全
- 提供 `journal_mode=DELETE`
- 建议数据库放本地，不同步

### 10.4 时间语义

- 存储 UTC 时间戳
- 同时存 IANA timezone
- 同时存本地日期键
- 区间使用半开区间 `[start, end)`
- 粒度：instant / day / week / month / range
- 缺失数据显式记录，不用 0 代替

---

## 11. 索引层

保留现有概念：

- `concepts`：分类法，只有叶子可贴卡
- `cards`：event / observation / finding / state / decision
- `card_concepts`：卡片到概念
- `card_blocks`：卡片到证据，带 `quote_sha`
- `card_relations`：卡片关系
- `reports`：日报/周报/双周报/全程总结
- `report_relations`：报告到卡片/报告

新增：

- `documents`：源文档，含 `retrieval_type`（`structured` / `prose` / `legacy`）
- `document_versions`：文档版本元数据
- `blocks`：通用块，含 `retrieval_type` 与 `profile_id`
- `block_lineage`：块拆分/合并/重命名
- `legacy_id_map`：旧 ID 映射

关系仍需补：

```text
claim_type: temporal | causal | associative
source: auto | manual | agent
confidence
weight
evidence_links
valid_from
valid_to
```

---

## 12. 观测层

### 12.1 对象

```text
series          指标定义
metric_points   指标点
observations    周期观测
states          持续状态
state_transitions 状态变化
change_points   转折点
patterns        规律
pattern_instances 规律实例
evidence_links  证据链接
```

### 12.2 指标

```text
metric_id
name
unit
value_type: int | float | duration | money | count
entity_type
aggregation: sum | avg | min | max | last
direction: higher_better | lower_better | neutral
```

### 12.3 观测

```text
observation_id
metric_id
period_start / period_end
value
baseline
delta
sample_size
confidence
evidence_links
```

### 12.4 状态

```text
state_id
state_type
start / end
status
severity
evidence_links
```

### 12.5 转折点

```text
change_id
metric_id
effective_at
before / after
effect_size
confidence
method
evidence_links
```

### 12.6 规律

```text
pattern_id
claim_type: association | causal | temporal | cycle | threshold
conditions
outcome
support_count
opportunity_count
counterexample_count
counterexample_links
period_start / period_end
confidence
status: candidate | active | deprecated
last_verified
evidence_links
```

### 12.7 检测器

v1 优先确定性方法：

- 移动平均 / 基线
- 变化率
- 简单转折点
- 频次 / 支持率
- 滞后相关
- 反例统计

Agent/LLM 产出只能进入 `draft`，人工或规则审核后 `active`。

---

## 13. History

### 13.1 定位

History 记录 Stratum 自身数据的变更：

- 哪个文档被索引
- 哪些块/卡片/观测被创建或修改
- 谁做的
- 为什么

不记录 Markdown 源文件版本。

### 13.2 模型

```text
commits
  id
  seq
  parent_id
  author
  message
  created_at
  change_count
  schema_version

changes
  id
  commit_id NULL   -- NULL 表示 pending
  entity_type
  entity_id
  op
  before_version_id
  after_version_id
  before_json
  after_json
  created_at

entity_versions
  id
  entity_type
  entity_id
  version
  commit_id
  blob_id
  data_json
  created_at

workspace_state
  key
  value
```

### 13.3 工作流

```text
写入 -> 自动记录 pending change
status -> 查看 pending / head / 异常
commit -> 把 pending 封成一个 commit
log -> 查看线性历史
diff -> 比较 pending 与 head，或两个 commit
restore -> 用旧版本生成新的补偿 commit
fsck -> 校验引用、版本、链
```

没有：

```text
staging area
branch
merge
rebase
checkout source files
```

### 13.4 恢复

`stratum restore <commit>`：

- 只恢复 Stratum 管理的索引/观测数据
- 不修改 Markdown
- 生成新的 commit
- 不重写历史

---

## 14. CLI

### 14.1 命令

```text
stratum init
stratum workspace add/list/remove/relocate
stratum ingest
stratum lint
stratum status
stratum query
stratum commit
stratum log
stratum diff
stratum restore
stratum fsck
stratum migrate-legacy
stratum demo
```

### 14.2 输出

v1 只输出纯文本。

- 不使用 JSON 作为 CLI 主输出
- 表格、树、缩进、短格式
- `--short` 可选
- 错误信息给人读

内部 Python API 返回类型化对象，后续 UI 使用。

`ingest` 与 `query` 支持显式检索类型：

```bash
stratum ingest --type prose notes/
stratum query --type prose --grep "潮间带"
stratum query --type structured --from 2026-01-01
```

未显式指定时：

- 有契约/claim 字段 -> `structured`
- 无标题、无 claim、但可分段 -> `prose`
- 已知旧 Loom 报告 -> `legacy`
- 无法判断 -> `prose`，并记录 `TYPE_INFERRED`

### 14.3 退出码

```text
0  成功
1  业务失败（lint 不通过、fsck 发现错误）
2  参数/环境错误
3  数据不完整或需要人工处理
```

---

## 15. status 与异常

`stratum status` 输出：

```text
Stratum status
Workspace: personal
HEAD: 7f3a9c2  2026-11-04 15:30:00
Author: Axeuh
Message: index: 2026-11-03

Working state:
  M  data/daily/2026-11-03/理解-2026-11-03-批次-16.md
  A  data/daily/2026-11-04/理解-2026-11-04-批次-06.md
  D  data/daily/2026-10-31/复盘-2026-10-31.md
  R  notes/a.md -> notes/b.md

Contract:
  structured: 46
  prose: 87
  legacy: 148
  level 1: 12
  level 2: 30
  level 3: 4

Index:
  blocks 7421 (+12 -3 ~2 stale)
  cards 429
  relations 148
  observations 37
  patterns 6

Anomalies:
  BLOCK_MISSING      BLK-... referenced by C-...
  BLOCK_STALE        BLK-... snapshot mismatch
  CARD_UNRESOLVED    C-... evidence missing
  RELATION_DANGLING  C-... --precedes--> C-...
  PATTERN_STALE      P-... evidence missing

Run `stratum fsck` for a deep check.
```

异常类型：

```text
DOC_ADDED / DOC_MODIFIED / DOC_DELETED / DOC_RENAMED
BLOCK_NEW / BLOCK_MISSING / BLOCK_STALE / BLOCK_ORPHAN
BLOCK_DUPLICATE_ANCHOR / BLOCK_AMBIGUOUS
CARD_UNRESOLVED / CARD_STALE
RELATION_DANGLING / RELATION_STALE
PATTERN_STALE / MIGRATION_PENDING
CONTRACT_INVALID / PROFILE_CHANGED
TYPE_INFERRED / TYPE_AMBIGUOUS
```

`status` 只读、快速。  
`fsck` 深度校验，`--fix` 只做安全修复。

---

## 16. 旧索引迁移

### 16.1 原则

- 旧 JSONL 不改写
- 旧 ID 原样保留
- 新 ID 与旧 ID 映射
- 一次性快照迁移，观察期保留外部回退窗口
- 迁移失败可回滚

### 16.2 阶段

```text
1. 导入 8 张表 + meta
2. 校验 ID、引用、哈希
3. 一次性快照：导入旧 JSONL 到 SQLite
4. 观察期：Stratum 不读旧 JSONL；旧索引只读冻结，外部流程仍可回退
5. 新写入只写 SQLite；14 天零 fallback 后退役旧读
```

### 16.3 映射

```text
B-/L-/R- block_id -> legacy_block_id
C- card_id        -> legacy_card_id
concept_id        -> 原样
report_id         -> 原样
```

必须保证：

- 卡片到块的引用不变
- 关系端点不变
- 报告关系不变
- 证据哈希不变

---

## 17. 跨平台

目标：

```text
Windows 10/11  第一优先
Linux          第一优先
macOS          best-effort
```

约束：

- Python 3.11+
- SQLite 使用 Python 标准库
- 不依赖 git 二进制
- 不依赖 symlink
- 不依赖大小写
- 不依赖文件系统顺序
- 路径存 POSIX `/`
- 读取 UTF-8，兼容 `utf-8-sig`
- 哈希基于 LF 归一化文本
- 对象/数据库路径短
- 原子替换
- 单写者文件锁
- 同步盘警告

---

## 18. 隐私

- 仓库不保存真实个人数据
- `demo/` 全部合成
- 数据库默认 0600
- 不联网
- 日志不打印敏感正文
- History 可能包含快照，默认不入公开仓库
- 后续可选加密；v1 不做

---

## 19. 性能目标

第一版目标：

```text
300 天，900 张卡，1.5 万块，几千条关系
status < 1 秒
单日增量 ingest < 2 秒
300 天全量重建 < 60 秒
单跳查询 < 100 毫秒
三跳路径查询 < 2 秒
```

最终数字待基准测试后修订。

---

## 20. Demo

```text
demo/
  README.md
  profiles/
    demo.toml
  workspaces/
    tidepool-lab/
      README.md
      journal/
        2026-01-05.md
        2026-01-12.md
        2026-01-19.md
    lantern-library/
      README.md
      reading-log.md
      notes/
        the-dispossessed.md
    prose-notes/
      README.md
      field-notes.md
  expected/
    README.md
```

要求：

- 全部合成数据
- 覆盖多 workspace
- 覆盖 heading / table / code fence / manual range
- 覆盖 legacy 叙事和 L2 claim
- 覆盖 `prose` 段落检索
- 覆盖历史、status、fsck
- 不提交生成的 `.stratum/` 和数据库

---

## 21. 待确定项

| ID | 事项 | 建议默认 | 是否阻塞 |
|---|---|---|---|
| O1 | 开源许可证 | AGPL-3.0-only，与 Kinship 一致 | 发布前 |
| O2 | 包名 | 先查 PyPI；发行名可用 `axeuh-stratum`，命令 `stratum` | 实现前 |
| O3 | Python 版本 | 3.11+ | 实现前 |
| O4 | 查询命令 | v1 必须有 `stratum query`，纯文本 | 实现前 |
| O5 | 隐私/加密 | v1 本地明文 + 0600；加密后置 | 真实数据前 |
| O6 | 迁移切换 | 一次性快照 + 外部回退窗口 14 天，零 fallback 后切换 | 迁移前 |
| O7 | 性能数字 | 采用本文目标，基准后修订 | 实现前 |
| O8 | 锚点歧义策略 | 歧义标 broken，不静默重绑 | 块模块前 |
| O9 | 时间语义 | UTC + timezone + 本地日期 + 半开区间 | 观测模块前 |
| O10 | 观测检测器 | v1 确定性；agent 草案需审核 | 观测模块前 |
| O11 | 跨 workspace 查询 | v1 不支持 | CLI 前 |
| O12 | 配置格式 | TOML，标准库解析 | 实现前 |
| O13 | 新 ID 方案 | `BLK-<hash>`，旧 ID 保留 | schema 前 |
| O14 | 数据库位置 | `.stratum/stratum.db`，可覆盖 | init 前 |
| O15 | 文档快照 | 块快照必须；整文档版本可选 | schema 前 |
| O16 | Loom 读取方式 | v1 由 CLI 或 Python API 调用，不做 HTTP | 集成前 |
| O17 | 跨平台 CI | Windows + Linux | 发布前 |
| O18 | schema 迁移 | 只前进，升级前备份 | schema 前 |
| O19 | demo 许可/语言 | 英文合成语料 + 一份中文 fixture | 发布前 |
| O20 | 自定义正则安全 | 限制长度，防 ReDoS | 规则模块前 |
| O21 | 契约所有者 | Stratum 定义，Loom 遵守 | 契约前 |
| O22 | 旧报告处理 | `contract_version=0`，低权重，不重写 | 迁移前 |
| O23 | 契约 lint 策略 | 默认 advisory，严格模式可选 | 实现前 |
| O24 | 仓库远端 | 未创建；先本地，确认后建公开仓库 | 发布前 |
| O25 | `prose` 自动判定 | 无标题/无 claim 且有空行 -> prose；否则 structured | 实现前 |
| O26 | `prose` 段落参数 | 最小 80 字，最大 4000 字，超长按句子再切 | 实现前 |
| O27 | `prose` 检索排序 | 词面命中 + 段落长度 + 文档时间；低于 structured | 实现前 |

---

## 22. 验收标准

设计阶段：

- 所有已拍板决策映射到本文章节
- 所有待确定项有默认值
- 任意 Markdown 路径、手动块、profile、fallback 均有定义
- `structured` / `prose` / `legacy` 三种检索类型均有定义
- History 明确无 staging/branch/merge
- 旧索引迁移有一次性快照、切换、回滚
- demo 无真实数据
- 跨平台约束明确

实现阶段：

- `init/ingest/lint/status/query/commit/log/diff/restore/fsck` 可在 demo 运行
- Windows/Linux 通过
- 旧索引迁移 100% ID 对齐
- fsck 能发现所有种子异常
- 无网络调用
- 无真实数据进入公开仓库
- 性能目标达标或经用户确认修订
