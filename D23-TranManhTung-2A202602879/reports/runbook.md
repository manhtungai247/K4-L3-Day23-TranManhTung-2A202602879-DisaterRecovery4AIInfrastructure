# Runbook — Region A outage

**Scope:** local Lab 23, filesystem snapshots, Region A primary and Region B standby. Keep the serving processes and edge running. The automated runbook prompts for operator confirmation unless `--auto` is supplied for a graded drill.

| # | Step | Copy-paste command / action | Success signal | Owner |
|---|---|---|---|---|
| 1 | Confirm outage and survivor | `python3 dr/health_checker.py --interval 5 --threshold 3 --duration 20 --out reports/health-events.jsonl` (in a separate terminal, inspect `python3 chaos/kill_region.py status`) | Region A fails readiness in three consecutive probes; Region B process responds to `/healthz`. Do not declare a double-region outage. | On-call |
| 2 | Declare incident and start RTO clock | `python3 dr/runbook.py --primary a --target b --backend fs` then answer `y` at the confirmation prompt | `reports/runbook-run.jsonl` records `thong_bao_incident` with an incident timestamp after the outage timestamp. | Incident commander |
| 3 | Restore state and scale the standby pool | Automated by the single `failover.failover()` call in step 2; verify with `curl http://127.0.0.1:8002/v1/state` | Failover log records `2_restore_snapshot` with RPO, documents lost and embedding model version, followed by `3_scale_pool`. | DR operator |
| 4 | Wait for standby readiness | `curl -i http://127.0.0.1:8002/readyz` | HTTP 200 and `ready: true`; failover logs `4_wait_ready`. A timeout aborts the cutover. | DR operator |
| 5 | Cut traffic over to Region B | Automated only after step 4; verify with `curl http://127.0.0.1:8080/edge/state` | `active_region` is `b`; failover log has `5_dns_cutover`. | Incident commander |
| 6 | Check golden signals | `python3 -c 'import httpx; print([httpx.get("http://127.0.0.1:8002/v1/infer", timeout=5).status_code for _ in range(10)])'` | Runbook log records 10 successful requests, error rate 0%, and p95 latency below 500 ms. Investigate any non-200 response before declaring recovery. | Service owner |
| 7 | Measure RTO/RPO and write postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` is `PASS`, `valid` is true, and the evidence/postmortem reports cite real `path:line` entries. | Incident commander |

## Rollback to Region A

Do not automatically fail back. The incident commander may return traffic to A only after A has been restored, its `/readyz` returns HTTP 200 for three consecutive checks, its vector count and model version are verified, and a canary request succeeds. If B is serving successfully, keep traffic on B while A is repaired. To roll back, get incident-commander approval, write `a` to `edge/active_region` without a BOM (`printf a > edge/active_region`), wait at least the configured edge TTL, and verify inference reports `edge_region: a`. If A fails readiness or the canary fails, stop rollback and keep traffic on B. The incident commander owns the decision.
