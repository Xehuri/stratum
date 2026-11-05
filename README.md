# Stratum

Stratum（时间地层）是一个独立的 Markdown 观测与索引系统。

它的目标不是保存更多文本，而是把长期 Markdown 记录沉积成可查询、可回滚、可审计的结构：

- 文档、块、卡片、关系
- 指标、观测、状态、转折点、规律
- 多知识库 workspace
- 系统自身的轻量历史与异常状态

## 状态

v0.1 已实现：CLI、SQLite schema、Markdown 切块、块身份、prose、ingest、claims、review、query、history、fsck、legacy importer、demo 与 `lint`。`lint` 在 v0.1.1 实现 strict 模式。

设计文档：

- [Stratum 设计（2026-11-04）](docs/design/stratum-design-2026-11-04.md)

## 快速开始

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
stratum demo
```

## 命令

```text
stratum init
stratum workspace add/list/remove/relocate
stratum ingest source
stratum ingest report
stratum add card
stratum add claim
stratum add relation
stratum review list/show/promote/reject/reopen/bulk-promote
stratum query
stratum query --follow --sort relevance
stratum status
stratum commit/log/diff/restore
stratum fsck
stratum migrate-legacy
stratum demo
```

## 边界

- Markdown 源文件是唯一真值。
- Stratum 不修改源 Markdown。
- 本仓库不保存真实个人数据。
- `src/stratum/demo_data/` 随包发布，只包含合成数据；`stratum demo` 可在任意工作目录运行。

## 许可

AGPL-3.0-only，见 [LICENSE](LICENSE)。
