#!/usr/bin/env python3
import argparse
import json
import os
import sys
import textwrap
from collections import deque
from datetime import datetime

# --- helpers ---------------------------------------------------------------

def _abbr_backend(url: str | None) -> str:
    if not url:
        return "-"
    u = url.rstrip("/")
    if "137.194.194.122" in u:
        return "rms"
    if "10.194.12.1" in u:
        return "gpu"
    return u.replace("http://", "").replace("https://", "")

def _get(dct, path, default=None):
    cur = dct
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def _parse_ts_iso(ts_iso: str | None) -> str:
    if not ts_iso:
        return "?" * 19
    try:
        # keep it short: "YYYY-MM-DD HH:MM:SS"
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts_iso[:19].replace("T", " ")

def _wrap(s: str, width: int, indent: str) -> str:
    return "\n".join(textwrap.wrap(s, width=width, subsequent_indent=indent, initial_indent=indent))

def _pick_metric(kind: str | None, details: dict) -> tuple[str, str]:
    """
    Returns (metric_label, metric_value_str)
    """
    if not isinstance(details, dict):
        details = {}

    if kind == "needs_migration":
        v = details.get("rms_p95_ms")
        return ("rms_p95", f"{v:.2f}ms" if isinstance(v, (int, float)) else "-")
    if kind == "rollback_needed":
        v = details.get("gpu_p95_ms")
        return ("gpu_p95", f"{v:.2f}ms" if isinstance(v, (int, float)) else "-")
    if kind == "latency_high":
        v = details.get("p95_ms")
        return ("p95", f"{v:.2f}ms" if isinstance(v, (int, float)) else "-")
    if kind == "service_down":
        v = details.get("up_value")
        return ("up", f"{v}" if v is not None else "-")
    return ("metric", "-")

def _pick_util(details: dict) -> str:
    if not isinstance(details, dict):
        return "-"
    v = details.get("gpu_util_pct")
    return f"{v:.0f}%" if isinstance(v, (int, float)) else "-"

# --- main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Pretty viewer for AURA trace.jsonl (multi-line, readable).")
    ap.add_argument("--file", default=os.path.expanduser("~/aura_ops/trace.jsonl"),
                    help="Path to trace.jsonl (default: ~/aura_ops/trace.jsonl)")
    ap.add_argument("--last", type=int, default=60, help="Show last N records (default: 60)")
    ap.add_argument("--width", type=int, default=110, help="Wrap width for detail lines (default: 110)")
    ap.add_argument("--only", default="", help="Comma-separated event kinds or actions to include (e.g. needs_migration,rollback_backend_to_rms)")
    args = ap.parse_args()

    filt = set(x.strip() for x in args.only.split(",") if x.strip())

    # tail safely without loading whole file
    buf = deque(maxlen=args.last)
    try:
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    buf.append(line)
    except FileNotFoundError:
        print(f"ERROR: file not found: {args.file}", file=sys.stderr)
        sys.exit(2)

    # header
    print("=" * args.width)
    print(f"{'TIME':19}  {'EVENT':14}  {'ACTION':24}  {'BACKEND':7}  {'METRIC':10}  {'GPU':4}  {'OK'}")
    print("-" * args.width)

    for line in buf:
        try:
            t = json.loads(line)
        except Exception:
            continue

        kind = _get(t, ["event", "kind"]) or "-"
        action = _get(t, ["decision", "action"]) or "-"
        ok = _get(t, ["execution", "ok"])
        ok_s = "yes" if ok is True else ("no" if ok is False else "-")

        # filtering
        if filt and (kind not in filt and action not in filt):
            continue

        ts_iso = (_get(t, ["decision", "ts_iso"])
                  or _get(t, ["event", "ts_iso"])
                  or _get(t, ["execution", "ts_iso"]))
        ts = _parse_ts_iso(ts_iso)

        details = _get(t, ["event", "details"], {}) or {}
        metric_k, metric_v = _pick_metric(kind, details)
        util = _pick_util(details)

        # backend best-effort
        observed_backend = _get(t, ["execution", "verification", "observed_backend"])
        current_backend = details.get("current_backend") if isinstance(details, dict) else None
        backend = _abbr_backend(observed_backend or current_backend)

        # line 1: fixed columns
        print(f"{ts:19}  {kind[:14]:14}  {action[:24]:24}  {backend[:7]:7}  {metric_k+':':6}{metric_v:>4}  {util:>4}  {ok_s}")

        # line 2+: wrapped rationale/evidence (indented)
        rationale = _get(t, ["decision", "rationale"], {}) or {}
        evidence = _get(t, ["diagnosis", "evidence"], {}) or {}
        because = rationale.get("because") or ""
        policy = rationale.get("policy") or ""
        reason = evidence.get("reason") or details.get("reason") or ""

        bits = []
        if policy:
            bits.append(f"policy={policy}")
        if because:
            bits.append(f"because={because}")
        if reason:
            bits.append(f"reason={reason}")

        # include key thresholds if present
        if isinstance(details, dict):
            if "latency_slo_ms" in details:
                bits.append(f"slo={details.get('latency_slo_ms')}ms")
            if "gpu_util_threshold" in details:
                bits.append(f"gpu_thr={details.get('gpu_util_threshold')}%")

        # include cooldown seconds if present
        if isinstance(rationale, dict) and "seconds_since_last" in rationale:
            try:
                bits.append(f"since_last={float(rationale['seconds_since_last']):.1f}s")
            except Exception:
                bits.append(f"since_last={rationale['seconds_since_last']}")

        if bits:
            msg = " | ".join(bits)
            print(_wrap(msg, width=args.width, indent="    "))

        # show error if execution had one
        verr = _get(t, ["execution", "verification", "error"])
        if verr:
            print(_wrap(f"error={verr}", width=args.width, indent="    "))

        print()  # blank line between records

if __name__ == "__main__":
    main()
