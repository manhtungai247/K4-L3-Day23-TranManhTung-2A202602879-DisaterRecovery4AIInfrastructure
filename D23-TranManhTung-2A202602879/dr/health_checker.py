"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """TODO: trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    if region not in URL:
        return False, f"unknown_region={region}"
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.TimeoutException:
        return False, "probe_timeout"
    except httpx.HTTPError as exc:
        return False, f"probe_error={type(exc).__name__}"
    if response.status_code != 200:
        return False, f"readyz_status={response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return False, "invalid_readyz_json"
    if not body.get("ready", True):
        return False, ";".join(body.get("reasons", [])) or "ready_false"
    return True, "readyz_status=200"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """TODO: vòng lặp poll + phát hiện transition + ghi JSONL."""
    if interval <= 0 or timeout <= 0 or threshold < 1 or duration < 0:
        raise ValueError("interval/timeout phải > 0, threshold >= 1, duration >= 0")

    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    states = {region: None for region in URL}
    failures = {region: 0 for region in URL}
    deadline = time.monotonic() + duration

    with out.open("a", encoding="utf-8") as log:
        while time.monotonic() < deadline:
            cycle_start = time.monotonic()
            for region in URL:
                if time.monotonic() >= deadline:
                    break
                ready, reason = probe(region, timeout)
                if ready:
                    failures[region] = 0
                    # Baseline HEALTHY is implicit; log subsequent recovery only.
                    if states[region] == "UNHEALTHY":
                        states[region] = "HEALTHY"
                        event = {
                            "ts": time.time(), "event": "state_change",
                            "region": region, "to": "HEALTHY", "reason": reason,
                            "interval_s": interval, "threshold": threshold,
                            "consecutive_fails": 0,
                        }
                        log.write(json.dumps(event) + "\n")
                        log.flush()
                else:
                    failures[region] += 1
                    if failures[region] >= threshold and states[region] != "UNHEALTHY":
                        states[region] = "UNHEALTHY"
                        event = {
                            "ts": time.time(), "event": "state_change",
                            "region": region, "to": "UNHEALTHY", "reason": reason,
                            "interval_s": interval, "threshold": threshold,
                            "consecutive_fails": failures[region],
                        }
                        log.write(json.dumps(event) + "\n")
                        log.flush()
            remaining = interval - (time.monotonic() - cycle_start)
            if remaining > 0:
                time.sleep(min(remaining, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
