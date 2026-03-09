import json, yaml, requests, subprocess
from pathlib import Path

cfg = yaml.safe_load(open("config.yaml"))

def prom(q):
    r = requests.get(cfg["prometheus_url"]+"/api/v1/query", params={"query": q}, timeout=5).json()
    res = r.get("data",{}).get("result",[])
    if not res: return None
    v = float(res[0]["value"][1])
    if v != v: return None
    return v

def router_get():
    url = cfg["router_admin_base_url"].rstrip("/") + "/admin/backend"
    tok = cfg["router_admin_token"]
    r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=5)
    return r.status_code, r.text.strip()

def gpu_util():
    user = cfg["gpu_ssh_user"]; host = cfg["gpu_ssh_host"]
    cmd = "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits | head -n1"
    p = subprocess.run(
        ["ssh","-o","BatchMode=yes","-o","StrictHostKeyChecking=accept-new", f"{user}@{host}", cmd],
        text=True, capture_output=True
    )
    if p.returncode != 0:
        return None, p.stderr.strip()
    try:
        return float(p.stdout.strip()), ""
    except Exception as e:
        return None, f"parse_err: {e} out={p.stdout!r}"

state_path = Path("state.json")
state = json.loads(state_path.read_text()) if state_path.exists() else {}

slo = float(cfg.get("latency_slo_ms", 20))
thr = float(cfg.get("gpu_util_threshold", 30))

code, backend_txt = router_get()
print("ROUTER_ADMIN:", code, backend_txt)

p95_rms = prom(cfg["promql_latency_p95_rms_ms"])
p95_gpu = prom(cfg["promql_latency_p95_gpu_ms"])
print("P95_RMS_MS:", p95_rms, "| P95_GPU_MS:", p95_gpu, "| SLO_MS:", slo)

util, util_err = gpu_util()
print("GPU_UTIL_PCT:", util, "| err:", util_err, "| threshold:", thr)

print("STATE last_migrate_ts:", state.get("last_migrate_ts"))
print("STATE last_rollback_ts:", state.get("last_rollback_ts"))

