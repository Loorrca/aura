#!/usr/bin/env bash
set -euo pipefail

# Pretty output
BOLD="$(tput bold || true)"; DIM="$(tput dim || true)"; RESET="$(tput sgr0 || true)"
RED="$(tput setaf 1 || true)"; GREEN="$(tput setaf 2 || true)"; YELLOW="$(tput setaf 3 || true)"; CYAN="$(tput setaf 6 || true)"; GRAY="$(tput setaf 7 || true)"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG_INV="$HERE/configs/inventory.yaml"
CFG_MOD="$HERE/configs/models.yaml"
CFG_POL="$HERE/configs/policy.yaml"

PY="${PYTHON:-python}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "${RED}missing dependency: $1${RESET}"; exit 1; }; }
need curl
need "$PY"

# Parse YAML with python (no yq dependency)
py_yaml_get() {
  local file="$1"; local expr="$2"
  "$PY" - <<PY
import yaml
p="$file"
expr="$expr"
d=yaml.safe_load(open(p)) or {}
cur=d
for k in expr.split("."):
    if not k:
        continue
    if isinstance(cur, dict) and k in cur:
        cur=cur[k]
    else:
        cur=None
        break
print("" if cur is None else str(cur).strip())
PY
}

# ---- load config values ----
router_public="$(py_yaml_get "$CFG_INV" "router.public_url" | tr -d '\r')"
router_admin="$(py_yaml_get "$CFG_INV" "router.admin_url" | tr -d '\r')"
token="$(py_yaml_get "$CFG_INV" "router.admin_token" | tr -d '\r')"
prom="$(py_yaml_get "$CFG_INV" "prometheus.url" | tr -d '\r')"

rms_url="$(py_yaml_get "$CFG_INV" "nodes.rms.infer_url" | tr -d '\r')"
gpu_url="$(py_yaml_get "$CFG_INV" "nodes.gpu.infer_url" | tr -d '\r')"

job_rms="$(py_yaml_get "$CFG_INV" "nodes.rms.prom_job" | tr -d '\r')"
job_gpu="$(py_yaml_get "$CFG_INV" "nodes.gpu.prom_job" | tr -d '\r')"

model_full="$(py_yaml_get "$CFG_MOD" "registry.variants.full" | tr -d '\r')"
model_fast="$(py_yaml_get "$CFG_MOD" "registry.variants.fast" | tr -d '\r')"
model_quant="$(py_yaml_get "$CFG_MOD" "registry.variants.quant" | tr -d '\r')"

slo_ms="$(py_yaml_get "$CFG_POL" "slo.latency_slo_ms" | tr -d '\r')"
min_rps="$(py_yaml_get "$CFG_POL" "slo.min_rps_for_decision" | tr -d '\r')"

# PromQL templates
tpl_p95="$(py_yaml_get "$CFG_POL" "promql_templates.latency_p95_ms" | tr -d '\r')"
tpl_rps="$(py_yaml_get "$CFG_POL" "promql_templates.rps" | tr -d '\r')"

# ---- canonical env vars used by functions (fix TOKEN/ROUTER_ADMIN/PROM confusion) ----
ROUTER_PUBLIC="${router_public%/}"
ROUTER_ADMIN="${router_admin%/}"
TOKEN="$token"
PROM="${prom%/}"

render_tpl() {
  local tpl="$1"; local job="$2"
  echo "${tpl//'{{job}}'/$job}"
}

q_up()   { local job="$1"; echo "up{job=\"$job\"}"; }
q_rps()  { local job="$1"; render_tpl "$tpl_rps" "$job"; }
q_p95()  { local job="$1"; render_tpl "$tpl_p95" "$job"; }

curl_json() {
  # Usage: curl_json URL
  curl -fsS --connect-timeout "${CURL_CONNECT_TIMEOUT:-1}" --max-time "${CURL_MAXTIME:-3}" \
    -H "Authorization: Bearer ${TOKEN}" \
    "$1"
}

json_get() {
  # json_get <key>  (reads JSON from stdin)
  local key="$1"
  local data
  data="$(cat)"
  "$PY" - <<'PYIN' "$key" "$data"
import json,sys
key=sys.argv[1]
data=sys.argv[2]
try:
    o=json.loads(data) if data else {}
except Exception:
    print("")
    raise SystemExit(0)

def walk(o,key):
    if isinstance(o, dict):
        if key in o: return o[key]
        for v in o.values():
            r=walk(v,key)
            if r is not None: return r
    elif isinstance(o, list):
        for v in o:
            r=walk(v,key)
            if r is not None: return r
    return None
v=walk(o,key)
print("" if v is None else v)
PYIN
}


prom_query() {
  local query="$1"
  curl -sS --max-time 3 -G "$PROM/api/v1/query" --data-urlencode "query=$query" || true
}

prom_value() {
  local query="$1"
  local s
  s="$(prom_query "$query")"
  "$PY" - <<'PYIN' "$s"
import json,sys
s=sys.argv[1].strip()
if not s:
    print("")
    raise SystemExit(0)
try:
    o=json.loads(s)
except Exception:
    print("")
    raise SystemExit(0)

res=o.get("data",{}).get("result",[])
if not res:
    print("")
else:
    v=res[0].get("value",[None,""])
    print(v[1] if len(v)>1 else "")
PYIN
}


router_backend() {
  local out
  out="$(curl_json "${ROUTER_ADMIN}/admin/backend" 2>/dev/null)" || return 1
  "$PY" - <<'PY' "$out"
import json,sys
print(json.loads(sys.argv[1]).get("backend",""))
PY
}

health() {
  CURL_MAXTIME="${CURL_HEALTH_MAXTIME:-5}" curl -fsS --connect-timeout "${CURL_CONNECT_TIMEOUT:-1}" --max-time "${CURL_HEALTH_MAXTIME:-5}" \
    "$1/health" 2>/dev/null || echo ""
}

router_set_backend() {
  local backend="$1"
  curl -s -X POST "$ROUTER_ADMIN/admin/backend" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"backend\":\"$backend\"}" >/dev/null
}

#admin_load() {
#  local base="$1"; local model_uri="$2"; local delay_ms="$3"
#  curl -s -X POST "$base/admin/load_model" \
#    -H "Authorization: Bearer $TOKEN" \
#    -H "Content-Type: application/json" \
#    -d "{\"model_uri\":\"$model_uri\",\"delay_ms\":$delay_ms}"
#}

admin_load() {
  local base="$1"; local model_uri="$2"; local delay_ms="$3"
  echo "${DIM}POST ${base}/admin/load_model (delay_ms=${delay_ms})${RESET}" >&2
  curl -fsS --connect-timeout 2 --max-time 180 \
    -X POST "$base/admin/load_model" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"model_uri\":\"$model_uri\",\"delay_ms\":$delay_ms}"
}
hr() { echo "${GRAY}--------------------------------------------------------------------------------${RESET}"; }

banner() {
  hr
  echo "${BOLD}${CYAN}$1${RESET}"
  hr
}

dash_once() {
  local b; b="$(router_backend || true)"
  local rh; rh="$(health "$rms_url" || echo '{}')"
  local gh; gh="$(health "$gpu_url" || echo '{}')"

  local rms_model rms_delay gpu_model gpu_delay
  rms_model="$(echo "$rh" | json_get model_uri)"
  rms_delay="$(echo "$rh" | json_get delay_ms)"
  gpu_model="$(echo "$gh" | json_get model_uri)"
  gpu_delay="$(echo "$gh" | json_get delay_ms)"

  local rms_up rms_rps rms_p95 gpu_up gpu_rps gpu_p95
  rms_up="$(prom_value "$(q_up "$job_rms")")"
  rms_rps="$(prom_value "$(q_rps "$job_rms")")"
  rms_p95="$(prom_value "$(q_p95 "$job_rms")")"
  gpu_up="$(prom_value "$(q_up "$job_gpu")")"
  gpu_rps="$(prom_value "$(q_rps "$job_gpu")")"
  gpu_p95="$(prom_value "$(q_p95 "$job_gpu")")"

  printf "${BOLD}router${RESET} backend: %s\n" "${b:-<none>}"
  printf "  ${BOLD}rms${RESET}  up=%-4s rps=%-10s p95_ms=%-8s | model=%s delay_ms=%s\n" \
    "${rms_up:-}" "${rms_rps:-}" "${rms_p95:-None}" "${rms_model:-}" "${rms_delay:-}"
  printf "  ${BOLD}gpu${RESET}  up=%-4s rps=%-10s p95_ms=%-8s | model=%s delay_ms=%s\n" \
    "${gpu_up:-}" "${gpu_rps:-}" "${gpu_p95:-None}" "${gpu_model:-}" "${gpu_delay:-}"
}

watch_dash() {
  local seconds="$1"
  local end=$(( $(date +%s) + seconds ))

  local prev_backend="" prev_rms_model="" prev_gpu_model=""

  while [ "$(date +%s)" -lt "$end" ]; do
    local b rh gh
    b="$(router_backend || true)"
    rh="$(health "$rms_url" || echo '{}')"
    gh="$(health "$gpu_url" || echo '{}')"

    local rms_model gpu_model
    rms_model="$(echo "$rh" | json_get model_uri)"
    gpu_model="$(echo "$gh" | json_get model_uri)"

    clear || true
    echo "${BOLD}${CYAN}AURA Live Demo Dashboard${RESET}  ${DIM}(SLO=${slo_ms}ms min_rps=${min_rps})${RESET}"
    hr
    dash_once
    hr

    if [ -n "$prev_backend" ] && [ "$b" != "$prev_backend" ]; then
      echo "${GREEN}✔ router backend changed:${RESET} ${prev_backend}  ->  ${b}"
    fi
    if [ -n "$prev_rms_model" ] && [ "$rms_model" != "$prev_rms_model" ]; then
      echo "${YELLOW}★ RMS model changed:${RESET} ${prev_rms_model}  ->  ${rms_model}"
    fi
    if [ -n "$prev_gpu_model" ] && [ "$gpu_model" != "$prev_gpu_model" ]; then
      echo "${YELLOW}★ GPU model changed:${RESET} ${prev_gpu_model}  ->  ${gpu_model}"
    fi

    prev_backend="$b"
    prev_rms_model="$rms_model"
    prev_gpu_model="$gpu_model"

    sleep 2
  done
}

cleanup() {
  if [ -n "${LOADGEN_PID:-}" ] && kill -0 "$LOADGEN_PID" >/dev/null 2>&1; then
    kill "$LOADGEN_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

usage() {
  cat <<EOF
Usage:
  ./demo.sh preflight
  ./demo.sh migrate      # RMS slow -> daemon should move router to GPU under load
  ./demo.sh downgrade    # Force "no bigger node", daemon should downgrade model (if supported)
  ./demo.sh watch [sec]  # Just dashboard

Environment overrides:
  DURATION=60 CONC=6 QPS=3 BATCH=1
EOF
}

cmd="${1:-}"
case "$cmd" in
  preflight)
    banner "Preflight"
    echo "router_public: $ROUTER_PUBLIC"
    echo "router_admin : $ROUTER_ADMIN"
    echo "prometheus  : $PROM"
    echo "rms_url     : $rms_url"
    echo "gpu_url     : $gpu_url"
    echo "models: full=$model_full fast=$model_fast quant=$model_quant"
    echo "PROMQL rms up : up{job=\"$job_rms\"}"
    echo "PROMQL gpu up : up{job=\"$job_gpu\"}"
    echo
    dash_once
    ;;
  watch)
    secs="${2:-60}"
    watch_dash "$secs"
    ;;
  migrate)
    : "${DURATION:=60}"
    : "${CONC:=6}"
    : "${QPS:=2}"
    : "${BATCH:=1}"

    banner "Scenario: migrate (RMS slow -> GPU) while daemon runs"

    echo "${CYAN}Step 1:${RESET} Set node models + delays"
    admin_load "$rms_url" "$model_full" 40 >/dev/null
    admin_load "$gpu_url" "$model_full" 0  >/dev/null

    echo "${CYAN}Step 2:${RESET} Route router -> RMS"
    router_set_backend "$rms_url"
    sleep 1
    dash_once
    echo

    echo "${CYAN}Step 3:${RESET} Start loadgen through router for ${DURATION}s (daemon should react)"
    "$PY" "$HERE/loadgen.py" \
      --router "$ROUTER_PUBLIC" \
      --path "${AURA_DEMO_PATH:-/predict}" \
      --prompt "${AURA_DEMO_PROMPT:-Summarize and flag anomalies: Alice paid Bob 3200€ then cash withdrawal same day.}" \
      --max-tokens "${AURA_DEMO_MAX_TOKENS:-96}" \
      --duration "$DURATION" \
      --concurrency "$CONC" \
      --qps "$QPS" \
      --batch "$BATCH" &
    LOADGEN_PID="$!"

    echo "${CYAN}Step 4:${RESET} Live dashboard (watch backend/model changes)"
    watch_dash "$DURATION"

    echo
    banner "Final status"
    dash_once
    ;;
  downgrade)
    : "${DURATION:=60}"
    : "${POST_DOWNGRADE:=120}"
    : "${CONC:=6}"
    : "${QPS:=2}"
    : "${BATCH:=1}"
    TOTAL=$((DURATION + POST_DOWNGRADE))

    banner "Scenario: downgrade model when no bigger node exists (daemon must support variant downgrade)"

    echo "${CYAN}Step 0:${RESET} Temporarily make RMS non-viable (so daemon can’t migrate away)"
    cp -f "$CFG_INV" "$CFG_INV.bak"
    "$PY" - <<PY
import yaml
p="$CFG_INV"
d=yaml.safe_load(open(p)) or {}
d.setdefault("nodes",{}).setdefault("rms",{}).__setitem__("supports",[])
open(p,"w").write(yaml.safe_dump(d, sort_keys=False))
PY
    trap 'mv -f "$CFG_INV.bak" "$CFG_INV"; cleanup' EXIT

    echo "${CYAN}Step 1:${RESET} Put router on GPU and induce high latency on GPU (delay_ms=80)"
    admin_load "$gpu_url" "$model_full" 80 >/dev/null
    router_set_backend "$gpu_url"
    sleep 1
    dash_once
    echo

    echo "${CYAN}Step 2:${RESET} Start loadgen through router for ${DURATION}s"
    "$PY" "$HERE/loadgen.py" \
      --router "$ROUTER_PUBLIC" \
      --path "${AURA_DEMO_PATH:-/predict}" \
      --prompt "${AURA_DEMO_PROMPT:-Summarize and flag anomalies: Alice paid Bob 3200€ then cash withdrawal same day.}" \
      --max-tokens "${AURA_DEMO_MAX_TOKENS:-96}" \
      --duration "$TOTAL" \
      --concurrency "$CONC" \
      --qps "$QPS" \
      --batch "$BATCH" &
    LOADGEN_PID="$!"

    echo "${CYAN}Step 3:${RESET} Live dashboard (expect model change on GPU if daemon downgrades)"
    watch_dash "$TOTAL"

    echo
    banner "Final status"
    dash_once
    ;;
  *)
    usage
    exit 1
    ;;
esac


