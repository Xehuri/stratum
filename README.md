# Stratum

Stratum（时间地层）是一个独立的 Markdown 观测与索引系统。

它的目标不是保存更多文本，而是把长期 Markdown 记录沉积成可查询、可回滚、可审计的结构：

- 文档、块、卡片、关系
- 指标、观测、状态、转折点、规律
- 多知识库 workspace
- 系统自身的轻量历史与异常状态

## 状态

当前处于设计阶段，尚未开始实现。

设计文档：

- [Stratum 设计（2026-11-04）](docs/design/stratum-design-2026-11-04.md)

## 边界

- Markdown 源文件是唯一真值。
- Stratum 不修改源 Markdown。
- 本仓库不保存真实个人数据。
- `demo/` 只包含合成数据。

## 计划中的命令

```text
stratum init
stratum workspace
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
```
