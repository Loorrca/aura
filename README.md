# AURA — Closed-Loop Inference Routing, Downgrade, and Observability

## Executive summary

AURA is a distributed inference demo platform built to demonstrate three ideas end to end:

1. **Stable public inference endpoint** behind a router.
2. **Closed-loop control** that reacts to real metrics and automatically migrates traffic or downgrades the active model.
3. **Observable operations** through Prometheus, Grafana, and trace files that explain *why* the system made a decision.

The project started as an MLflow/iris-based experiment and evolved into a more realistic, LLM-backed demo using **Ollama** model variants routed through the same architecture.

The final system is composed of:

- **prim-core**: public router (`aura-router.service`)
- **prim-monitor**: Prometheus + Grafana
- **prim-agent**: closed-loop control agent (`aura-ops.service`) and demo tooling
- **rms**: inference service on a Grace/Blackwell-class machine
- **gpu**: inference service on an RTX-class machine, optionally exposed through relays/tunnels when direct networking is not available

The repository is now structured to make the architecture portable and reproducible instead of hard-wired to one lab network.

---

## What the project demonstrates

AURA is not just a model server. It is a small **autonomic inference system**:

- the **router** exposes a stable endpoint to clients,
- the **inference services** expose health, admin, and Prometheus metrics,
- the **agent** polls Prometheus and state, diagnoses system conditions, chooses a policy action, and executes it,
- the **monitoring stack** shows the outcome live.

The two core demo scenarios are:

### 1. Migration
If the active backend violates the latency SLO under load, and a more capable node is available, the agent:
- pre-loads the target backend with the same model variant,
- verifies health,
- switches the router backend,
- updates state and traces.

### 2. Downgrade
If the active backend violates the latency SLO, but no more capable node is available, the agent:
- keeps the same backend,
- selects a smaller/faster variant,
- hot-swaps the model on the active node,
- verifies health and records the action.

---

## Repository layout

```text
AURA/
├── router/
│   └── router.py
├── infer/
│   ├── rms/
│   │   ├── app.py
│   │   ├── control.sh
│   │   └── model_version.txt
│   └── gpu/
│       ├── app.py
│       ├── control.sh
│       └── model_version.txt
├── ops/
│   ├── aura_ops.py
│   ├── demo.py
│   ├── demo.sh
│   ├── loadgen.py
│   ├── configs/
│   │   ├── inventory.yaml
│   │   ├── models.yaml
│   │   └── policy.yaml
│   ├── state/
│   └── tools/
├── monitoring/
│   ├── docker-compose.yml
│   └── prometheus/
│       └── prometheus.yml
├── scripts/
│   └── aura_init.py
├── Makefile
└── .gitignore
```

---

## Architecture overview

### Control plane

**prim-agent** runs the closed-loop agent (`ops/aura_ops.py`). It:
- loads the configuration files,
- queries Prometheus,
- polls router and backend health,
- decides whether to restart, migrate, or downgrade,
- executes the selected action,
- writes `state/last_trace.json` and `state/trace.jsonl`.

### Data plane

**prim-core** runs the public FastAPI router (`router/router.py`). It:
- forwards `/predict` and `/generate` to the active backend,
- exposes `/health`, `/metrics`, `/admin/backend`,
- keeps the currently selected backend in memory,
- exports router request/error/latency metrics.

### Compute plane

Two inference nodes expose the same service contract:

- `rms` (`infer/rms/app.py`)
- `gpu` (`infer/gpu/app.py`)

Each backend provides:
- `/health`
- `/admin/status`
- `/admin/load_model`
- `/predict`
- `/generate`
- `/metrics`

### Observability plane

**prim-monitor** runs Prometheus and Grafana from `monitoring/docker-compose.yml` and scrapes:
- Prometheus itself
- both inference nodes
- the router

---

## Endpoints and service contract

### Router endpoints

Implemented in `router/router.py`.

- `GET /health` → router state + current backend health
- `POST /predict` → proxy to `BACKEND/predict`
- `POST /generate` → proxy to `BACKEND/generate`
- `GET /metrics` → Prometheus metrics
- `GET /admin/backend` → current backend
- `POST /admin/backend` → switch active backend

This is a deliberately thin component. It does not contain policy logic. It only exposes a stable public entrypoint and a small admin API.

### Inference endpoints

Implemented in `infer/*/app.py`.

- `GET /health`
- `GET /admin/status`
- `POST /admin/load_model`
- `POST /predict`
- `POST /generate`
- `GET /metrics`

The service supports **two model loading modes**:

1. **MLflow mode** for `models:/...` URIs
2. **Ollama mode** for `ollama:...` URIs

This design lets the same control loop operate across both classical and LLM-backed demos without changing the control logic.

---

## Router code walkthrough

File: `router/router.py`

### Key ideas

- `BACKEND` is an in-memory URL representing the active backend.
- `_set_backend()` updates the active backend and a `router_backend` gauge.
- `TIMEOUT` is controlled by `AURA_BACKEND_TIMEOUT`.
- the admin token is validated by `_require_admin()`.

### Request handling

- `/predict` and `/generate` are almost identical:
  - they read the inbound request body,
  - forward the request to the current backend using `httpx.AsyncClient`,
  - observe latency,
  - increment error counters on backend failure,
  - return the backend JSON response.

### Why this router is useful

It keeps the client-facing endpoint stable while the agent can freely change the active backend behind it.

---

## Inference service code walkthrough

File: `infer/rms/app.py` and `infer/gpu/app.py`

Both files share the same application contract.

### Boot-time configuration

Key environment variables:

- `MLFLOW_TRACKING_URI`
- `AURA_MODEL_VERSION`
- `AURA_MODEL_URI`
- `AURA_DELAY_MS`
- `AURA_INFER_ADMIN_TOKEN`
- `OLLAMA_URL`
- `AURA_OLLAMA_DEFAULT_MODEL`

### State

Global runtime state tracks:
- loaded MLflow model (`model`)
- active model URI (`_current_model_uri`)
- current artificial delay (`_current_delay_ms`)
- active Ollama model and options (`_current_ollama_model`, `_current_ollama_opts`)

### Model loading logic

`_load_model()` is the key function:

- if `model_uri` starts with `ollama:`, it parses and stores the Ollama model name/options;
- otherwise it treats the URI as an MLflow model and loads it with `mlflow.pyfunc.load_model()`.

This is the pivot that turned the demo from a dummy classifier into a useful prompt-based AI demo.

### `/predict`

Legacy structured inference endpoint:
- accepts iris-like numeric features,
- uses the loaded MLflow pyfunc model,
- records counters and histogram latency.

### `/generate`

Prompt-based LLM endpoint:
- accepts a text prompt plus optional generation controls,
- chooses the active Ollama model (or a default one),
- calls the local Ollama HTTP API,
- records the same counters and histogram latency.

### `/admin/load_model`

This is what the agent and demo scripts use to change behavior without restarting the process.

---

## Agent code walkthrough

File: `ops/aura_ops.py`

This file contains the full closed-loop controller.

### 1. Configuration layer

The agent reads three YAML files every loop:
- `inventory.yaml`
- `models.yaml`
- `policy.yaml`

This is important: the system behavior is mostly data-driven.

### 2. Observability agent

`observability_agent()` collects evidence:
- current router backend
- current backend node mapping
- current model URI from router health
- Prometheus signals: `up`, `rps`, `p95`
- optional GPU utilization through SSH

It only emits the key event `active_backend_over_slo` if:
- the active backend latency exceeds the SLO
- AND the active backend has enough traffic to make a decision meaningful.

### 3. Diagnosis agent

`diagnosis_agent()` reduces raw events to a high-level hypothesis:
- `service_down` → `inference_service_unreachable`
- `active_backend_over_slo` → `active_over_slo`

### 4. Decision agent

`decision_agent()` applies policy:

- if the active backend is over SLO and a **bigger** candidate node exists, choose `set_backend`
- otherwise choose `set_variant_on_node` to downgrade the active model variant
- enforce cooldowns (`restart_seconds`, `switch_seconds`, `migrate_seconds`)
- validate support lists (`supports` per node)

### 5. Execution agent

`execution_agent()` performs the chosen action:

- `restart_inference`
- `switch_model_version` (legacy path)
- `set_variant_on_node`
- `set_backend`

The most important action is `set_backend`:
1. hot-swap the desired model on the target node,
2. verify target health and loaded model URI,
3. switch the router backend,
4. verify backend selection,
5. persist state.

### 6. Explanation agent

Every loop writes a structured trace to:
- `state/last_trace.json`
- `state/trace.jsonl`

This makes the system explainable during the defense.

---

## Demo tooling

### `ops/demo.sh`

Main scenario runner.

Provides:
- `preflight`
- `migrate`
- `downgrade`
- `watch`

It loads the same config files as the agent, sets up models and delays, calls router admin endpoints, starts load generation, and renders a terminal dashboard.

### `ops/loadgen.py`

Load generator that now supports both:
- `/predict`
- `/generate`

Key options:
- `--path`
- `--prompt`
- `--max-tokens`
- `--duration`
- `--concurrency`
- `--qps`
- `--timeout`

This is what allowed the project to evolve from numeric test traffic to a useful LLM prompt demo.

### `ops/demo.py`

Standalone scripted demonstration runner that uses the same production control loop (`import aura_ops`) to reproduce scenarios in a more controlled way.

---

## Monitoring stack

### Docker Compose

`monitoring/docker-compose.yml` starts:
- Prometheus on port `9090`
- Grafana on port `3000`

### Prometheus

Prometheus scrapes:
- both inference jobs
- the router
- itself

The most important signals for the control loop are:
- `inference_requests_total`
- `inference_latency_seconds_bucket`
- `up{job=...}`

The current policy uses these templates:

- **RPS**
  - `sum(rate(inference_requests_total{job="{{job}}"}[2m]))`
- **p95 latency**
  - `1000 * histogram_quantile(0.95, sum by (le) (rate(inference_latency_seconds_bucket{job="{{job}}"}[30s])))`

### Grafana

Grafana is used to show the live effect of migration and downgrade:
- request rate
- latency p95 / p99
- backend changes
- error rates

---

## Configuration files explained

### `ops/configs/inventory.yaml`
Describes topology:
- router URLs
- Prometheus URL
- node infer URLs
- node Prometheus jobs
- SSH information
- supported variants
- default delay values

### `ops/configs/models.yaml`
Defines semantic variants:
- `full`
- `fast`
- `quant`

These are not merely names. They are the *actuation targets* used by the agent and demo scripts.

### `ops/configs/policy.yaml`
Defines operational policy:
- loop interval
- cooldowns
- verification delays/timeouts
- SLO thresholds
- GPU utilization thresholds
- PromQL templates
- desired default variant

This file is the heart of the policy layer.

---

## Demo scenarios in detail

### Migration scenario

`./demo.sh migrate`

Flow:
1. load the same variant on both backends,
2. add extra delay on RMS,
3. route the router to RMS,
4. start load generation,
5. agent detects SLO violation,
6. agent hot-swaps target backend if needed,
7. agent switches router backend to GPU,
8. latency and error rate improve.

### Downgrade scenario

`./demo.sh downgrade`

Flow:
1. make RMS non-viable for migration,
2. route traffic to GPU,
3. make GPU slow with additional delay,
4. start load generation,
5. agent detects SLO violation,
6. no bigger node is eligible,
7. agent downgrades the variant on GPU,
8. latency drops while backend stays the same.

---

## What was fixed during the project

This project was not only about building the architecture; it was also about debugging and hardening it.

Important issues solved during integration:

- hidden stale IPs (`137.194.194.66`) in service code and configs
- Prometheus targets down due to wrong targets / unreachable private IPs
- need for a GPU relay/tunnel in the school network architecture
- broken JSON parsing in shell pipelines (`python - <<EOF` stdin conflict)
- closed-loop hang due to SSH without timeout
- router lacking `/generate` proxy route
- loadgen incompatibility with LLM prompts and empty `--max-tokens`
- systemd restart loops caused by port conflicts
- MLflow artifact lookup issues with remote nodes
- migration and downgrade thresholds not visible without enough sustained load

These debugging steps are part of the story and should be presented to the jury as engineering work, not as noise.

---

## Portable deployment philosophy

The current school deployment used relays and tunnels because some nodes were not directly reachable.

The repository is now organized so that the **portable/default assumption** is simpler:
- all nodes are reachable on the same network,
- configuration is generated interactively,
- deployment uses SSH and systemd,
- optional MLflow support can be kept as a separate component.

The repository already contains:
- `scripts/aura_init.py` to ask for URLs, jobs, SSH info, and generate configs
- `Makefile` with basic scenario targets

The next logical step is a deployment script or Ansible playbook that:
- copies code to the right hosts,
- writes `/etc/aura/*.env`,
- installs systemd units,
- starts services.

---

## Suggested GitHub workflow

1. keep `router/`, `infer/`, `ops/`, `monitoring/`, `scripts/` in a single repo
2. never commit generated state (`ops/state/`, `grafana/`, `prometheus/data/`, `.venv`, logs)
3. keep `inventory.yaml`, `models.yaml`, `policy.yaml` as editable runtime config
4. keep secrets in environment files or GitHub Actions secrets, not in repo
5. provide a short `README` quickstart and a longer architecture document

---

## Demo runbook (jury/soutenance)

### Pre-demo
- verify both inference services are up
- verify router backend is reachable
- verify Prometheus scrape targets are up
- warm up the Ollama models once
- verify Grafana dashboard refreshes correctly

### Live sequence
1. explain architecture slide
2. run `./demo.sh preflight`
3. send one prompt manually through the router (`/generate`)
4. run `./demo.sh migrate` with LLM load
5. show router backend change and Grafana drop
6. run `./demo.sh downgrade`
7. show model change on the active backend and the latency drop
8. conclude with design trade-offs and future work

---

## Strengths of the design

- simple, inspectable components
- strong separation between data plane and control plane
- explainable decisions via JSON traces
- same operational model for classical ML and LLM-backed demos
- configuration-driven rather than hardcoded topology
- live observability with Grafana and Prometheus

## Current limitations

- in-memory router backend selection (not persisted outside process)
- direct systemd/SSH orchestration instead of a full declarative deploy tool
- some network workarounds still exist for non-flat networks
- MLflow artifact portability not fully solved for remote workers
- `capacity_rank` default heuristic still assumes `gpu > rms`

---

## Recommended future work

1. formalize deployment with Ansible or a richer deploy script
2. replace manual relay/tunnel setup with a direct network or remote-write approach
3. persist router state externally
4. add structured labels to metrics (endpoint, variant, backend) for finer dashboards
5. make the agent compare candidate quality/performance, not only capacity rank
6. support canary rollout and rollback, not only switch/downgrade
7. fully decouple LLM model config from MLflow model config

---

## Conclusion

AURA demonstrates a full MLOps/LLMOps control loop in a compact but realistic environment:
- configurable topology,
- stable router entrypoint,
- hot-swappable backends and model variants,
- policy-driven migration and downgrade,
- traceable and observable behavior.

It is not just a collection of services. It is a **closed-loop adaptive inference platform** with a clear architecture, readable code, and a demonstrable operational story.
