#!/usr/bin/env python3
import argparse
import random
import threading
import time
from collections import deque

import requests


def make_payload(path: str, batch: int = 1, prompt: str = "", max_tokens: int | None = None):
    # If hitting /generate, send prompt JSON
    if path.rstrip("/").endswith("/generate"):
        payload = {"prompt": prompt}
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        return payload

    # Default: /predict payload (iris-like 4 features)
    inputs = []
    for _ in range(batch):
        inputs.append([random.random() * 8, random.random() * 5, random.random() * 7, random.random() * 3])
    return {"inputs": inputs}


def worker(stop_evt: threading.Event, url: str, path: str, qps: float, batch: int, prompt: str, timeout_s: float,
           max_tokens: int | None, stats: dict, idx: int):
    sess = requests.Session()
    period = 1.0 / qps if qps > 0 else 0.0
    next_t = time.time()

    while not stop_evt.is_set():
        now = time.time()
        if period > 0 and now < next_t:
            time.sleep(min(0.01, next_t - now))
            continue
        if period > 0:
            next_t += period

        t0 = time.time()
        ok = False
        try:
            payload = make_payload(path, batch=batch, prompt=prompt, max_tokens=max_tokens)
            r = sess.post(url, json=payload, timeout=timeout_s)
            ok = (r.status_code == 200)
        except Exception:
            ok = False
        dt = (time.time() - t0) * 1000.0

        with stats["lock"]:
            stats["sent"] += 1
            if ok:
                stats["ok"] += 1
                stats["lat_ms"].append(dt)
            else:
                stats["err"] += 1


def percentile(values, p):
    if not values:
        return None
    xs = sorted(values)
    k = int((len(xs) - 1) * p)
    return xs[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--router", required=True, help="Router public base URL, e.g. http://137.194.194.21:9000")
    ap.add_argument("--path", default="/predict", help="Request path: /predict (default) or /generate")
    ap.add_argument("--prompt", default="Summarize and flag anomalies: Alice paid Bob 3200€ then cash withdrawal same day.",
                    help="Prompt used when --path=/generate")
    ap.add_argument("--max-tokens", type=int, default=None, help="Optional max_tokens for /generate")
    ap.add_argument("--duration", type=int, default=30, help="Seconds")
    ap.add_argument("--concurrency", type=int, default=4, help="Number of worker threads")
    ap.add_argument("--qps", type=float, default=2.0, help="Per-worker QPS (approx)")
    ap.add_argument("--batch", type=int, default=1, help="Batch size per request (only used for /predict)")
    ap.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout (seconds)")
    args = ap.parse_args()

    path = args.path if args.path.startswith("/") else ("/" + args.path)
    url = args.router.rstrip("/") + path

    stats = {
        "lock": threading.Lock(),
        "sent": 0,
        "ok": 0,
        "err": 0,
        "lat_ms": deque(maxlen=5000),
    }

    stop_evt = threading.Event()
    threads = []
    for i in range(args.concurrency):
        t = threading.Thread(
            target=worker,
            args=(stop_evt, url, path, args.qps, args.batch, args.prompt, args.timeout, args.max_tokens, stats, i),
            daemon=True,
        )
        threads.append(t)
        t.start()

    t_end = time.time() + args.duration
    last_print = 0.0

    try:
        while time.time() < t_end:
            time.sleep(0.2)
            if time.time() - last_print >= 1.0:
                last_print = time.time()
                with stats["lock"]:
                    sent = stats["sent"]
                    ok = stats["ok"]
                    err = stats["err"]
                    lats = list(stats["lat_ms"])

                p50 = percentile(lats, 0.50)
                p95 = percentile(lats, 0.95)
                p99 = percentile(lats, 0.99)

                print(
                    f"[loadgen] url={url} sent={sent} ok={ok} err={err} "
                    f"p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms" if p50 else
                    f"[loadgen] url={url} sent={sent} ok={ok} err={err} (no latency samples yet)"
                )
    finally:
        stop_evt.set()
        for t in threads:
            t.join(timeout=1.0)

    with stats["lock"]:
        lats = list(stats["lat_ms"])
        sent = stats["sent"]
        ok = stats["ok"]
        err = stats["err"]

    p50 = percentile(lats, 0.50)
    p95 = percentile(lats, 0.95)
    p99 = percentile(lats, 0.99)

    print("\n=== loadgen summary ===")
    print(f"url={url}")
    print(f"sent={sent} ok={ok} err={err}")
    if p50:
        print(f"p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms")
    else:
        print("no latency samples")


if __name__ == "__main__":
    main()
