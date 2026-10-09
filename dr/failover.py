"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """TODO: append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
        stream.flush()
    print(json.dumps(record, ensure_ascii=False))
    return record


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=3.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """TODO: 5 bước ở trên, đúng thứ tự."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if wait < 0:
        raise ValueError("wait phải >= 0")

    completed = []
    try:
        initial = state_of(target)
    except (httpx.HTTPError, ValueError) as exc:
        emit(step="1_verify_target", target=target, status="failed", reason=str(exc))
        return {"ok": False, "steps": completed, "reason": f"target_unavailable: {exc}"}
    emit(step="1_verify_target", target=target, status="ok", target_state=initial)
    completed.append("1_verify_target")

    restore_started_ts = time.time()
    restore_started = time.monotonic()
    try:
        meta = snapshot.get(target, backend)
        rpo = snapshot.rpo(pathlib.Path("state/region-a/vectors.sqlite"),
                           pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    except Exception as exc:
        emit(step="2_restore_snapshot", target=target, status="failed", reason=str(exc))
        return {"ok": False, "steps": completed, "reason": f"snapshot_restore_failed: {exc}"}
    details = {
        "rpo_seconds": rpo.get("rpo_seconds"),
        "docs_lost": rpo.get("docs_lost"),
        "embed_model_version": meta.get("embed_model_version"),
        "snapshot_at": meta.get("snapshot_at"),
        "restored_at": meta.get("restored_at"),
        "latest_doc_ts": meta.get("latest_doc_ts"),
        "restore_started_ts": restore_started_ts,
        "restore_duration_s": round(time.monotonic() - restore_started, 3),
    }
    emit(step="2_restore_snapshot", target=target, status="ok", **details)
    completed.append("2_restore_snapshot")

    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    previous_pool = pool_file.read_text(encoding="utf-8").strip() if pool_file.exists() else "cold"
    pool_file.write_text("full\n", encoding="utf-8")
    emit(step="3_scale_pool", target=target, status="ok", from_state=previous_pool, to_state="full")
    completed.append("3_scale_pool")

    ready_deadline = time.monotonic() + wait
    last_reason = "readiness_timeout"
    while True:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, max(0.1, wait)))
            if response.status_code == 200:
                body = response.json()
                if body.get("ready", True):
                    waited = round(wait - max(0.0, ready_deadline - time.monotonic()), 3)
                    emit(step="4_wait_ready", target=target, status="ok", waited_s=waited,
                         ready_state=body)
                    completed.append("4_wait_ready")
                    break
                last_reason = ";".join(body.get("reasons", [])) or "ready_false"
            else:
                last_reason = f"readyz_status={response.status_code}"
        except httpx.HTTPError as exc:
            last_reason = f"readyz_error={type(exc).__name__}"
        if time.monotonic() >= ready_deadline:
            elapsed = round(wait - max(0.0, ready_deadline - time.monotonic()), 3)
            emit(step="4_wait_ready", target=target, status="timeout", waited_s=elapsed,
                 reason=last_reason)
            return {"ok": False, "steps": completed + ["4_wait_ready"],
                    "reason": last_reason, **details, "target_state": initial,
                    "cutover": False}
        time.sleep(min(0.25, max(0.0, ready_deadline - time.monotonic())))

    active_file = pathlib.Path("edge/active_region")
    active_file.parent.mkdir(parents=True, exist_ok=True)
    active_file.write_text(target, encoding="utf-8")
    emit(step="5_dns_cutover", target=target, status="ok", active_region=target)
    completed.append("5_dns_cutover")
    final_state = state_of(target)
    return {"ok": True, "steps": completed, **details, "target_state": final_state,
            "cutover": True, "active_region": target}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
