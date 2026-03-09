import os
import time
import threading
from typing import List, Optional

import mlflow
import mlflow.pyfunc
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import json
import urllib.request
import urllib.error
import urllib.parse


MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://137.194.194.21:5000")

MODEL_VERSION = os.getenv("AURA_MODEL_VERSION", "1")
DEFAULT_MODEL_URI = os.getenv("AURA_MODEL_URI", f"models:/aura_week3_model/{MODEL_VERSION}")
DEFAULT_DELAY_MS = int(os.getenv("AURA_DELAY_MS", "0"))

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
_current_ollama_model: Optional[str] = None
_current_ollama_opts: dict = {}

DEMO_FORCE_DELAY_ON_V1 = os.getenv("AURA_DEMO_FORCE_DELAY_ON_V1", "0") == "1"
if DEMO_FORCE_DELAY_ON_V1 and DEFAULT_DELAY_MS == 0 and MODEL_VERSION == "1":
    DEFAULT_DELAY_MS = 40

ADMIN_TOKEN = os.getenv("AURA_INFER_ADMIN_TOKEN", "")

app = FastAPI(title="AURA Inference Service", version="0.4")

model = None
_current_model_uri: Optional[str] = None
_current_delay_ms: int = 0
_load_lock = threading.Lock()

REQS = Counter("inference_requests_total", "Total inference requests")
ERRS = Counter("inference_errors_total", "Total inference errors")
LAT = Histogram("inference_latency_seconds", "Inference latency in seconds")


class PredictRequest(BaseModel):
    inputs: List[List[float]]


class LoadModelRequest(BaseModel):
    model_uri: str
    delay_ms: Optional[int] = None

class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None

def _require_admin(req: Request):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="AURA_INFER_ADMIN_TOKEN not configured")
    auth = req.headers.get("authorization", "")
    if auth != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")



def _load_model(model_uri: str, delay_ms: Optional[int] = None):
    global model, _current_model_uri, _current_delay_ms, _current_ollama_model, _current_ollama_opts
    with _load_lock:
        if model_uri.startswith("ollama:"):
            mname, opts = _parse_ollama_uri(model_uri)
            _current_ollama_model = mname
            _current_ollama_opts = opts
            _current_model_uri = model_uri
        else:
            mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
            m = mlflow.pyfunc.load_model(model_uri)
            model = m
            _current_model_uri = model_uri

        if delay_ms is not None:
            _current_delay_ms = int(delay_ms)

def _parse_ollama_uri(uri: str):
    # uri like: ollama:llama3.2:1b?num_predict=128&temperature=0.2
    s = uri[len("ollama:"):]
    if "?" in s:
        model_name, q = s.split("?", 1)
        params = urllib.parse.parse_qs(q)
    else:
        model_name, params = s, {}
    opts = {}
    def one(k, cast):
        if k in params and params[k]:
            try: opts[k] = cast(params[k][0])
            except Exception: pass
    one("temperature", float)
    one("top_p", float)
    one("num_predict", int)
    return model_name.strip(), opts

def _ollama_generate(model_name: str, prompt: str, opts: dict):
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "options": opts or {},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("response", "")

@app.on_event("startup")
def startup():
    global _current_delay_ms
    _current_delay_ms = DEFAULT_DELAY_MS
    _load_model(DEFAULT_MODEL_URI, delay_ms=_current_delay_ms)
    print(f"Loaded model: {_current_model_uri} delay_ms={_current_delay_ms}")


@app.get("/health")
def health():
    return {"status": "ok", "model_uri": _current_model_uri, "delay_ms": _current_delay_ms}


@app.get("/admin/status")
def admin_status(req: Request):
    _require_admin(req)
    return {"model_uri": _current_model_uri, "delay_ms": _current_delay_ms}


@app.post("/admin/load_model")
def admin_load_model(req: Request, body: LoadModelRequest):
    _require_admin(req)
    if not (body.model_uri.startswith("models:/") or body.model_uri.startswith("ollama:")):
        raise HTTPException(status_code=400, detail="model_uri must start with models:/ or ollama:")
    new_delay = _current_delay_ms if body.delay_ms is None else int(body.delay_ms)
    _load_model(body.model_uri, delay_ms=new_delay)
    return {"ok": True, "model_uri": _current_model_uri, "delay_ms": _current_delay_ms}


@app.post("/predict")
def predict(req: PredictRequest):
    global model
    REQS.inc()
    start = time.time()
    try:
        if _current_delay_ms > 0:
            time.sleep(_current_delay_ms / 1000.0)

        df = pd.DataFrame(req.inputs, columns=["f1", "f2", "f3", "f4"])
        preds = model.predict(df)
        return {"predictions": [int(x) for x in preds]}
    except Exception:
        ERRS.inc()
        raise
    finally:
        LAT.observe(time.time() - start)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/generate")
def generate(req: GenerateRequest):
    REQS.inc()
    start = time.time()
    try:
        if _current_delay_ms > 0:
            time.sleep(_current_delay_ms / 1000.0)

        # pick active ollama model if set, otherwise default
        if _current_model_uri and _current_model_uri.startswith("ollama:"):
            model_name, base_opts = _parse_ollama_uri(_current_model_uri)
        else:
            model_name = os.getenv("AURA_OLLAMA_DEFAULT_MODEL", "llama3.2:1b")
            base_opts = {}

        opts = dict(base_opts)
        # allow request overrides (optional)
        if req.max_tokens is not None: opts["num_predict"] = int(req.max_tokens)
        if req.temperature is not None: opts["temperature"] = float(req.temperature)
        if req.top_p is not None: opts["top_p"] = float(req.top_p)

        text = _ollama_generate(model_name, req.prompt, opts)
        return {"text": text, "model": model_name, "opts": opts}
    except Exception:
        ERRS.inc()
        raise
    finally:
        LAT.observe(time.time() - start)
