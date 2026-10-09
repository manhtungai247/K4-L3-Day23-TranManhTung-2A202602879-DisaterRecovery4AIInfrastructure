"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
P95_LIMIT_MS = 500.0


def step(n, name, **kw):
    """TODO: ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
              "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
        stream.flush()
    print(json.dumps(record, ensure_ascii=False))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """TODO: auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    answer = input(f"{msg} [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """TODO: 7 bước ở trên."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary và target phải là hai region khác nhau: a/b")

    # 1. Confirm both the outage and the survivor over consecutive readiness probes.
    primary_probes, target_probes = [], []
    for _ in range(3):
        primary_probes.append(hc.probe(primary, timeout=1.0))
        try:
            response = httpx.get(f"{URL[target]}/healthz", timeout=1.0)
            target_probes.append((response.status_code == 200,
                                  f"healthz_status={response.status_code}"))
        except httpx.HTTPError as exc:
            target_probes.append((False, f"healthz_error={type(exc).__name__}"))
        time.sleep(0.1)
    outage_confirmed = all(not ready for ready, _ in primary_probes)
    target_available = any(ready for ready, _ in target_probes)
    step(1, "xac_nhan_outage", primary=primary, target=target,
         primary_probes=[{"ready": r, "reason": why} for r, why in primary_probes],
         target_probes=[{"ready": r, "reason": why} for r, why in target_probes],
         confirmed=outage_confirmed and target_available)
    if not outage_confirmed or not target_available:
        step(7, "post_incident", status="aborted", reason="outage_not_confirmed_or_target_unavailable")
        return {"ok": False, "reason": "outage_not_confirmed_or_target_unavailable"}

    chaos_path = pathlib.Path("chaos/chaos-events.jsonl")
    outage_ts = None
    if chaos_path.exists():
        for line in reversed(chaos_path.read_text(encoding="utf-8").splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("action") == "kill" and event.get("region") == primary:
                outage_ts = event.get("ts")
                break

    # 2. Start the incident clock only after operator confirmation.
    if not confirm(auto, f"Xác nhận failover từ Region {primary.upper()} sang {target.upper()}?"):
        step(2, "thong_bao_incident", status="cancelled", outage_ts=outage_ts)
        step(7, "post_incident", status="aborted", reason="operator_declined")
        return {"ok": False, "reason": "operator_declined"}
    incident_start = time.time()
    step(2, "thong_bao_incident", status="started", outage_ts=outage_ts,
         notification_delay_s=None if outage_ts is None else round(incident_start - outage_ts, 3),
         incident_start_ts=incident_start)

    # 3. The failover procedure owns all five substeps and must run exactly once.
    result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", status="ok" if result.get("ok") else "failed",
         failover_steps=result.get("steps", []), reason=result.get("reason"))

    # 4. Record the state returned by failover; do not restore a second time.
    replica = result.get("target_state") or {}
    state_ok = bool(result.get("ok") and replica.get("weights") and replica.get("count", 0) > 0)
    step(4, "verify_state_replica", status="ok" if state_ok else "failed",
         region=target, state=replica, rpo_seconds=result.get("rpo_seconds"),
         docs_lost=result.get("docs_lost"), embed_model_version=result.get("embed_model_version"))

    # 5. Record the cutover result returned by the single failover call.
    cutover_ok = bool(result.get("cutover") and result.get("active_region") == target)
    step(5, "dns_cutover", status="ok" if cutover_ok else "failed",
         active_region=result.get("active_region"), target=target)

    # 6. Send ten real inference requests directly to the promoted region.
    latencies, errors = [], []
    for index in range(10):
        started = time.perf_counter()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", params={"q": f"golden-signal-{index}"},
                                 timeout=5.0)
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)
            if response.status_code != 200 or response.json().get("region") != target:
                errors.append({"request": index, "status": response.status_code,
                               "body": response.text[:200]})
        except (httpx.HTTPError, ValueError) as exc:
            errors.append({"request": index, "error": type(exc).__name__})
        time.sleep(0.05)
    ordered = sorted(latencies)
    p95 = round(ordered[min(len(ordered) - 1, max(0, int(0.95 * len(ordered) + 0.999) - 1))], 1) if ordered else None
    error_rate = round(len(errors) / 10, 3)
    golden_ok = not errors and error_rate == 0 and p95 is not None and p95 <= P95_LIMIT_MS
    step(6, "verify_golden_signals", status="ok" if golden_ok else "failed",
         requests=10, successful=10 - len(errors), error_rate=error_rate,
         p95_latency_ms=p95, p95_limit_ms=P95_LIMIT_MS, errors=errors)

    # 7. Close the incident and leave a reproducible measurement command.
    elapsed = round(time.time() - incident_start, 3)
    success = bool(result.get("ok") and state_ok and cutover_ok and golden_ok)
    step(7, "post_incident", status="resolved" if success else "needs_attention",
         elapsed_s=elapsed, rpo_seconds=result.get("rpo_seconds"),
         docs_lost=result.get("docs_lost"), measure_command=
         "python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {**result, "ok": success, "incident_elapsed_s": elapsed,
            "golden_signals": {"requests": 10, "error_rate": error_rate,
                               "p95_latency_ms": p95}}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
