import json
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple
from datetime import datetime

import requests
import yaml

# =========================
# Paths (relative to this file)
# =========================
BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "configs"
STATE_DIR = BASE_DIR / "state"

STATE_PATH = STATE_DIR / "state.json"
TRACE_PATH = STATE_DIR / "last_trace.json"
TRACE_LOG_PATH = STATE_DIR / "trace.jsonl"
TRACE_LOG_MAX_BYTES = 50 * 1024 * 1024
TRACE_LOG_KEEP = 3


@dataclass
class Event:
    ts: float
    kind: str
    severity: str
    details: Dict[str, Any]


@dataclass
class Diagnosis:
    ts: float
    hypothesis: str
    evidence: Dict[str, Any]


@dataclass
class Decision:
    ts: float
    action: str
    rationale: Dict[str, Any]


@dataclass
class ExecutionResult:
    ts: float
    action: str
    ok: bool
    verification: Dict[str, Any]
    stdout: str = ""
    stderr: str = ""


# =========================
# Config loading (new structure)
# =========================
def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    return yaml.safe_load(path.read_text()) or {}


def load_cfg() -> Dict[str, Any]:
    inv = _load_yaml(CONFIG_DIR / "inventory.yaml")
    pol = _load_yaml(CONFIG_DIR / "policy.yaml")
    mod = _load_yaml(CONFIG_DIR / "models.yaml")
    return {"inventory": inv, "policy": pol, "models": mod}


# =========================
# Small helpers for cfg shape
# =========================
def inv(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return cfg.get("inventory") or {}


def pol(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return cfg.get("policy") or {}


def mod(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return cfg.get("models") or {}


def policy_get(cfg: Dict[str, Any], path: List[str], default: Any) -> Any:
    cur: Any = pol(cfg)
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def prometheus_url(cfg: Dict[str, Any]) -> str:
    return str((inv(cfg).get("prometheus") or {}).get("url") or "").rstrip("/")


def router_public_url(cfg: Dict[str, Any]) -> str:
    return str((inv(cfg).get("router") or {}).get("public_url") or "").rstrip("/")


def router_admin_url(cfg: Dict[str, Any]) -> str:
    return str((inv(cfg).get("router") or {}).get("admin_url") or "").rstrip("/")


def router_token(cfg: Dict[str, Any]) -> str:
    return str((inv(cfg).get("router") or {}).get("admin_token") or "")


def nodes_map(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return inv(cfg).get("nodes") or {}


def node(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    return nodes_map(cfg).get(name) or {}


def node_infer_url(cfg: Dict[str, Any], name: str) -> str:
    return str(node(cfg, name).get("infer_url") or "").rstrip("/")


def node_prom_job(cfg: Dict[str, Any], name: str) -> str:
    return str(node(cfg, name).get("prom_job") or "")


def node_supports(cfg: Dict[str, Any], name: str) -> List[str]:
    return list(node(cfg, name).get("supports") or [])


def node_default_delay_ms(cfg: Dict[str, Any], name: str, fallback: int = 0) -> int:
    v = node(cfg, name).get("default_delay_ms")
    return int(v) if v is not None else int(fallback)


def node_capacity_rank(cfg: Dict[str, Any], name: str) -> int:
    """
    Optional production knob:
      inventory.nodes.<name>.capacity_rank (higher = more powerful)
    If absent, apply a safe heuristic: 'gpu' > others.
    """
    v = node(cfg, name).get("capacity_rank")
    if v is not None:
        try:
            return int(v)
        except Exception:
            pass
    return 100 if name.lower() == "gpu" else 10


def node_ssh_user_host(cfg: Dict[str, Any], name: str) -> Optional[Tuple[str, str]]:
    ssh = node(cfg, name).get("ssh") or {}
    user = str(ssh.get("user") or "")
    host = str(ssh.get("host") or "")
    if not user or not host:
        return None
    return (user, host)


def infer_admin_token_for_node(cfg: Dict[str, Any], node_name: str) -> str:
    """
    Allows per-node override:
      inventory.nodes.<node>.admin_token
    Falls back to router.admin_token for your current setup.
    """
    tok = str(node(cfg, node_name).get("admin_token") or "").strip()
    if tok:
        return tok
    return router_token(cfg)


# =========================
# State + logging
# =========================
def read_state() -> Dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "last_restart_ts": 0.0,
        "last_switch_ts": 0.0,
        "last_backend_switch_ts": 0.0,
        "last_variant_switch_ts": 0.0,
        "last_seen_model_uri": None,
        "last_seen_backend": None,
        "last_seen_node": None,
    }


def write_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def ts_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def rotate_trace_log_if_needed() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if TRACE_LOG_PATH.exists() and TRACE_LOG_PATH.stat().st_size >= TRACE_LOG_MAX_BYTES:
            for i in range(TRACE_LOG_KEEP, 0, -1):
                src = TRACE_LOG_PATH.with_suffix(TRACE_LOG_PATH.suffix + f".{i}")
                dst = TRACE_LOG_PATH.with_suffix(TRACE_LOG_PATH.suffix + f".{i+1}")
                if src.exists():
                    if i == TRACE_LOG_KEEP:
                        src.unlink(missing_ok=True)
                    else:
                        src.rename(dst)
            TRACE_LOG_PATH.rename(TRACE_LOG_PATH.with_suffix(TRACE_LOG_PATH.suffix + ".1"))
    except Exception:
        pass


# =========================
# Prometheus + HTTP + SSH
# =========================
def promql_instant(prom_url: str, query: str) -> Optional[float]:
    r = requests.get(f"{prom_url}/api/v1/query", params={"query": query}, timeout=5)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        return None
    result = data.get("data", {}).get("result", [])
    if not result:
        return None
    val = float(result[0]["value"][1])
    if val != val:  # NaN
        return None
    return val


def ssh_run(user: str, host: str, remote_cmd: str) -> subprocess.CompletedProcess:
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=3", "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=1", f"{user}@{host}", remote_cmd]
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=8)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "ssh_timeout")
def get_health(base_url: str) -> Dict[str, Any]:
    r = requests.get(f"{base_url.rstrip('/')}/health", timeout=5)
    r.raise_for_status()
    return r.json()


def parse_model_version(model_uri: str) -> Optional[str]:
    if not model_uri:
        return None
    parts = model_uri.strip().split("/")
    return parts[-1] if len(parts) >= 3 else None


# =========================
# Router API
# =========================
def router_get_backend(cfg: Dict[str, Any]) -> Optional[str]:
    try:
        url = router_admin_url(cfg) + "/admin/backend"
        token = router_token(cfg)
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=5)
        r.raise_for_status()
        return r.json().get("backend", "").rstrip("/")
    except Exception:
        return None


def router_set_backend(cfg: Dict[str, Any], backend_url: str) -> None:
    url = router_admin_url(cfg) + "/admin/backend"
    token = router_token(cfg)
    r = requests.post(
        url,
        json={"backend": backend_url},
        headers={"Authorization": f"Bearer {token}"},
        timeout=8,
    )
    r.raise_for_status()


# =========================
# Model catalog helpers
# =========================
def pick_variant_model_uri(cfg: Dict[str, Any], variant: str) -> str:
    variants = ((mod(cfg).get("registry") or {}).get("variants") or {})
    uri = (variants.get(variant) or "").strip()
    if not uri:
        raise RuntimeError(f"models.yaml: registry.variants.{variant} is missing/empty")
    return uri


def guess_variant_from_model_uri(cfg: Dict[str, Any], model_uri: Optional[str]) -> Optional[str]:
    if not model_uri:
        return None
    variants = ((mod(cfg).get("registry") or {}).get("variants") or {})
    ver = parse_model_version(model_uri)
    for name, uri in variants.items():
        if uri == model_uri:
            return name
        if ver and parse_model_version(uri) == ver:
            return name
    return None


def variant_supported(cfg: Dict[str, Any], node_name: str, variant: str) -> bool:
    return variant in node_supports(cfg, node_name)


def variant_order_big_to_small(cfg: Dict[str, Any]) -> List[str]:
    """
    Optional policy knob:
      policy.models.variant_order_big_to_small: ["full","quant","fast"]
    Defaults to a sane order.
    """
    v = policy_get(cfg, ["models", "variant_order_big_to_small"], None)
    if isinstance(v, list) and v:
        return [str(x) for x in v if str(x)]
    return ["full", "quant", "fast"]


def next_smaller_variant(cfg: Dict[str, Any], current: str) -> Optional[str]:
    order = variant_order_big_to_small(cfg)
    if current not in order:
        # if unknown: go to desired or fast
        return "fast" if "fast" in order else (order[-1] if order else None)
    i = order.index(current)
    if i >= len(order) - 1:
        return None
    return order[i + 1]


# =========================
# Signals
# =========================
def ssh_gpu_util_percent(cfg: Dict[str, Any]) -> Optional[float]:
    pair = node_ssh_user_host(cfg, "gpu")
    if not pair:
        return None
    user, host = pair
    cmd = "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits | head -n1"
    proc = ssh_run(user, host, cmd)
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except Exception:
        return None


def infer_admin_load_model(cfg: Dict[str, Any], node_name: str, base_url: str, model_uri: str, delay_ms: int) -> Dict[str, Any]:
    token = infer_admin_token_for_node(cfg, node_name)
    if not token:
        raise RuntimeError("Missing admin token for inference admin endpoints")

    timeout = int(policy_get(cfg, ["verification", "infer_admin_timeout_seconds"], 20))
    url = base_url.rstrip("/") + "/admin/load_model"
    r = requests.post(
        url,
        json={"model_uri": model_uri, "delay_ms": int(delay_ms)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


# =========================
# PromQL templates
# =========================
def render_template(tpl: str, **kwargs: str) -> str:
    out = tpl
    for k, v in kwargs.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def promql_latency_p95_ms(cfg: Dict[str, Any], node_name: str) -> str:
    tpl = str(policy_get(cfg, ["promql_templates", "latency_p95_ms"], "")).strip()
    if not tpl:
        raise RuntimeError("policy.yaml: promql_templates.latency_p95_ms missing")
    job = node_prom_job(cfg, node_name)
    if not job:
        raise RuntimeError(f"inventory.yaml: nodes.{node_name}.prom_job missing")
    return render_template(tpl, job=job)


def promql_rps(cfg: Dict[str, Any], node_name: str) -> str:
    tpl = str(policy_get(cfg, ["promql_templates", "rps"], "")).strip()
    if not tpl:
        raise RuntimeError("policy.yaml: promql_templates.rps missing")
    job = node_prom_job(cfg, node_name)
    if not job:
        raise RuntimeError(f"inventory.yaml: nodes.{node_name}.prom_job missing")
    return render_template(tpl, job=job)


def promql_up(cfg: Dict[str, Any], node_name: str) -> str:
    job = node_prom_job(cfg, node_name)
    if not job:
        raise RuntimeError(f"inventory.yaml: nodes.{node_name}.prom_job missing")
    return f'up{{job="{job}"}}'


def node_up(cfg: Dict[str, Any], node_name: str) -> Optional[float]:
    return promql_instant(prometheus_url(cfg), promql_up(cfg, node_name))


def node_p95_ms(cfg: Dict[str, Any], node_name: str) -> Optional[float]:
    return promql_instant(prometheus_url(cfg), promql_latency_p95_ms(cfg, node_name))


def node_rps(cfg: Dict[str, Any], node_name: str) -> Optional[float]:
    return promql_instant(prometheus_url(cfg), promql_rps(cfg, node_name))


def backend_url_to_node(cfg: Dict[str, Any], backend_url: Optional[str]) -> Optional[str]:
    if not backend_url:
        return None
    b = backend_url.rstrip("/")
    for name, nd in nodes_map(cfg).items():
        u = str((nd or {}).get("infer_url") or "").rstrip("/")
        if u and u == b:
            return name
    return None


# =========================
# Agents
# =========================
def observability_agent(cfg: Dict[str, Any], state: Dict[str, Any]) -> List[Event]:
    events: List[Event] = []
    now = time.time()

    # Current backend + node
    current_backend = router_get_backend(cfg)
    current_node = backend_url_to_node(cfg, current_backend)
    if current_backend:
        state["last_seen_backend"] = current_backend
        state["last_seen_node"] = current_node
        write_state(state)

    # Current serving model/variant via router /health (best-effort)
    current_model_uri = None
    current_variant = None
    try:
        rh = get_health(router_public_url(cfg))
        bh = (rh.get("backend_health") or {})
        current_model_uri = bh.get("model_uri")
        current_variant = guess_variant_from_model_uri(cfg, current_model_uri)
        if current_model_uri:
            state["last_seen_model_uri"] = current_model_uri
            write_state(state)
    except Exception:
        pass

    # Service down rule (kept)
    rule_sd = policy_get(cfg, ["rules", "service_down"], {}) or {}
    sd_promql = str(rule_sd.get("promql") or "").strip()
    sd_thr = float(rule_sd.get("threshold", 1))
    if sd_promql:
        try:
            up_val = promql_instant(prometheus_url(cfg), sd_promql)
        except Exception:
            up_val = None
        if up_val is not None and up_val < sd_thr:
            events.append(Event(now, "service_down", "high", {"up_value": up_val, "threshold": sd_thr}))

    # Active backend over SLO
    slo_ms = float(policy_get(cfg, ["slo", "latency_slo_ms"], 20))
    min_rps = float(policy_get(cfg, ["slo", "min_rps_for_decision"], 0.0))

    gpu_thr = float(policy_get(cfg, ["signals", "gpu_util_threshold"], 30))
    gpu_util = ssh_gpu_util_percent(cfg)

    if current_node:
        p95 = node_p95_ms(cfg, current_node)
        rps = node_rps(cfg, current_node)
        if p95 is not None and p95 > slo_ms and rps is not None and rps >= min_rps:
            events.append(Event(
                now,
                "active_backend_over_slo",
                "high",
                {
                    "current_backend": current_backend,
                    "current_node": current_node,
                    "p95_ms": p95,
                    "rps": rps,
                    "latency_slo_ms": slo_ms,
                    "min_rps_for_decision": min_rps,
                    "gpu_util_pct": gpu_util,
                    "gpu_util_threshold": gpu_thr,
                    "current_model_uri": current_model_uri,
                    "current_variant": current_variant,
                },
            ))

    return events


def prioritize(events: List[Event]) -> Optional[Event]:
    if not events:
        return None
    order = {"service_down": 0, "active_backend_over_slo": 1}
    return sorted(events, key=lambda e: order.get(e.kind, 99))[0]


def diagnosis_agent(evt: Event) -> Diagnosis:
    now = time.time()
    if evt.kind == "service_down":
        return Diagnosis(now, "inference_service_unreachable", {"reason": "prom_up_is_0", **evt.details})
    if evt.kind == "active_backend_over_slo":
        return Diagnosis(now, "active_over_slo", {"reason": "p95_over_slo", **evt.details})
    return Diagnosis(now, "unknown", evt.details)


def decision_agent(cfg: Dict[str, Any], diag: Diagnosis, state: Dict[str, Any]) -> Decision:
    now = time.time()

    restart_cd = int(policy_get(cfg, ["cooldowns", "restart_seconds"], 120))
    switch_cd = int(policy_get(cfg, ["cooldowns", "switch_seconds"], 180))
    backend_cd = int(policy_get(cfg, ["cooldowns", "migrate_seconds"], 30))
    variant_cd = int(policy_get(cfg, ["cooldowns", "switch_seconds"], 180))

    last_restart = float(state.get("last_restart_ts", 0.0))
    last_switch = float(state.get("last_switch_ts", 0.0))
    last_backend_switch = float(state.get("last_backend_switch_ts", 0.0))
    last_variant_switch = float(state.get("last_variant_switch_ts", 0.0))

    # Restart action (kept, RMS only unless you generalize later)
    if diag.hypothesis == "inference_service_unreachable":
        if now - last_restart < restart_cd:
            return Decision(now, "do_nothing", {"policy": "restart_cooldown", "seconds_since_last": now - last_restart})
        return Decision(now, "restart_inference", {"policy": "allowed", "node": "rms"})

    # Legacy model switch action (kept for your week-5 demo path if you still call it)
    if diag.hypothesis == "latency_slo_violation":
        if now - last_switch < switch_cd:
            return Decision(now, "do_nothing", {"policy": "switch_cooldown", "seconds_since_last": now - last_switch})
        target_uri = pick_variant_model_uri(cfg, "fast")
        target_ver = parse_model_version(target_uri)
        if state.get("last_seen_model_uri") and parse_model_version(str(state.get("last_seen_model_uri"))) == target_ver:
            return Decision(now, "do_nothing", {"policy": "already_on_target_version", "target_version": target_ver})
        return Decision(now, "switch_model_version", {"policy": "allowed", "target_version": target_ver})

    # Production rule: active backend over SLO
    if diag.hypothesis == "active_over_slo":
        current_node = str(diag.evidence.get("current_node") or "").strip()
        if not current_node:
            return Decision(now, "do_nothing", {"policy": "missing_current_node"})

        # Use serving variant if known; else desired.variant
        desired_variant = str(diag.evidence.get("current_variant") or policy_get(cfg, ["desired", "variant"], "fast")).strip()
        if not desired_variant:
            desired_variant = "fast"

        # 1) Try migrate to a bigger node that can run the SAME variant
        if now - last_backend_switch >= backend_cd:
            cur_rank = node_capacity_rank(cfg, current_node)
            gpu_thr = float(diag.evidence.get("gpu_util_threshold", policy_get(cfg, ["signals", "gpu_util_threshold"], 30)))
            gpu_util = diag.evidence.get("gpu_util_pct")

            candidates: List[str] = []
            for n in nodes_map(cfg).keys():
                if n == current_node:
                    continue
                # only consider "bigger"
                if node_capacity_rank(cfg, n) <= cur_rank:
                    continue
                # must be up
                upv = node_up(cfg, n)
                if upv is not None and upv < 1:
                    continue
                # must support the variant
                if not variant_supported(cfg, n, desired_variant):
                    continue
                # headroom constraint for GPU (extend later per node)
                if n == "gpu" and gpu_util is not None and float(gpu_util) >= gpu_thr:
                    continue
                candidates.append(n)

            # choose biggest
            candidates.sort(key=lambda x: node_capacity_rank(cfg, x), reverse=True)
            if candidates:
                target_node = candidates[0]
                chosen_uri = pick_variant_model_uri(cfg, desired_variant)
                return Decision(
                    now,
                    "set_backend",
                    {
                        "policy": "allowed",
                        "because": "active_over_slo_migrate_to_bigger_node",
                        "current_node": current_node,
                        "target_node": target_node,
                        "chosen_variant": desired_variant,
                        "chosen_uri": chosen_uri,
                    },
                )

        # 2) Otherwise, downgrade model on current node (smaller model)
        if now - last_variant_switch < variant_cd:
            return Decision(now, "do_nothing", {"policy": "variant_switch_cooldown", "seconds_since_last": now - last_variant_switch})

        smaller = next_smaller_variant(cfg, desired_variant)
        if not smaller:
            return Decision(now, "do_nothing", {"policy": "already_smallest_variant", "current_variant": desired_variant})

        if not variant_supported(cfg, current_node, smaller):
            return Decision(now, "do_nothing", {"policy": "smaller_variant_not_supported_on_current_node", "current_node": current_node, "smaller_variant": smaller})

        chosen_uri = pick_variant_model_uri(cfg, smaller)
        return Decision(
            now,
            "set_variant_on_node",
            {
                "policy": "allowed",
                "because": "no_bigger_node_available_downgrade_model",
                "node": current_node,
                "chosen_variant": smaller,
                "chosen_uri": chosen_uri,
            },
        )

    return Decision(now, "do_nothing", {"policy": "no_rule"})


# =========================
# Execution helpers
# =========================
def _post_hotswap_sleep(cfg: Dict[str, Any]) -> None:
    time.sleep(float(policy_get(cfg, ["verification", "post_hotswap_sleep_seconds"], 1.0)))


def execution_agent(cfg: Dict[str, Any], decision: Decision, state: Dict[str, Any]) -> ExecutionResult:
    now = time.time()

    if decision.action == "do_nothing":
        return ExecutionResult(now, "do_nothing", True, {"skipped": True})

    # Restart RMS (legacy)
    if decision.action == "restart_inference":
        node_name = str((decision.rationale or {}).get("node") or "rms")
        pair = node_ssh_user_host(cfg, node_name)
        if not pair:
            return ExecutionResult(time.time(), "restart_inference", False, {"error": f"missing ssh for node {node_name}"})
        user, host = pair
        svc = str(policy_get(cfg, ["legacy", "rms_service_name"], "aura-infer"))
        proc = ssh_run(user, host, f"sudo systemctl restart {svc}")
        time.sleep(2)

        ok = False
        health = {}
        try:
            health = get_health(node_infer_url(cfg, node_name))
            ok = (health.get("status") == "ok")
        except Exception:
            ok = False

        if ok:
            state["last_restart_ts"] = time.time()
            write_state(state)

        return ExecutionResult(time.time(), "restart_inference", ok, {"health": health}, proc.stdout.strip(), proc.stderr.strip())

    # Legacy sed switch (optional)
    if decision.action == "switch_model_version":
        pair = node_ssh_user_host(cfg, "rms")
        if not pair:
            return ExecutionResult(time.time(), "switch_model_version", False, {"error": "missing ssh for rms"})
        user, host = pair
        svc = str(policy_get(cfg, ["legacy", "rms_service_name"], "aura-infer"))
        target_ver = str((decision.rationale or {}).get("target_version") or "").strip() or "2"

        cmd = (
            f"sudo sed -i 's/^AURA_MODEL_VERSION=.*/AURA_MODEL_VERSION={target_ver}/' /etc/aura/infer.env && "
            f"sudo systemctl restart {svc}"
        )
        proc = ssh_run(user, host, cmd)
        time.sleep(2)

        ok = proc.returncode == 0
        if ok:
            state["last_switch_ts"] = time.time()
            write_state(state)

        return ExecutionResult(time.time(), "switch_model_version", ok, {"target_version": target_ver}, proc.stdout.strip(), proc.stderr.strip())

    # Set variant on a node (no router change)
    if decision.action == "set_variant_on_node":
        node_name = str((decision.rationale or {}).get("node") or "").strip()
        if not node_name:
            return ExecutionResult(time.time(), "set_variant_on_node", False, {"error": "missing node in rationale"})

        url = node_infer_url(cfg, node_name)
        if not url:
            return ExecutionResult(time.time(), "set_variant_on_node", False, {"error": f"missing infer_url for {node_name}"})

        desired_uri = str((decision.rationale or {}).get("chosen_uri") or "").strip()
        if not desired_uri:
            return ExecutionResult(time.time(), "set_variant_on_node", False, {"error": "missing chosen_uri"})

        desired_delay = node_default_delay_ms(cfg, node_name, fallback=0)

        try:
            admin_resp = infer_admin_load_model(cfg, node_name, url, desired_uri, desired_delay)
        except Exception as e:
            return ExecutionResult(time.time(), "set_variant_on_node", False, {"error": f"hotswap_failed: {e}", "node": node_name})

        _post_hotswap_sleep(cfg)

        try:
            h = get_health(url)
        except Exception as e:
            return ExecutionResult(time.time(), "set_variant_on_node", False, {"error": f"health_failed: {e}", "admin_resp": admin_resp})

        ok = (h.get("model_uri") == desired_uri)
        if ok:
            state["last_variant_switch_ts"] = time.time()
            state["last_seen_model_uri"] = desired_uri
            write_state(state)

        return ExecutionResult(time.time(), "set_variant_on_node", ok, {"node": node_name, "health": h, "admin_resp": admin_resp})

    # Set backend (load model on target, then route)
    if decision.action == "set_backend":
        target_node = str((decision.rationale or {}).get("target_node") or "").strip()
        if not target_node:
            return ExecutionResult(time.time(), "set_backend", False, {"error": "missing target_node in rationale"})

        target_url = node_infer_url(cfg, target_node)
        if not target_url:
            return ExecutionResult(time.time(), "set_backend", False, {"error": f"missing infer_url for node {target_node}"})

        desired_uri = str((decision.rationale or {}).get("chosen_uri") or "").strip()
        if not desired_uri:
            return ExecutionResult(time.time(), "set_backend", False, {"error": "missing chosen_uri"})

        desired_delay = node_default_delay_ms(cfg, target_node, fallback=0)

        # 1) hot-swap model on target
        try:
            admin_resp = infer_admin_load_model(cfg, target_node, target_url, desired_uri, desired_delay)
        except Exception as e:
            return ExecutionResult(time.time(), "set_backend", False, {"error": f"hotswap_failed: {e}", "target_node": target_node})

        _post_hotswap_sleep(cfg)

        # 2) verify target model
        try:
            th = get_health(target_url)
        except Exception as e:
            return ExecutionResult(time.time(), "set_backend", False, {"error": f"target_health_failed: {e}", "admin_resp": admin_resp})

        if th.get("model_uri") != desired_uri:
            return ExecutionResult(
                time.time(),
                "set_backend",
                False,
                {"error": "target_model_mismatch", "desired_uri": desired_uri, "target_health": th, "admin_resp": admin_resp},
            )

        # 3) switch router
        try:
            router_set_backend(cfg, target_url)
            observed = router_get_backend(cfg)
            ok = (observed == target_url.rstrip("/"))

            if ok:
                state["last_backend_switch_ts"] = time.time()
                state["last_seen_backend"] = observed
                state["last_seen_node"] = target_node
                state["last_seen_model_uri"] = desired_uri
                write_state(state)

            router_h = {}
            try:
                router_h = get_health(router_public_url(cfg))
            except Exception:
                pass

            return ExecutionResult(
                time.time(),
                "set_backend",
                ok,
                {"observed_backend": observed, "router_health": router_h, "target_health": th, "admin_resp": admin_resp, "target_node": target_node},
            )
        except Exception as e:
            return ExecutionResult(time.time(), "set_backend", False, {"error": str(e), "target_health": th, "admin_resp": admin_resp})

    return ExecutionResult(now, decision.action, False, {"error": "unknown_action"})


def explanation_agent(evt: Optional[Event], diag: Optional[Diagnosis], dec: Decision, res: ExecutionResult) -> None:
    trace = {
        "event": asdict(evt) if evt else None,
        "diagnosis": asdict(diag) if diag else None,
        "decision": asdict(dec),
        "execution": asdict(res),
    }

    if trace["event"] and "ts" in trace["event"]:
        trace["event"]["ts_iso"] = ts_iso(trace["event"]["ts"])
    if trace["diagnosis"] and "ts" in trace["diagnosis"]:
        trace["diagnosis"]["ts_iso"] = ts_iso(trace["diagnosis"]["ts"])
    if trace["decision"] and "ts" in trace["decision"]:
        trace["decision"]["ts_iso"] = ts_iso(trace["decision"]["ts"])
    if trace["execution"] and "ts" in trace["execution"]:
        trace["execution"]["ts_iso"] = ts_iso(trace["execution"]["ts"])

    print(json.dumps(trace, indent=2))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_PATH.write_text(json.dumps(trace, indent=2))

    rotate_trace_log_if_needed()
    with TRACE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(trace) + "\n")


def run_once() -> None:
    cfg = load_cfg()
    state = read_state()

    evt = prioritize(observability_agent(cfg, state))
    if evt is None:
        dec = Decision(time.time(), "do_nothing", {"policy": "no_events"})
        res = ExecutionResult(time.time(), "do_nothing", True, {"skipped": True})
        explanation_agent(None, None, dec, res)
        return

    diag = diagnosis_agent(evt)
    dec = decision_agent(cfg, diag, state)
    res = execution_agent(cfg, dec, state)
    explanation_agent(evt, diag, dec, res)


def main():
    cfg = load_cfg()
    interval = int(policy_get(cfg, ["loop", "check_interval_seconds"], 15))
    print(f"[AURA-OPS] loop started, interval={interval}s")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[AURA-OPS] error: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
