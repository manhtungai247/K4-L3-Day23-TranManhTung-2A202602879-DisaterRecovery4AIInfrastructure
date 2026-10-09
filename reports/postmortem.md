# Postmortem — Lab 23 DR Drill

**Date:** 2026-10-09 · **Impact:** Region A requests failed until Region B was restored and cut over. This review focuses on system behavior and response timing, not individual mistakes.

## 1. Timeline

| Time (UTC) | Event | Evidence |
|---|---|---|
| 2026-10-09 02:51:00 | Region A network-blocked; outage clock starts | `chaos/chaos-events.jsonl:7` |
| 2026-10-09 02:51:01 | First user request fails | `reports/drill-2-withdr.jsonl:2` |
| 2026-10-09 02:51:17 | Health checker marks Region A unhealthy | `reports/health-events.jsonl:8` |
| 2026-10-09 02:51:22 | Incident/runbook confirmation recorded | `reports/runbook-run.jsonl:16` |
| 2026-10-09 02:51:28 | Region B ready and edge cutover recorded | `reports/failover-events.jsonl:39`, `reports/failover-events.jsonl:40` |
| 2026-10-09 02:51:31 | First successful inference from Region B; service recovered | `reports/drill-2-withdr.jsonl:17` |

## 2. RTO/RPO against objectives

- **RTO:** objective 300 s; measured 30.7 s; headroom 269.3 s. Result: PASS. Evidence: `reports/measure-drill-2.json:20`.
- **RPO:** objective 300 s; measured 16.0 s and 8 documents lost; headroom 284.0 s. Evidence: `reports/failover-events.jsonl:37`.
- **Largest RTO component:** 21.2 s from outage to snapshot-restore completion. It includes the 16.2 s health-check detection time plus runbook confirmation/dispatch. The configured theoretical detection floor is 15.0 s (`5 s × 3`). Evidence: `reports/health-events.jsonl:8`, `reports/runbook-run.jsonl:16`, `reports/failover-events.jsonl:37`.
- Snapshot copy itself took 0.094 s, GPU pool warm-up took 6.179 s, and edge cache added about 3.2 s after cutover. Evidence: `reports/failover-events.jsonl:37`, `reports/failover-events.jsonl:39`, `reports/failover-events.jsonl:40`, `reports/drill-2-withdr.jsonl:17`.

## 3. Root cause — five whys

1. **Why did users see errors?** The edge continued to route to Region A after its serving process stopped responding.
2. **Why did routing not change immediately?** The edge caches the active region and has no autonomous failover decision.
3. **Why was recovery delayed?** The health checker requires three consecutive readiness failures, then the incident runbook confirms the outage before restoring and cutting over.
4. **Why was the standby not immediately usable?** Region B began without the primary's vectors or model weights and required snapshot restore plus pool warm-up.
5. **Why was user recovery dependent on these steps?** The system had no continuously replicated, already-warm standby with an integrated alert/approval path. The drill demonstrates the current control path and its measured cost.

## 4. Action items

| # | Action | Owner | Deadline | Expected RTO/RPO effect |
|---|---|---|---|---|
| 1 | Connect the unhealthy transition to an on-call alert and a one-click runbook launch; retain operator approval before cutover. | Platform on-call | 2026-10-16 | Target to remove most of the measured ~4.9 s post-detection dispatch delay; validate in another drill. |
| 2 | Compare 10 s and 30 s snapshot intervals over five drills; record RPO distribution and storage/IO cost before changing the default. | Data platform owner | 2026-10-23 | A 10 s interval can reduce the replication-lag bound by up to about 20 s; measure rather than assume. |
| 3 | Add an alert for Region B remaining `UNHEALTHY` during normal standby, while distinguishing expected empty-state from service-process failure. | SRE | 2026-10-23 | Earlier detection of a degraded standby; prevents discovering the gap during a primary outage. |

## 5. Reflection

1. `interval × threshold = 5 s × 3 = 15 s`. This theoretical floor is **48.9%** of the 30.7 s measured RTO. The observed health transition was at +16.2 s. Evidence: `reports/health-events.jsonl:8`, `reports/measure-drill-2.json:20`.
2. Reducing the interval from 5 s to 1 s at threshold 3 lowers the theoretical floor from 15 s to 3 s, a maximum nominal saving of 12 s. The tradeoff is more probes and a higher chance that transient network jitter causes false unhealthy transitions; keep the consecutive-failure threshold and test for flapping.
3. `docs_lost: 8` means eight recently ingested documents were absent from the restored replica. Customers relying on those records could receive incomplete retrieval results until the data is re-ingested; it does not by itself mean eight customers were affected. Evidence: `reports/failover-events.jsonl:37`.
