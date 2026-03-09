#!/usr/bin/env python3
from __future__ import annotations
import secrets
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
OPS_CFG = ROOT / "ops" / "configs"
PROM_CFG = ROOT / "monitoring" / "prometheus"
OPS_CFG.mkdir(parents=True, exist_ok=True)
PROM_CFG.mkdir(parents=True, exist_ok=True)

def ask(prompt: str, default: str | None = None) -> str:
    s = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    return s or (default or "")

def ask_url(name: str, default: str) -> str:
    u = ask(name, default).rstrip("/")
    return u

def main():
    print("\nAURA interactive init\n")

    # Core URLs
    router_public = ask_url("Router public URL", "http://<router-ip>:9000")
    router_admin  = ask_url("Router admin URL",  router_public)

    prom_url      = ask_url("Prometheus URL", "http://<monitor-ip>:9090")

    # Tokens
    token = ask("Admin token (leave blank to generate)", "")
    if not token:
        token = secrets.token_urlsafe(24)
        print(f"Generated admin token: {token}")

    # Nodes
    rms_infer = ask_url("RMS inference URL", "http://<rms-ip>:8000")
    gpu_infer = ask_url("GPU inference URL", "http://<gpu-ip>:8000")

    # Prometheus jobs (match your prom scrape job names)
    rms_job = ask("Prometheus job name for RMS", "rms_inference")
    gpu_job = ask("Prometheus job name for GPU", "gpu_inference")

    # Optional SSH for signals/actions
    rms_ssh_user = ask("SSH user for RMS (optional)", "")
    rms_ssh_host = ask("SSH host/IP for RMS (optional)", "")
    gpu_ssh_user = ask("SSH user for GPU (optional)", "")
    gpu_ssh_host = ask("SSH host/IP for GPU (optional)", "")

    inv = {
        "router": {
            "public_url": router_public,
            "admin_url": router_admin,
            "admin_token": token,
        },
        "prometheus": {
            "url": prom_url,
        },
        "nodes": {
            "rms": {
                "infer_url": rms_infer,
                "prom_job": rms_job,
                "supports": ["full", "fast", "quant"],
            },
            "gpu": {
                "infer_url": gpu_infer,
                "prom_job": gpu_job,
                "supports": ["full", "fast", "quant"],
            },
        },
    }

    if rms_ssh_user and rms_ssh_host:
        inv["nodes"]["rms"]["ssh"] = {"user": rms_ssh_user, "host": rms_ssh_host}
    if gpu_ssh_user and gpu_ssh_host:
        inv["nodes"]["gpu"]["ssh"] = {"user": gpu_ssh_user, "host": gpu_ssh_host}

    (OPS_CFG / "inventory.yaml").write_text(yaml.safe_dump(inv, sort_keys=False))

    # Prometheus config (simple static scrape)
    prom_yml = {
        "global": {"scrape_interval": "15s"},
        "scrape_configs": [
            {"job_name": "prometheus", "static_configs": [{"targets": ["localhost:9090"]}]},
            {"job_name": rms_job, "metrics_path": "/metrics",
             "static_configs": [{"targets": [rms_infer.replace("http://","")], "labels": {"node":"rms","service":"inference"}}]},
            {"job_name": gpu_job, "metrics_path": "/metrics",
             "static_configs": [{"targets": [gpu_infer.replace("http://","")], "labels": {"node":"gpu","service":"inference"}}]},
            {"job_name": "aura_router", "metrics_path": "/metrics",
             "static_configs": [{"targets": [router_public.replace("http://","")]}]},
        ],
    }
    (PROM_CFG / "prometheus.yml").write_text(yaml.safe_dump(prom_yml, sort_keys=False))

    print("\nWrote:")
    print(" - ops/configs/inventory.yaml")
    print(" - monitoring/prometheus/prometheus.yml")
    print("\nNext: edit ops/configs/models.yaml and ops/configs/policy.yaml if needed.\n")

if __name__ == "__main__":
    main()
