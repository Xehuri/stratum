---
report_contract: v1
report_type: daily
report_id: RPT-D-2026-01-05
period_start: 2026-01-05
period_end: 2026-01-06
granularity: day
author: 02-daily-review
base_commit: none
created_at: 2026-01-06T00:30:00+08:00
timezone: Asia/Shanghai
---

## 叙事

这是一个合成的日复盘叙事段落，仅用于测试。

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
- body: 示例观测内容，仅用于测试格式。

### CHG-D-2026-01-05-01 示例变化
- claim_id: CHG-D-2026-01-05-01
- claim_type: change
- period_start: 2026-01-05
- period_end: 2026-01-06
- granularity: day
- entities: 示例实体
- evidence: C-EXAMPLE-0001
- confidence: low
- baseline: 示例基线
- delta: 示例变化量
- body: 示例状态变化，仅用于测试格式。

## 运维

- 命令: stratum status
- 结果: 合成数据
