#!/usr/bin/env python3
"""Append-only forward shadow ledger for frozen Regime Hybrid v2.

This module does NOT backfill decisions. A run may only append a UTC day >= the
ledger start date and > the last recorded UTC day. Strategy inputs are supplied
by a signal adapter; execution is dry-run through bybit_execution_engine.

Production deployment should run this after the previous UTC daily candle has
closed. Missing/stale venue data => CASH/NO_ENTRY, never inferred/backfilled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bybit_execution_engine import D, EngineConfig, ExecutionEngine

STRATEGY_NAME = "Regime Hybrid Cap2 v2 Cross-Venue Consensus"
FORWARD_START_UTC = "2026-09-02"
FIRST_CLEAN_REBALANCE_UTC = "2026-10-01"
MIN_COMPLETE_MONTHS = 3
MIN_CALENDAR_DAYS = 90


@dataclass(frozen=True)
class ShadowPolicy:
    forward_start_utc: str = FORWARD_START_UTC
    first_clean_rebalance_utc: str = FIRST_CLEAN_REBALANCE_UTC
    min_complete_months: int = MIN_COMPLETE_MONTHS
    min_calendar_days: int = MIN_CALENDAR_DAYS
    weight_per_slot: str = "0.20"
    cap: int = 2
    entry_tranches: int = 3
    max_adverse_drift_bps: str = "10"
    max_total_slippage_bps: str = "15"
    long_guard: str = "0.12"
    short_guard: str = "0.25"
    veto_closes: int = 3


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def sha256_file(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strategy_fingerprint() -> dict[str, str]:
    files = ["regime_hybrid_v2_crossvenue.py", "regime_hybrid_v1_pit.py", "bybit_execution_engine.py", "forward_shadow.py"]
    return {p: sha256_file(p) for p in files if Path(p).exists()}


class Ledger:
    def __init__(self, root: str = "forward_shadow"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.events = self.root / "events.jsonl"
        self.manifest = self.root / "manifest.json"

    def init(self) -> None:
        if self.manifest.exists():
            old = json.loads(self.manifest.read_text(encoding="utf-8"))
            # Fingerprint changes are surfaced, not silently accepted.
            now = strategy_fingerprint()
            if old.get("strategy_fingerprint") != now:
                raise RuntimeError("FROZEN STRATEGY/ENGINE FINGERPRINT CHANGED. Start a new experiment ID; do not overwrite this forward shadow.")
            return
        m = {
            "experiment_id": "RH2-FWD-20260902",
            "strategy": STRATEGY_NAME,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "policy": asdict(ShadowPolicy()),
            "strategy_fingerprint": strategy_fingerprint(),
            "status": "RUNNING",
            "note": "No pre-start decisions may be appended. September 2026 is observation-only because start occurred after Sep monthly entry window.",
        }
        self.manifest.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def rows(self) -> list[dict[str, Any]]:
        if not self.events.exists():
            return []
        out=[]
        for line in self.events.read_text(encoding="utf-8").splitlines():
            if line.strip(): out.append(json.loads(line))
        return out

    def append(self, row: dict[str, Any]) -> None:
        day = row["utc_day"]
        if day < FORWARD_START_UTC:
            raise RuntimeError("Cannot backfill before forward start")
        rows = self.rows()
        if rows and day <= rows[-1]["utc_day"]:
            raise RuntimeError(f"Append-only violation: {day} <= {rows[-1]['utc_day']}")
        prev_hash = rows[-1]["event_hash"] if rows else "GENESIS"
        payload = {**row, "prev_hash": prev_hash}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        payload["event_hash"] = hashlib.sha256(encoded).hexdigest()
        with self.events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def status(self) -> dict[str, Any]:
        rows=self.rows(); start=datetime.fromisoformat(FORWARD_START_UTC).date(); now=datetime.fromisoformat(utc_today()).date()
        days=(now-start).days+1
        clean=[r for r in rows if r.get("phase") == "CLEAN_CYCLE"]
        months=sorted({r["utc_day"][:7] for r in clean})
        complete=[m for m in months if any(x.get("utc_day","").startswith(m) and x.get("month_closed") for x in clean)]
        return {"experiment_id":"RH2-FWD-20260902","calendar_days_elapsed":days,"ledger_days":len(rows),"clean_months_seen":months,"complete_clean_months":complete,"min_calendar_days":MIN_CALENDAR_DAYS,"min_complete_months":MIN_COMPLETE_MONTHS,"gate_passed":days>=MIN_CALENDAR_DAYS and len(complete)>=MIN_COMPLETE_MONTHS}


def load_signal_json(path: str) -> dict[str, Any]:
    x=json.loads(Path(path).read_text(encoding="utf-8"))
    required={"signal_date","data_fresh","venue_a_ok","venue_b_ok","regime","targets"}
    missing=required-set(x)
    if missing: raise RuntimeError(f"Signal adapter missing keys: {sorted(missing)}")
    return x


def phase_for(day: str) -> str:
    return "OBSERVATION_ONLY" if day < FIRST_CLEAN_REBALANCE_UTC else "CLEAN_CYCLE"


def run_day(signal: dict[str,Any], root: str, day: str | None=None) -> dict[str,Any]:
    day=day or utc_today(); ledger=Ledger(root); ledger.init()
    phase=phase_for(day)
    safe=bool(signal["data_fresh"] and signal["venue_a_ok"] and signal["venue_b_ok"])
    intended=[]; simulated=[]
    if phase == "CLEAN_CYCLE" and safe:
        for t in signal.get("targets",[]):
            intended.append(t)
            # Shadow engine does not authenticate. Reference price must be supplied by signal/execution adapter.
            if t.get("execute_today") and t.get("reference_price") and t.get("quote_notional"):
                cfg=EngineConfig(mode="dry-run",journal_path=str(Path(root)/"execution_journal.jsonl"),max_adverse_drift_bps=D(ShadowPolicy().max_adverse_drift_bps),max_total_slippage_bps=D(ShadowPolicy().max_total_slippage_bps))
                eng=ExecutionEngine(cfg)
                oid=eng.make_order_link_id("RH2",day,t["symbol"],int(t.get("tranche",1)),"E")
                side="Buy" if t["side"]=="long" else "Sell"
                guard=D(ShadowPolicy().long_guard if t["side"]=="long" else ShadowPolicy().short_guard)
                try:
                    r=eng.protected_ioc(t["symbol"],side,D(t["quote_notional"]),D(t["reference_price"]),oid,guard)
                    simulated.append(asdict(r))
                except Exception as e:
                    simulated.append({"symbol":t["symbol"],"status":"EXECUTION_ADAPTER_ERROR","error":repr(e)})
    decision = "CASH" if (phase=="OBSERVATION_ONLY" or not safe or signal.get("regime")=="neutral") else "TARGETS"
    row={
        "utc_day":day,
        "recorded_utc":datetime.now(timezone.utc).isoformat(),
        "phase":phase,
        "signal_date":signal["signal_date"],
        "data_fresh":bool(signal["data_fresh"]),
        "venue_a_ok":bool(signal["venue_a_ok"]),
        "venue_b_ok":bool(signal["venue_b_ok"]),
        "regime":signal.get("regime","neutral"),
        "decision":decision,
        "targets":intended,
        "simulated_execution":simulated,
        "month_closed":bool(signal.get("month_closed",False)),
        "adapter_meta":signal.get("adapter_meta",{}),
    }
    ledger.append(row)
    return {"event":row,"status":ledger.status()}


def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["init","run","status"]); p.add_argument("--root",default="forward_shadow"); p.add_argument("--signal-json"); p.add_argument("--utc-day")
    a=p.parse_args(); l=Ledger(a.root)
    if a.command=="init": l.init(); print(json.dumps({"manifest":json.loads(l.manifest.read_text()),"status":l.status()},indent=2,ensure_ascii=False))
    elif a.command=="status": l.init(); print(json.dumps(l.status(),indent=2,ensure_ascii=False))
    else:
        if not a.signal_json: raise SystemExit("--signal-json required")
        print(json.dumps(run_day(load_signal_json(a.signal_json),a.root,a.utc_day),indent=2,ensure_ascii=False))

if __name__=="__main__": main()
