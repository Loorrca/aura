#!/usr/bin/env python3
import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import yaml

# Import your production control loop
import aura_ops


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "configs"


# -------------------------
# Config helpers (match aura_ops structure)
# -------------------------
def load_cfg() -> Dict[str, Any]:
    def _y(p: Path) -> Dict[str, Any]:
        return yaml.safe_load(p.read_text()) or {}
    return {
        "inventory": _y(CONFIG_DIR / "inventory.yaml"),
        "policy": _y(CONFIG_DIR / "policy.yaml"),
        "models": _y(CONFIG_DIR / "models.yaml"),
    }


def inv(cfg): return cfg.get("inventory") or {}
def pol(cfg): return cfg.get("policy") or {}
def mod(cfg): return cfg.get("models") or {}

def router_pub(cfg) -> str:
    return str((inv(cfg).get("router") or {}).get("public_url") or "").rstrip("/")

def router_admin(cfg) -> str:
    return str((inv(cfg).get("router") or {}).get("admin_url") or "").rstrip("/")

def token(cfg) -> str:
    return str((inv(cfg).get("router") or {}).get("admin_token") or "")

def nodes(cfg) -> Dict[str, Any]:
    return inv(cfg).get("nodes") or {}

def infer_url(cfg, node: str) -> str:
    return str((nodes(cfg).get(node) or {}).get("infer_url") or "").rstrip("/")

def prom_url(cfg) -> str:
    return str((inv(cfg).get("prometheus") or {}).get("url") or "").rstrip("/")

def prom_job(cfg, node: str) -> str:
    return str((nodes(cfg).get(node) or {}).get("prom_job") or "")

def default_delay_ms(cfg, node: str, fallback: int = 0) -> int:
    v = (nodes(cfg).get(node) or {}).get("default_delay_ms")
    return int(v) if v is not None else int(fallback)

def variant_uri(cfg, variant: str) -> str:
    v = ((mod(cfg).get("registry") or {}).get("variants") or {}).get(variant) or ""
    v = str(v).strip()
    if not v:
        raise RuntimeError(f"Missing models.registry.variants.{variant}")
    return v


# -------------------------
# HTTP helpers
# -------------------------
def http_json(method: str, url: str, **kwargs) -> Dict[str, Any]:
    r = requests.request(method, url, timeout=10, **kwargs)
    r.raise_for_status()
    return r.json()

def get_health(url: str) -> Dict[str, Any]:
    return http_json("GET", url.rstrip("/") + "/health")

def router_get_backend(cfg) -> Optional[str]:
    try:
        r = http_json(
            "GET",
            router_admin(cfg) + "/admin/backend",
            headers={"Authorization": f"Bearer {token(cfg)}"},
        )
        return str(r.get("backend") or "").rstrip("/")
    except Exception:
        return None

def router_set_backend(cfg, backend_url: str) -> None:
    http_json(
        "POST",
        router_admin(cfg) + "/admin/backend",
        headers={"Authorization": f"Bearer {token(cfg)}"},
        json={"backend": backend_url.rstrip("/")},
    )

def infer_admin_load(cfg, node: str, model_uri: str, delay_ms: Optional[int] = None) -> Dict[str, Any]:
    payload = {"model_uri": model_uri}
    if delay_ms is not None:
        payload["delay_ms"] = int(delay_ms)

    # Your inference app expects AURA_INFER_ADMIN_TOKEN; you currently reuse router token.
    return http_json(
        "POST",
        infer_url(cfg, node) + "/admin/load_model",
        headers={"Authorization": f"Bearer {token(cfg)}"},
        json=payload,
    )


# -------------------------
# Prometheus helpers
# -------------------------
def promql_instant(cfg, query: str) -> Optional[float]:
    r = requests.get(f"{prom_url(cfg)}/api/v1/query", params={"query": query}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        return None
    res = data.get("data", {}).get("result", [])
    if not res:
        return None
    v = float(res[0]["value"][1])
    if v != v:
        return None
    return v

def prom_p95_ms(cfg, node: str) -> Optional[float]:
    tpl = str((pol(cfg).get("promql_templates") or {}).get("latency_p95_ms") or "").strip()
    if not tpl:
        raise RuntimeError("policy.yaml missing promql_templates.latency_p95_ms")
    job = prom_job(cfg, node)
    q = tpl.replace("{{job}}", job)
    return promql_instant(cfg, q)

def prom_rps(cfg, node: str) -> Optional[float]:
    tpl = str((pol(cfg).get("promql_templates") or {}).get("rps") or "").strip()
    if not tpl:
        raise RuntimeError("policy.yaml missing promql_templates.rps")
    job = prom_job(cfg, node)
    q = tpl.replace("{{job}}", job)
    return promql_instant(cfg, q)

def prom_up(cfg, node: str) -> Optional[float]:
    job = prom_job(cfg, node)
    return promql_instant(cfg, f'up{{job="{job}"}}')


# -------------------------
# Load generator (router /predict)
# -------------------------
def _predict_once(router_base: str, payload: Dict[str, Any]) -> None:
    requests.post(router_base.rstrip("/") + "/predict", json=payload, timeout=5).raise_for_status()

def generate_load(router_base: str, seconds: int, concurrency: int, qps_per_thread: float) -> None:
    stop_at = time.time() + seconds
    payload = {"inputs": [[5.1, 3.5, 1.4, 0.2]]}  # iris-like

    sleep_s = 0.0 if qps_per_thread <= 0 else 1.0 / qps_per_thread

    def worker():
        while time.time() < stop_at:
            try:
                _predict_once(router_base, payload)
            except Exception:
                pass
            if sleep_s > 0:
                time.sleep(sleep_s)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads: t.start()
    for t in threads: t.join()


# -------------------------
# Pretty printing
# -------------------------
def banner(msg: str) -> None:
    print("\n" + "=" * 80)
    print(msg)
    print("=" * 80)

def show_status(cfg) -> None:
    b = router_get_backend(cfg)
    print(f"router backend: {b}")

    for n in nodes(cfg).keys():
        url = infer_url(cfg, n)
        try:
            h = get_health(url)
        except Exception as e:
            h = {"error": str(e)}
        print(f"{n:>4} health: {json.dumps(h)}")

    for n in nodes(cfg).keys():
        print(f"{n:>4} prom up: {prom_up(cfg, n)}  rps: {prom_rps(cfg, n)}  p95_ms: {prom_p95_ms(cfg, n)}")


# -------------------------
# Scenarios
# -------------------------
def scenario_migrate_to_bigger(cfg) -> None:
    """
    Force RMS to be "slow" (delay 40ms), GPU fast (delay 0ms),
    route to RMS, generate load, then run aura_ops.run_once()
    until it migrates to GPU.
    """
    slo = float((pol(cfg).get("slo") or {}).get("latency_slo_ms") or 20)
    banner(f"Scenario 1: migrate to bigger node when p95 > SLO (SLO={slo}ms)")

    # Ensure both nodes loaded with same variant ("full" by default) but different delay
    v = "full"
    banner("Set models on nodes (RMS slow, GPU fast)")
    infer_admin_load(cfg, "rms", variant_uri(cfg, v), delay_ms=default_delay_ms(cfg, "rms", 40))
    infer_admin_load(cfg, "gpu", variant_uri(cfg, v), delay_ms=default_delay_ms(cfg, "gpu", 0))

    banner("Route router -> RMS")
    router_set_backend(cfg, infer_url(cfg, "rms"))
    time.sleep(1)
    show_status(cfg)

    banner("Generate load through router for ~20s")
    generate_load(router_pub(cfg), seconds=20, concurrency=4, qps_per_thread=8)

    banner("Run aura_ops.run_once() a few times to trigger migration")
    for i in range(6):
        print(f"\n--- run_once #{i+1} ---")
        aura_ops.run_once()
        time.sleep(3)

    banner("Final status (expect router backend = GPU)")
    show_status(cfg)


def scenario_downgrade_variant(cfg) -> None:
    """
    Route to GPU, then make GPU "slow" by pushing delay_ms high (80ms).
    Since GPU is the biggest node, no bigger target exists => aura_ops should
    downgrade model variant on the same node (full -> quant -> fast).
    """
    slo = float((pol(cfg).get("slo") or {}).get("latency_slo_ms") or 20)
    banner(f"Scenario 2: downgrade model when no bigger node exists (SLO={slo}ms)")

    # Put router on GPU
    banner("Route router -> GPU")
    router_set_backend(cfg, infer_url(cfg, "gpu"))
    time.sleep(1)

    # Load 'full' and make it slow on GPU (induce violation)
    banner("Induce high latency on GPU (delay_ms=80)")
    infer_admin_load(cfg, "gpu", variant_uri(cfg, "full"), delay_ms=80)
    time.sleep(1)
    show_status(cfg)

    banner("Generate load through router for ~20s")
    generate_load(router_pub(cfg), seconds=20, concurrency=4, qps_per_thread=8)

    banner("Run aura_ops.run_once() a few times (expect set_variant_on_node)")
    for i in range(6):
        print(f"\n--- run_once #{i+1} ---")
        aura_ops.run_once()
        time.sleep(3)

    banner("Final status (expect GPU model switched to smaller variant AND delay reset to node default)")
    show_status(cfg)


def scenario_cooldown_smoke(cfg) -> None:
    """
    Run back-to-back run_once() and show that cooldown prevents immediate re-switch.
    """
    banner("Scenario 3: cooldown smoke test (second run should do nothing)")
    print("\n--- run_once #1 ---")
    aura_ops.run_once()
    print("\n--- run_once #2 (immediately) ---")
    aura_ops.run_once()
    banner("Check state/trace output for cooldown policy")
    print(f"State file: {BASE_DIR / 'state' / 'state.json'}")
    print(f"Last trace: {BASE_DIR / 'state' / 'last_trace.json'}")


# -------------------------
# CLI
# -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["preflight", "status", "migrate", "downgrade", "cooldown"])
    args = ap.parse_args()

    cfg = load_cfg()

    if args.cmd in ("preflight", "status"):
        banner("Preflight / Status")
        show_status(cfg)
        return

    if args.cmd == "migrate":
        scenario_migrate_to_bigger(cfg)
        return

    if args.cmd == "downgrade":
        scenario_downgrade_variant(cfg)
        return

    if args.cmd == "cooldown":
        scenario_cooldown_smoke(cfg)
        return


if __name__ == "__main__":
    main()
