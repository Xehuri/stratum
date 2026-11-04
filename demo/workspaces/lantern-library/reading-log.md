---
stratum_contract: 2
doc_type: claim_log
period_start: 2026-02-02
period_end: 2026-02-04
entities: [lantern-library]
---

# Lantern Library Reading Log

Synthetic reading log for the Stratum demo. The headings use the claim profile
so each entry becomes one atomic block.

## OBS-D-2026-02-02-01 Reading pace rose after shorter sessions

- claim_id: OBS-D-2026-02-02-01
- claim_type: observation
- period_start: 2026-02-02
- period_end: 2026-02-03
- granularity: day
- entities: lantern-library
- evidence: C-LANTERN-SYNTHETIC-0001
- confidence: medium
- body: The synthetic reader finished more pages after splitting reading time into two shorter sessions.

The table below is synthetic. It is included so the demo covers Markdown tables
inside a structured block.

| Session | Pages | Minutes |
| --- | ---: | ---: |
| Morning | 18 | 25 |
| Evening | 22 | 30 |

## OBS-D-2026-02-03-01 Evening sessions were quieter

- claim_id: OBS-D-2026-02-03-01
- claim_type: observation
- period_start: 2026-02-03
- period_end: 2026-02-04
- granularity: day
- entities: lantern-library
- evidence: C-LANTERN-SYNTHETIC-0002
- confidence: low
- body: The invented evening session had fewer interruptions than the invented morning session.
