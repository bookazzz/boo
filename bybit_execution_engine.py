#!/usr/bin/env python3
"""Protected Bybit V5 execution engine for Regime Hybrid v2.

Modes:
- dry-run: no authenticated exchange writes
- testnet: private API enabled with BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET
- live: private API enabled only with BYBIT_API_KEY / BYBIT_API_SECRET and
  BYBIT_LIVE_ACK=YES_I_ACCEPT_LIVE_TRADING

Safety contract:
- deterministic orderLinkId (idempotency)
- reconcile before every write; never blind-retry an unknown submit
- IOC LIMIT orders only for entries, with hard price cap/floor from a reference price
- instrument tick/qty/min-notional validation
- partial fills use actual filled quantity
- exchange-native full-position MarkPrice stop after linear fills
- no live fallback from testnet/dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

LIVE_ACK = "YES_I_ACCEPT_LIVE_TRADING"


def D(x: Any) -> Decimal:
    return Decimal(str(x))


def decstr(x: Decimal) -> str:
    s = format(x, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def floor_step(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_step(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass(frozen=True)
class EngineConfig:
    mode: str = "dry-run"
    category: str = "linear"
    recv_window_ms: int = 5000
    max_adverse_drift_bps: Decimal = D("10")
    max_total_slippage_bps: Decimal = D("15")
    reconcile_timeout_sec: float = 12.0
    poll_sec: float = 0.75
    journal_path: str = "execution_journal.jsonl"

    @property
    def base_url(self) -> str:
        return "https://api-testnet.bybit.com" if self.mode == "testnet" else "https://api.bybit.com"


class BybitError(RuntimeError):
    pass


class BybitREST:
    def __init__(self, cfg: EngineConfig, api_key: str = "", api_secret: str = ""):
        self.cfg = cfg
        self.api_key = api_key
        self.api_secret = api_secret
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "regime-hybrid-v2-execution/1.0"})

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        method = method.upper()
        headers: dict[str, str] = {}
        body = ""
        query = ""
        if method == "GET":
            query = urlencode(sorted((k, str(v)) for k, v in params.items()))
        else:
            body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.api_key or not self.api_secret:
                raise BybitError("Authenticated request requires API credentials")
            ts = str(int(time.time() * 1000))
            rw = str(self.cfg.recv_window_ms)
            payload = ts + self.api_key + rw + (query if method == "GET" else body)
            sig = hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            headers.update({
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": rw,
                "X-BAPI-SIGN": sig,
            })
        url = self.cfg.base_url + path
        r = self.s.request(method, url, params=params if method == "GET" else None, data=body if method != "GET" else None, headers=headers, timeout=20)
        if r.status_code != 200:
            raise BybitError(f"HTTP {r.status_code} {path}: {r.text[:300]}")
        try:
            out = r.json()
        except Exception as e:
            raise BybitError(f"Non-JSON response {path}: {e}") from e
        if int(out.get("retCode", 0)) != 0:
            raise BybitError(f"Bybit retCode={out.get('retCode')} retMsg={out.get('retMsg')} path={path}")
        return out

    def server_time(self) -> dict[str, Any]:
        return self._request("GET", "/v5/market/time")

    def ticker(self, symbol: str, category: str | None = None) -> dict[str, Any]:
        out = self._request("GET", "/v5/market/tickers", {"category": category or self.cfg.category, "symbol": symbol})
        xs = out.get("result", {}).get("list", [])
        if not xs:
            raise BybitError(f"No ticker for {symbol}")
        return xs[0]

    def instrument(self, symbol: str, category: str | None = None) -> dict[str, Any]:
        out = self._request("GET", "/v5/market/instruments-info", {"category": category or self.cfg.category, "symbol": symbol})
        xs = out.get("result", {}).get("list", [])
        if not xs:
            raise BybitError(f"No instrument info for {symbol}")
        return xs[0]

    def position(self, symbol: str, category: str | None = None) -> list[dict[str, Any]]:
        out = self._request("GET", "/v5/position/list", {"category": category or self.cfg.category, "symbol": symbol}, auth=True)
        return out.get("result", {}).get("list", [])

    def open_orders(self, symbol: str, category: str | None = None, order_link_id: str | None = None) -> list[dict[str, Any]]:
        out = self._request("GET", "/v5/order/realtime", {"category": category or self.cfg.category, "symbol": symbol, "orderLinkId": order_link_id, "openOnly": 0, "limit": 50}, auth=True)
        return out.get("result", {}).get("list", [])

    def order_history(self, symbol: str, category: str | None = None, order_link_id: str | None = None) -> list[dict[str, Any]]:
        out = self._request("GET", "/v5/order/history", {"category": category or self.cfg.category, "symbol": symbol, "orderLinkId": order_link_id, "limit": 50}, auth=True)
        return out.get("result", {}).get("list", [])

    def executions(self, symbol: str, category: str | None = None, order_link_id: str | None = None) -> list[dict[str, Any]]:
        out = self._request("GET", "/v5/execution/list", {"category": category or self.cfg.category, "symbol": symbol, "orderLinkId": order_link_id, "limit": 100}, auth=True)
        return out.get("result", {}).get("list", [])

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v5/order/create", payload, auth=True)

    def cancel_order(self, symbol: str, order_link_id: str, category: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/v5/order/cancel", {"category": category or self.cfg.category, "symbol": symbol, "orderLinkId": order_link_id}, auth=True)

    def set_trading_stop(self, symbol: str, stop_loss: Decimal, category: str = "linear", position_idx: int = 0) -> dict[str, Any]:
        return self._request("POST", "/v5/position/trading-stop", {
            "category": category,
            "symbol": symbol,
            "tpslMode": "Full",
            "positionIdx": position_idx,
            "stopLoss": decstr(stop_loss),
            "slTriggerBy": "MarkPrice",
        }, auth=True)


class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        row = {"ts": int(time.time() * 1000), **event}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out

    def has_submit(self, order_link_id: str) -> bool:
        return any(e.get("event") == "submit_accepted" and e.get("orderLinkId") == order_link_id for e in self.events())

    def unresolved(self, symbol: str | None = None) -> list[str]:
        status: dict[str, str] = {}
        sym: dict[str, str] = {}
        for e in self.events():
            oid = e.get("orderLinkId")
            if not oid:
                continue
            sym[oid] = e.get("symbol", sym.get(oid, ""))
            if e.get("event") in {"submit_unknown", "submit_accepted"}:
                status[oid] = e.get("event")
            if e.get("event") in {"reconciled", "rejected", "cancelled"}:
                status[oid] = "resolved"
        return sorted(oid for oid, st in status.items() if st != "resolved" and (symbol is None or sym.get(oid) == symbol))


@dataclass
class ProtectedOrderResult:
    status: str
    symbol: str
    side: str
    orderLinkId: str
    reference_price: str
    market_price: str | None = None
    limit_price: str | None = None
    requested_qty: str | None = None
    filled_qty: str = "0"
    avg_price: str | None = None
    reason: str | None = None
    order_id: str | None = None
    stop_loss: str | None = None


class ExecutionEngine:
    def __init__(self, cfg: EngineConfig, client: BybitREST | None = None):
        if cfg.mode not in {"dry-run", "testnet", "live"}:
            raise ValueError("mode must be dry-run, testnet, or live")
        if cfg.mode == "live" and os.getenv("BYBIT_LIVE_ACK") != LIVE_ACK:
            raise BybitError(f"Live mode locked. Set BYBIT_LIVE_ACK={LIVE_ACK} explicitly.")
        if cfg.mode == "testnet":
            key, sec = os.getenv("BYBIT_TESTNET_API_KEY", ""), os.getenv("BYBIT_TESTNET_API_SECRET", "")
        elif cfg.mode == "live":
            key, sec = os.getenv("BYBIT_API_KEY", ""), os.getenv("BYBIT_API_SECRET", "")
        else:
            key = sec = ""
        self.cfg = cfg
        self.client = client or BybitREST(cfg, key, sec)
        self.journal = Journal(cfg.journal_path)

    @staticmethod
    def make_order_link_id(strategy: str, execution_day: str, symbol: str, tranche: int, action: str = "E") -> str:
        raw = f"{strategy}-{execution_day.replace('-', '')}-{symbol}-{action}{tranche}"
        if len(raw) <= 36:
            return raw
        digest = hashlib.sha1(raw.encode()).hexdigest()[:8]
        return f"RH2-{execution_day.replace('-', '')}-{symbol[:10]}-{action}{tranche}-{digest}"[:36]

    @staticmethod
    def parse_rules(inst: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
        pf = inst.get("priceFilter", {}) or {}
        lf = inst.get("lotSizeFilter", {}) or {}
        tick = D(pf.get("tickSize") or inst.get("tickSize") or "0.00000001")
        qty_step = D(lf.get("qtyStep") or lf.get("basePrecision") or inst.get("lotSize") or "0.00000001")
        min_qty = D(lf.get("minOrderQty") or lf.get("minOrderAmt") or inst.get("minSize") or "0")
        min_notional = D(lf.get("minNotionalValue") or lf.get("minOrderAmt") or "0")
        return tick, qty_step, max(min_qty, D("0")), max(min_notional, D("0"))

    def public_health(self, symbol: str = "BTCUSDT") -> dict[str, Any]:
        t = self.client.server_time()
        q = self.client.ticker(symbol)
        i = self.client.instrument(symbol)
        return {"mode": self.cfg.mode, "server_time": t.get("time"), "symbol": symbol, "lastPrice": q.get("lastPrice"), "status": i.get("status")}

    def reconcile_order(self, symbol: str, order_link_id: str) -> dict[str, Any]:
        orders = []
        try:
            orders.extend(self.client.open_orders(symbol, order_link_id=order_link_id))
        except Exception:
            pass
        try:
            orders.extend(self.client.order_history(symbol, order_link_id=order_link_id))
        except Exception:
            pass
        # Dedupe by orderId, prefer later entries.
        by_id = {o.get("orderId") or f"x{i}": o for i, o in enumerate(orders)}
        fills = self.client.executions(symbol, order_link_id=order_link_id)
        filled = sum((D(x.get("execQty", "0")) for x in fills), D("0"))
        value = sum((D(x.get("execQty", "0")) * D(x.get("execPrice", "0")) for x in fills), D("0"))
        avg = value / filled if filled > 0 else None
        terminal = None
        for o in by_id.values():
            if o.get("orderStatus") in {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}:
                terminal = o.get("orderStatus")
        resolved = terminal is not None
        out = {"resolved": resolved, "terminal": terminal, "filled_qty": decstr(filled), "avg_price": decstr(avg) if avg else None, "orders": list(by_id.values()), "fills": fills}
        if resolved:
            self.journal.append({"event": "reconciled", "symbol": symbol, "orderLinkId": order_link_id, **{k: out[k] for k in ("terminal", "filled_qty", "avg_price")}})
        return out

    def reconcile_symbol(self, symbol: str) -> dict[str, Any]:
        unresolved = self.journal.unresolved(symbol)
        reconciled = {oid: self.reconcile_order(symbol, oid) for oid in unresolved}
        still = [oid for oid, x in reconciled.items() if not x.get("resolved")]
        positions = self.client.position(symbol) if self.cfg.mode != "dry-run" else []
        opens = self.client.open_orders(symbol) if self.cfg.mode != "dry-run" else []
        return {"symbol": symbol, "unresolved_before": unresolved, "unresolved_after": still, "positions": positions, "open_orders": opens}

    def _market_price(self, ticker: dict[str, Any], side: str) -> Decimal:
        if side == "Buy":
            return D(ticker.get("ask1Price") or ticker.get("lastPrice"))
        return D(ticker.get("bid1Price") or ticker.get("lastPrice"))

    def protected_ioc(self, symbol: str, side: str, quote_notional: Decimal, reference_price: Decimal, order_link_id: str, guard_pct: Decimal | None = None) -> ProtectedOrderResult:
        if side not in {"Buy", "Sell"}:
            raise ValueError("side must be Buy or Sell")
        if reference_price <= 0 or quote_notional <= 0:
            raise ValueError("reference_price and quote_notional must be positive")

        # Never submit if this deterministic ID may already exist.
        if self.cfg.mode != "dry-run":
            pre = self.reconcile_symbol(symbol)
            if pre["unresolved_after"]:
                return ProtectedOrderResult("BLOCKED_UNRESOLVED", symbol, side, order_link_id, decstr(reference_price), reason=str(pre["unresolved_after"]))
            if self.journal.has_submit(order_link_id):
                r = self.reconcile_order(symbol, order_link_id)
                return ProtectedOrderResult("ALREADY_SUBMITTED", symbol, side, order_link_id, decstr(reference_price), filled_qty=r.get("filled_qty", "0"), avg_price=r.get("avg_price"))

        ticker = self.client.ticker(symbol)
        market = self._market_price(ticker, side)
        adverse = ((market / reference_price - 1) if side == "Buy" else (reference_price / market - 1)) * D("10000")
        if adverse > self.cfg.max_adverse_drift_bps:
            out = ProtectedOrderResult("SKIPPED_DRIFT", symbol, side, order_link_id, decstr(reference_price), decstr(market), reason=f"adverse_drift_bps={adverse}")
            self.journal.append({"event": "skip_drift", **asdict(out)})
            return out

        inst = self.client.instrument(symbol)
        tick, qty_step, min_qty, min_notional = self.parse_rules(inst)
        cap = self.cfg.max_total_slippage_bps / D("10000")
        raw_limit = reference_price * (D("1") + cap if side == "Buy" else D("1") - cap)
        limit_price = floor_step(raw_limit, tick) if side == "Buy" else ceil_step(raw_limit, tick)
        qty = floor_step(quote_notional / limit_price, qty_step)
        if qty <= 0 or qty < min_qty or qty * limit_price < min_notional:
            reason = f"qty={qty}, min_qty={min_qty}, notional={qty*limit_price}, min_notional={min_notional}"
            out = ProtectedOrderResult("SKIPPED_MIN_ORDER", symbol, side, order_link_id, decstr(reference_price), decstr(market), decstr(limit_price), decstr(qty), reason=reason)
            self.journal.append({"event": "skip_min_order", **asdict(out)})
            return out

        payload = {"category": self.cfg.category, "symbol": symbol, "side": side, "orderType": "Limit", "qty": decstr(qty), "price": decstr(limit_price), "timeInForce": "IOC", "orderLinkId": order_link_id}
        if self.cfg.mode == "dry-run":
            # Conservative simulator: fill only if current touch is inside the hard limit.
            fillable = market <= limit_price if side == "Buy" else market >= limit_price
            fill = qty if fillable else D("0")
            avg = market if fillable else None
            status = "DRY_RUN_FILLED" if fillable else "DRY_RUN_NO_FILL"
            stop = None
            if fill > 0 and guard_pct is not None and avg is not None:
                stop = avg * (D("1") - guard_pct if side == "Buy" else D("1") + guard_pct)
            out = ProtectedOrderResult(status, symbol, side, order_link_id, decstr(reference_price), decstr(market), decstr(limit_price), decstr(qty), decstr(fill), decstr(avg) if avg else None, stop_loss=decstr(stop) if stop else None)
            self.journal.append({"event": "dry_run", **asdict(out)})
            return out

        try:
            ack = self.client.create_order(payload)
            order_id = ack.get("result", {}).get("orderId")
            self.journal.append({"event": "submit_accepted", "symbol": symbol, "orderLinkId": order_link_id, "orderId": order_id, "payload": payload})
        except (requests.Timeout, requests.ConnectionError) as e:
            # Submission state is unknown. Never blind retry. A later reconcile must resolve it.
            self.journal.append({"event": "submit_unknown", "symbol": symbol, "orderLinkId": order_link_id, "payload": payload, "error": repr(e)})
            return ProtectedOrderResult("SUBMIT_UNKNOWN_RECONCILE_REQUIRED", symbol, side, order_link_id, decstr(reference_price), decstr(market), decstr(limit_price), decstr(qty), reason=repr(e))

        deadline = time.time() + self.cfg.reconcile_timeout_sec
        rec: dict[str, Any] = {}
        while time.time() < deadline:
            rec = self.reconcile_order(symbol, order_link_id)
            if rec.get("resolved"):
                break
            time.sleep(self.cfg.poll_sec)
        if not rec.get("resolved"):
            self.journal.append({"event": "submit_unknown", "symbol": symbol, "orderLinkId": order_link_id, "orderId": order_id, "reason": "acknowledged_but_not_terminal_before_timeout"})
            return ProtectedOrderResult("ACKED_PENDING_RECONCILE", symbol, side, order_link_id, decstr(reference_price), decstr(market), decstr(limit_price), decstr(qty), order_id=order_id)

        filled = D(rec.get("filled_qty", "0"))
        avg = D(rec["avg_price"]) if rec.get("avg_price") else None
        stop = None
        if filled > 0 and avg is not None and guard_pct is not None and self.cfg.category == "linear":
            tick, _, _, _ = self.parse_rules(inst)
            raw_stop = avg * (D("1") - guard_pct if side == "Buy" else D("1") + guard_pct)
            stop = floor_step(raw_stop, tick) if side == "Buy" else ceil_step(raw_stop, tick)
            self.client.set_trading_stop(symbol, stop, category="linear", position_idx=0)
            self.journal.append({"event": "native_mark_stop_set", "symbol": symbol, "orderLinkId": order_link_id, "stopLoss": decstr(stop), "filled_qty": decstr(filled)})
        return ProtectedOrderResult("FILLED" if filled > 0 else "NO_FILL", symbol, side, order_link_id, decstr(reference_price), decstr(market), decstr(limit_price), decstr(qty), decstr(filled), decstr(avg) if avg else None, order_id=order_id, stop_loss=decstr(stop) if stop else None)


def build_engine(mode: str, journal: str) -> ExecutionEngine:
    return ExecutionEngine(EngineConfig(mode=mode, journal_path=journal))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["health", "reconcile", "order"])
    p.add_argument("--mode", choices=["dry-run", "testnet", "live"], default="dry-run")
    p.add_argument("--journal", default="execution_journal.jsonl")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--side", choices=["Buy", "Sell"], default="Buy")
    p.add_argument("--notional", default="10")
    p.add_argument("--reference", default=None)
    p.add_argument("--order-link-id", default=None)
    p.add_argument("--guard", default=None, help="decimal fraction, e.g. 0.12 or 0.25")
    a = p.parse_args()
    e = build_engine(a.mode, a.journal)
    if a.command == "health":
        print(json.dumps(e.public_health(a.symbol), indent=2))
    elif a.command == "reconcile":
        print(json.dumps(e.reconcile_symbol(a.symbol), indent=2, default=str))
    else:
        ticker = e.client.ticker(a.symbol)
        ref = D(a.reference or ticker.get("lastPrice"))
        oid = a.order_link_id or e.make_order_link_id("RH2", time.strftime("%Y-%m-%d", time.gmtime()), a.symbol, 1)
        r = e.protected_ioc(a.symbol, a.side, D(a.notional), ref, oid, D(a.guard) if a.guard else None)
        print(json.dumps(asdict(r), indent=2))


if __name__ == "__main__":
    main()
