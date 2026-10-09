# RTO/RPO Evidence — Lab 23

Measurements below come from the completed baseline and DR drills on 2026-10-09. All evidence references point to the generated logs from those runs.

## 1. Drill 1 — no DR

| Metric | Measured value | Method | Evidence |
|---|---:|---|---|
| Outage time | 2026-10-09T02:35:12 | Region A `netblock` kill | `chaos/chaos-events.jsonl:1` |
| First failed request | +2.0 s | First failed loadgen request after outage | `reports/drill-1-nodr.jsonl:46` |
| Recovery | none during drill | No successful request after failure | `reports/measure-drill-1.json:25` |
| RTO | NO_RECOVERY | No recovery timestamp exists | `reports/measure-drill-1.json:25` |

## 2. Drill 2 — DR enabled

| Milestone | Seconds after outage | Measurement | Evidence |
|---|---:|---|---|
| Outage (t0) | 0.0 s | Chaos kill timestamp | `chaos/chaos-events.jsonl:7` |
| First user error | +0.3 s | First failed request after t0 | `reports/drill-2-withdr.jsonl:2` |
| Health checker detects Region A | +16.2 s | `UNHEALTHY` transition | `reports/health-events.jsonl:8` |
| Snapshot restore completes | +21.2 s | Restore event with measured RPO | `reports/failover-events.jsonl:37` |
| Region B ready | +27.4 s | `waited_s: 6.179` | `reports/failover-events.jsonl:39` |
| DNS cutover | +27.4 s | Active region changes to B | `reports/failover-events.jsonl:40` |
| First successful request from B | +30.7 s | First successful loadgen request after failure | `reports/drill-2-withdr.jsonl:17` |

| Metric | Measured | Target | Result |
|---|---:|---:|---|
| Inference RTO | 30.7 s | 300 s | PASS — `reports/measure-drill-2.json:20` |
| Vector DB RPO | 16.0 s / 8 documents lost | 300 s | PASS — `reports/failover-events.jsonl:37` |

## 3. RTO breakdown

The health-check detection floor is `5 s × 3 = 15.0 s`. The measured response also includes operator/runbook confirmation after detection; the four measured components below sum to 30.7 seconds (rounding to one decimal place).

| Component | Seconds | Source | How to reduce it |
|---|---:|---|---|
| Health detection + runbook dispatch (15.0 s theoretical floor) | 21.2 s | Restore starts at `reports/failover-events.jsonl:37`; incident confirmation at `reports/runbook-run.jsonl:16`; detection at `reports/health-events.jsonl:8` | Reduce interval carefully or shorten dispatch while retaining confirmation and anti-flap controls. |
| Snapshot restore | 0.1 s | `restore_duration_s: 0.094` in `reports/failover-events.jsonl:37` | Reduce snapshot size or use a faster replicated store. |
| GPU pool warm-up | 6.2 s | `waited_s: 6.179` in `reports/failover-events.jsonl:39` | Keep a warm standby pool, with the associated compute cost. |
| DNS/LB cache after cutover | 3.2 s | First successful request at `reports/drill-2-withdr.jsonl:17` minus cutover at `reports/failover-events.jsonl:40` | Lower edge TTL after considering cache churn and request volume. |
| **Total measured RTO** | **30.7 s** | `reports/measure-drill-2.json:20` | |
