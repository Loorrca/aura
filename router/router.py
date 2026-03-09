import os, time
import httpx
from fastapi import FastAPI, Request, HTTPException
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

# --- config ---
BACKEND = os.environ.get("AURA_BACKEND_URL", "http://137.194.194.122:8000").rstrip("/")
TIMEOUT = float(os.environ.get("AURA_BACKEND_TIMEOUT", "5"))
ADMIN_TOKEN = os.environ.get("AURA_ROUTER_ADMIN_TOKEN", "")  # set in /etc/aura/router.env

app = FastAPI()

req_total = Counter("router_requests_total", "Total requests through router")
err_total = Counter("router_errors_total", "Total router errors")
lat = Histogram("router_latency_seconds", "Router latency", buckets=(.005,.01,.025,.05,.1,.25,.5,1,2.5,5))

# Metric that shows which backend is selected.
# We set only the active backend to 1 and clear others we’ve seen.
router_backend = Gauge("router_backend", "Selected backend (1=active)", ["backend"])
_seen_backends = set()

def _set_backend(url: str):
    global BACKEND
    url = url.rstrip("/")
    BACKEND = url
    _seen_backends.add(url)
    for b in list(_seen_backends):
        router_backend.labels(backend=b).set(1.0 if b == BACKEND else 0.0)

_set_backend(BACKEND)

def _require_admin(req: Request):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="admin token not configured")
    auth = req.headers.get("authorization", "")
    if auth != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")

@app.get("/health")
async def health():
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BACKEND}/health")
        return {"router": "ok", "backend": BACKEND, "backend_health": r.json()}

@app.post("/predict")
async def predict(req: Request):
    body = await req.body()
    req_total.inc()
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(
                f"{BACKEND}/predict",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        lat.observe(time.time() - t0)
        if r.status_code >= 400:
            err_total.inc()
        return r.json()
    except Exception:
        err_total.inc()
        raise

@app.get("/metrics")
def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)

@app.post("/generate")
async def generate(req: Request):
    body = await req.body()
    req_total.inc()
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(
                f"{BACKEND}/generate",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        lat.observe(time.time() - t0)
        if r.status_code >= 400:
            err_total.inc()
        return r.json()
    except Exception:
        err_total.inc()
        raise


# --- Admin API ---
@app.get("/admin/backend")
async def admin_get_backend(req: Request):
    _require_admin(req)
    return {"backend": BACKEND}

@app.post("/admin/backend")
async def admin_set_backend(req: Request):
    _require_admin(req)
    data = await req.json()
    backend = (data.get("backend") or "").strip()
    if not backend.startswith("http"):
        raise HTTPException(status_code=400, detail="backend must be a full URL like http://host:8000")
    # quick sanity: ensure backend /health responds
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{backend.rstrip('/')}/health")
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail="backend health check failed")
    _set_backend(backend)
    return {"ok": True, "backend": BACKEND}
 
