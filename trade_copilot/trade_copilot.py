#!/usr/bin/env python3
"""
Small Binance USD-M futures scanner and guarded order helper.

Default behavior is read-only. Live order placement requires API keys, --live,
and an exact typed confirmation.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any


DEFAULT_BASE_URL = "https://fapi.binance.com"
FAR_FUTURE_DELIVERY_MS = 4_000_000_000_000
DEFAULT_ALLOWED_SYMBOLS = "RAVEUSDT,HIGHUSDT,ALICEUSDT,PORTALUSDT,SIRENUSDT"


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def fmt_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick == 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def pct_change(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def allowed_symbols() -> set[str]:
    raw = os.getenv("ALLOWED_SYMBOLS", DEFAULT_ALLOWED_SYMBOLS)
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def public_ip() -> str:
    try:
        req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "trade-copilot/0.1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception as exc:
        return f"lookup failed: {exc}"


def account_available_usdt(client: BinanceClient) -> Decimal:
    account = client.signed_get("/fapi/v2/account")
    candidates = [
        account.get("availableBalance"),
        account.get("maxWithdrawAmount"),
    ]
    assets = account.get("assets") or []
    for asset in assets:
        if asset.get("asset") == "USDT":
            candidates.extend([asset.get("availableBalance"), asset.get("maxWithdrawAmount"), asset.get("walletBalance")])
    for value in candidates:
        if value is None:
            continue
        try:
            amount = Decimal(str(value))
            if amount >= 0:
                return amount
        except Exception:
            continue
    return Decimal("0")


def adjusted_margin(client: BinanceClient, requested_margin: Decimal, reserve: Decimal) -> Decimal:
    available = account_available_usdt(client)
    usable = available - reserve
    if usable <= 0:
        raise BinanceError(f"Insufficient available USDT margin. available={fmt_decimal(available)} reserve={fmt_decimal(reserve)}")
    if requested_margin <= usable:
        return requested_margin
    adjusted = usable.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if adjusted <= 0:
        raise BinanceError(f"Insufficient available USDT margin after reserve. available={fmt_decimal(available)} reserve={fmt_decimal(reserve)}")
    print(f"requested margin {fmt_decimal(requested_margin)} exceeds usable balance {fmt_decimal(usable)}; sizing down to {fmt_decimal(adjusted)}")
    return adjusted


def max_initial_leverage(client: BinanceClient, symbol: str) -> int | None:
    try:
        data = client.signed_get("/fapi/v1/leverageBracket", {"symbol": symbol.upper()})
        first = data[0]["brackets"][0] if isinstance(data, list) else data["brackets"][0]
        return int(first["initialLeverage"])
    except Exception:
        return None


def account_margin_summary(client: BinanceClient, symbols: set[str], reserve: Decimal) -> None:
    if not client.api_key or not client.api_secret:
        return
    available = account_available_usdt(client)
    usable = available - reserve
    print("\nACCOUNT")
    print(f"available USDT margin: {fmt_decimal(available)}")
    print(f"reserve: {fmt_decimal(reserve)}")
    print(f"usable: {fmt_decimal(usable) if usable > 0 else '0'}")
    print("minimum margin by symbol at max bracket leverage:")
    for symbol in sorted(symbols):
        try:
            rules = rules_for_symbol(client, symbol)
            max_lev = max_initial_leverage(client, symbol)
            if not max_lev:
                print(f"  {symbol}: max leverage unknown")
                continue
            min_margin = rules.min_notional / Decimal(max_lev)
            status = "fits" if usable >= min_margin else "does not fit"
            print(
                f"  {symbol}: max {max_lev}x, minNotional {fmt_decimal(rules.min_notional)}, "
                f"min margin {fmt_decimal(min_margin)} -> {status}"
            )
        except BinanceError as exc:
            print(f"  {symbol}: {exc}")


def active_exposure(client: BinanceClient, symbols: set[str]) -> list[str]:
    active: list[str] = []
    if not client.api_key or not client.api_secret:
        return active
    positions = client.signed_get("/fapi/v2/positionRisk")
    for position in positions:
        symbol = position.get("symbol")
        if symbol not in symbols:
            continue
        amount = Decimal(str(position.get("positionAmt", "0")))
        if amount != 0:
            active.append(f"{symbol} positionAmt={fmt_decimal(amount)}")
    for symbol in sorted(symbols):
        orders = client.signed_get("/fapi/v1/openOrders", {"symbol": symbol})
        if orders:
            active.append(f"{symbol} openOrders={len(orders)}")
    return active


class BinanceError(RuntimeError):
    pass


class BinanceClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("BINANCE_FAPI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = os.getenv("BINANCE_API_KEY", "")
        self.api_secret = os.getenv("BINANCE_API_SECRET", "")

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params or {}, signed=False)

    def signed_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise BinanceError("Missing BINANCE_API_KEY or BINANCE_API_SECRET. Put them in .env or environment variables.")
        data = dict(params or {})
        data.setdefault("recvWindow", 5000)
        data["timestamp"] = self.server_time()
        query = urllib.parse.urlencode(data, doseq=True)
        sig = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        data["signature"] = sig
        return self._request(method, path, data, signed=True)

    def signed_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.signed_request("GET", path, params)

    def server_time(self) -> int:
        data = self.public_get("/fapi/v1/time")
        return int(data["serverTime"])

    def _request(self, method: str, path: str, params: dict[str, Any], signed: bool) -> Any:
        query = urllib.parse.urlencode(params, doseq=True)
        url = self.base_url + path
        body = None
        headers = {"Accept": "application/json", "User-Agent": "trade-copilot/0.1"}
        if signed:
            headers["X-MBX-APIKEY"] = self.api_key

        if method.upper() == "GET":
            if query:
                url += "?" + query
        else:
            body = query.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise BinanceError(f"Binance HTTP {exc.code}: {payload}") from exc
        except urllib.error.URLError as exc:
            raise BinanceError(f"Network error: {exc}") from exc


@dataclass
class SymbolRules:
    symbol: str
    tick_size: Decimal
    lot_step: Decimal
    market_lot_step: Decimal
    min_qty: Decimal
    min_notional: Decimal


def trading_symbols(client: BinanceClient) -> dict[str, dict[str, Any]]:
    info = client.public_get("/fapi/v1/exchangeInfo")
    symbols: dict[str, dict[str, Any]] = {}
    pattern = re.compile(r"^[A-Z0-9]+(?:USDT|USDC)$")
    for item in info["symbols"]:
        symbol = item.get("symbol", "")
        if not pattern.match(symbol):
            continue
        if item.get("status") != "TRADING":
            continue
        if item.get("contractType") != "PERPETUAL":
            continue
        if int(item.get("deliveryDate") or 0) < FAR_FUTURE_DELIVERY_MS:
            continue
        symbols[symbol] = item
    return symbols


def rules_for_symbol(client: BinanceClient, symbol: str) -> SymbolRules:
    symbols = trading_symbols(client)
    item = symbols.get(symbol.upper())
    if not item:
        raise BinanceError(f"{symbol} is not a currently trading USD-M perpetual symbol.")
    filters = {f["filterType"]: f for f in item.get("filters", [])}
    price_filter = filters.get("PRICE_FILTER", {})
    lot_filter = filters.get("LOT_SIZE", {})
    market_lot_filter = filters.get("MARKET_LOT_SIZE", lot_filter)
    min_notional_filter = filters.get("MIN_NOTIONAL", {})
    return SymbolRules(
        symbol=symbol.upper(),
        tick_size=Decimal(str(price_filter.get("tickSize", "0"))),
        lot_step=Decimal(str(lot_filter.get("stepSize", "0"))),
        market_lot_step=Decimal(str(market_lot_filter.get("stepSize", lot_filter.get("stepSize", "0")))),
        min_qty=Decimal(str(market_lot_filter.get("minQty", lot_filter.get("minQty", "0")))),
        min_notional=Decimal(str(min_notional_filter.get("notional", "5"))),
    )


def recent_klines(client: BinanceClient, symbol: str, interval: str, limit: int) -> list[list[Any]]:
    return client.public_get("/fapi/v1/klines", {"symbol": symbol.upper(), "interval": interval, "limit": limit})


def kline_return(klines: list[list[Any]]) -> float:
    if len(klines) < 2:
        return 0.0
    return pct_change(float(klines[0][1]), float(klines[-1][4]))


def volume_ratio(klines: list[list[Any]], recent_n: int = 6) -> float:
    vols = [float(k[7]) for k in klines]
    if len(vols) < recent_n + 10:
        return 1.0
    recent = sum(vols[-recent_n:]) / recent_n
    base_slice = vols[-(recent_n + 24) : -recent_n] if len(vols) >= recent_n + 24 else vols[:-recent_n]
    base = sum(base_slice) / max(1, len(base_slice))
    return recent / base if base else 1.0


def range_position(klines: list[list[Any]]) -> float:
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    close = float(klines[-1][4])
    hi = max(highs)
    lo = min(lows)
    return (close - lo) / (hi - lo) if hi > lo else 0.5


def score_market(client: BinanceClient, row: dict[str, Any]) -> dict[str, Any] | None:
    symbol = row["symbol"]
    try:
        k5 = recent_klines(client, symbol, "5m", 72)
        k15 = recent_klines(client, symbol, "15m", 64)
    except BinanceError:
        return None
    if len(k5) < 20 or len(k15) < 20:
        return None
    ret15 = kline_return(k5[-3:])
    ret30 = kline_return(k5[-6:])
    ret1h = kline_return(k5[-12:])
    ret4h = kline_return(k15[-16:])
    vol = volume_ratio(k5)
    pos1h = range_position(k5[-12:])
    long_score = (
        ret30 * 1.1
        + ret1h
        + ret4h * 0.45
        + min(vol, 5.0) * 2
        + (4 if row["pct24"] > 20 else 0)
        - (3 if pos1h > 0.94 else 0)
        - (2 if row["funding_bp"] > 30 else 0)
    )
    short_score = (
        -ret30 * 1.1
        - ret1h
        - ret4h * 0.45
        + min(vol, 5.0) * 2
        + (4 if row["pct24"] < -10 else 0)
        - (3 if pos1h < 0.06 else 0)
        - (2 if row["funding_bp"] < -30 else 0)
    )
    return {
        **row,
        "ret15": ret15,
        "ret30": ret30,
        "ret1h": ret1h,
        "ret4h": ret4h,
        "vol_ratio": vol,
        "pos1h": pos1h,
        "long_score": long_score,
        "short_score": short_score,
    }


def scan(client: BinanceClient, min_volume: float, limit: int, candidates: int) -> None:
    symbols = trading_symbols(client)
    tickers = client.public_get("/fapi/v1/ticker/24hr")
    premium = {p["symbol"]: p for p in client.public_get("/fapi/v1/premiumIndex")}
    rows: list[dict[str, Any]] = []

    for ticker in tickers:
        symbol = ticker["symbol"]
        if symbol not in symbols:
            continue
        quote_volume = float(ticker["quoteVolume"])
        if quote_volume < min_volume:
            continue
        last = float(ticker["lastPrice"])
        high = float(ticker["highPrice"])
        low = float(ticker["lowPrice"])
        pct24 = float(ticker["priceChangePercent"])
        p = premium.get(symbol, {})
        funding_bp = float(p.get("lastFundingRate") or 0) * 10_000
        mark = float(p.get("markPrice") or last)
        index = float(p.get("indexPrice") or mark)
        rows.append(
            {
                "symbol": symbol,
                "last": last,
                "pct24": pct24,
                "quote_volume_m": quote_volume / 1_000_000,
                "range_pct": pct_change(low, high) if low else 0,
                "range_pos": (last - low) / (high - low) if high > low else 0.5,
                "funding_bp": funding_bp,
                "basis_pct": pct_change(index, mark) if index else 0,
            }
        )

    seed = sorted(
        rows,
        key=lambda r: abs(r["pct24"]) * 0.9 + min(r["range_pct"], 150) * 0.7 + math.log10(max(r["quote_volume_m"], 1)) * 5,
        reverse=True,
    )[:candidates]
    scored = [item for item in (score_market(client, row) for row in seed) if item]
    scored.sort(key=lambda r: max(r["long_score"], r["short_score"]), reverse=True)

    print("WATCHLIST")
    for row in scored[:limit]:
        side = "LONG" if row["long_score"] >= row["short_score"] else "SHORT"
        print(
            f"{row['symbol']:14s} {side:5s} "
            f"score={max(row['long_score'], row['short_score']):6.1f} "
            f"last={row['last']:<12g} 15m={row['ret15']:6.2f}% 30m={row['ret30']:6.2f}% "
            f"1h={row['ret1h']:6.2f}% 4h={row['ret4h']:7.2f}% "
            f"24h={row['pct24']:7.2f}% vol=${row['quote_volume_m']:7.1f}m "
            f"pos1h={row['pos1h']:.2f} funding={row['funding_bp']:7.2f}bp"
        )


def evaluate_setups(
    client: BinanceClient,
    margin: Decimal,
    leverage: int,
    target_pct: Decimal,
    stop_pct: Decimal,
    reserve: Decimal,
) -> tuple[set[str], list[dict[str, Any]]]:
    allowed = allowed_symbols()
    if not allowed:
        raise BinanceError("ALLOWED_SYMBOLS is empty. Refusing to judge an unrestricted universe.")

    symbols = trading_symbols(client)
    ticker_map = {ticker["symbol"]: ticker for ticker in client.public_get("/fapi/v1/ticker/24hr")}
    premium = {p["symbol"]: p for p in client.public_get("/fapi/v1/premiumIndex")}
    scored: list[dict[str, Any]] = []

    for symbol in sorted(allowed):
        if symbol not in symbols:
            continue
        ticker = ticker_map.get(symbol)
        if not ticker:
            continue
        last = float(ticker["lastPrice"])
        high = float(ticker["highPrice"])
        low = float(ticker["lowPrice"])
        p = premium.get(symbol, {})
        mark = float(p.get("markPrice") or last)
        index = float(p.get("indexPrice") or mark)
        row = {
            "symbol": symbol,
            "last": last,
            "pct24": float(ticker["priceChangePercent"]),
            "quote_volume_m": float(ticker["quoteVolume"]) / 1_000_000,
            "range_pct": pct_change(low, high) if low else 0,
            "range_pos": (last - low) / (high - low) if high > low else 0.5,
            "funding_bp": float(p.get("lastFundingRate") or 0) * 10_000,
            "basis_pct": pct_change(index, mark) if index else 0,
        }
        item = score_market(client, row)
        if item:
            scored.append(item)

    if not scored:
        raise BinanceError("No allowed symbols produced a score. Check ALLOWED_SYMBOLS and Binance symbol availability.")

    decisions: list[dict[str, Any]] = []
    usable_margin: Decimal | None = None
    account_available: Decimal | None = None
    if client.api_key and client.api_secret:
        account_available = account_available_usdt(client)
        usable_margin = account_available - reserve

    for row in scored:
        side = "LONG" if row["long_score"] >= row["short_score"] else "SHORT"
        score = max(row["long_score"], row["short_score"])
        reasons: list[str] = []
        if score < 18:
            reasons.append("score below 18")
        if row["quote_volume_m"] < 20:
            reasons.append("24h futures volume below $20m")
        if side == "LONG" and row["ret15"] < -1.5:
            reasons.append("15m tape is against the long")
        if side == "SHORT" and row["ret15"] > 1.5:
            reasons.append("15m tape is against the short")
        if side == "LONG" and row["pos1h"] > 0.94:
            reasons.append("long is too close to the 1h high")
        if side == "SHORT" and row["pos1h"] < 0.06:
            reasons.append("short is too close to the 1h low")
        if side == "SHORT" and row["funding_bp"] < -70:
            reasons.append("short is crowded by extreme negative funding")
        if side == "LONG" and row["funding_bp"] > 25:
            reasons.append("long is crowded by high positive funding")
        ticket_margin = margin
        if not reasons and usable_margin is not None:
            if usable_margin <= 0:
                reasons.append(
                    f"insufficient available margin: available {fmt_decimal(account_available or Decimal('0'))}, reserve {fmt_decimal(reserve)}"
                )
            else:
                if margin > usable_margin:
                    ticket_margin = usable_margin.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                try:
                    build_ticket(client, row["symbol"], side, ticket_margin, leverage, target_pct, stop_pct)
                except BinanceError as exc:
                    reasons.append(f"account cannot fit at {leverage}x with margin {fmt_decimal(ticket_margin)}: {exc}")
        decisions.append({**row, "side": side, "score": score, "reasons": reasons, "ticket_margin": ticket_margin})

    decisions.sort(key=lambda row: (len(row["reasons"]) == 0, row["score"]), reverse=True)
    return allowed, decisions


def print_judgement(decisions: list[dict[str, Any]]) -> None:
    print("JUDGEMENT")
    for row in decisions:
        verdict = "PASS" if not row["reasons"] else "REJECT"
        reason_text = "clean enough to arm" if not row["reasons"] else "; ".join(row["reasons"])
        print(
            f"{verdict:6s} {row['symbol']:14s} {row['side']:5s} score={row['score']:6.1f} "
            f"15m={row['ret15']:6.2f}% 30m={row['ret30']:6.2f}% 1h={row['ret1h']:6.2f}% "
            f"4h={row['ret4h']:7.2f}% pos1h={row['pos1h']:.2f} funding={row['funding_bp']:7.2f}bp "
            f"- {reason_text}"
        )


def judge(client: BinanceClient, margin: Decimal, leverage: int, target_pct: Decimal, stop_pct: Decimal, reserve: Decimal) -> None:
    allowed, decisions = evaluate_setups(client, margin, leverage, target_pct, stop_pct, reserve)
    print_judgement(decisions)
    best = decisions[0]
    if best["reasons"]:
        print("\nACTION: WAIT")
        print("No allowed symbol passes the judgement filter. That is a valid trade decision.")
        account_margin_summary(client, allowed, reserve)
        return

    margin = best.get("ticket_margin") or (adjusted_margin(client, margin, reserve) if client.api_key and client.api_secret else margin)
    ticket = build_ticket(client, best["symbol"], best["side"], margin, leverage, target_pct, stop_pct)
    print("\nACTION: ARM THIS TICKET")
    print_ticket(ticket)
    print("\nDry run:")
    print(
        f"python trade_copilot.py place {best['symbol']} {best['side']} "
        f"--margin {fmt_decimal(margin)} --leverage {leverage} "
        f"--target-pct {fmt_decimal(target_pct)} --stop-pct {fmt_decimal(stop_pct)}"
    )
    print("\nLive, only after you agree with the ticket:")
    print(
        f"python trade_copilot.py place {best['symbol']} {best['side']} "
        f"--margin {fmt_decimal(margin)} --leverage {leverage} "
        f"--target-pct {fmt_decimal(target_pct)} --stop-pct {fmt_decimal(stop_pct)} --live"
    )


def watch(
    client: BinanceClient,
    margin: Decimal,
    leverage: int,
    target_pct: Decimal,
    stop_pct: Decimal,
    reserve: Decimal,
    interval_seconds: int,
    min_score: Decimal,
    confirmations: int,
    live_auto: bool,
    max_trades_per_day: int,
    cooldown_seconds: int,
    max_cycles: int | None,
) -> None:
    if interval_seconds < 10:
        raise BinanceError("Watch interval must be at least 10 seconds.")
    if confirmations < 1:
        raise BinanceError("Confirmations must be at least 1.")
    if live_auto and (not client.api_key or not client.api_secret):
        raise BinanceError("--live-auto requires BINANCE_API_KEY and BINANCE_API_SECRET.")

    allowed = allowed_symbols()
    if not allowed:
        raise BinanceError("ALLOWED_SYMBOLS is empty. Refusing to watch an unrestricted universe.")

    mode = "LIVE-AUTO" if live_auto else "DRY-RUN"
    print(f"WATCH MODE: {mode}")
    print(f"allowed symbols: {', '.join(sorted(allowed))}")
    print(
        f"interval={interval_seconds}s min_score={fmt_decimal(min_score)} confirmations={confirmations} "
        f"max_trades_per_day={max_trades_per_day} reserve={fmt_decimal(reserve)}"
    )
    if live_auto:
        print("Live auto mode will place orders without typed confirmation when all gates pass.")

    streak_key: tuple[str, str] | None = None
    streak_count = 0
    trades_today = 0
    trade_day = time.strftime("%Y-%m-%d")
    cooldown_until = 0.0
    cycle = 0

    while True:
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        if today != trade_day:
            trade_day = today
            trades_today = 0

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{timestamp}] cycle={cycle + 1}")

        try:
            allowed, decisions = evaluate_setups(client, margin, leverage, target_pct, stop_pct, reserve)
            best = decisions[0]
            reason_text = "clean enough to arm" if not best["reasons"] else "; ".join(best["reasons"])
            print(
                f"best={best['symbol']} {best['side']} score={best['score']:.1f} "
                f"15m={best['ret15']:.2f}% 1h={best['ret1h']:.2f}% - {reason_text}"
            )

            active = active_exposure(client, allowed) if client.api_key and client.api_secret else []
            if active:
                print(f"ACTION: WAIT active exposure/order exists: {'; '.join(active)}")
                streak_key = None
                streak_count = 0
            elif best["reasons"]:
                print("ACTION: WAIT judgement rejected best setup")
                streak_key = None
                streak_count = 0
            elif Decimal(str(best["score"])) < min_score:
                print(f"ACTION: WAIT score below watch min_score {fmt_decimal(min_score)}")
                streak_key = None
                streak_count = 0
            elif trades_today >= max_trades_per_day:
                print(f"ACTION: WAIT daily trade cap reached ({trades_today}/{max_trades_per_day})")
            elif now < cooldown_until:
                remaining = int(cooldown_until - now)
                print(f"ACTION: WAIT cooldown active for {remaining}s")
            else:
                key = (best["symbol"], best["side"])
                if key == streak_key:
                    streak_count += 1
                else:
                    streak_key = key
                    streak_count = 1

                if streak_count < confirmations:
                    print(f"ACTION: WAIT confirmation streak {streak_count}/{confirmations} for {best['symbol']} {best['side']}")
                else:
                    ticket_margin = best.get("ticket_margin") or margin
                    ticket = build_ticket(client, best["symbol"], best["side"], ticket_margin, leverage, target_pct, stop_pct)
                    if live_auto:
                        print("ACTION: LIVE TRADE")
                        place_order(client, ticket, live=True, reserve=reserve, require_confirmation=False)
                        trades_today += 1
                        cooldown_until = time.time() + cooldown_seconds
                    else:
                        print("ACTION: DRY SIGNAL")
                        print_ticket(ticket)
                        print(
                            f"live command: python trade_copilot.py place {best['symbol']} {best['side']} "
                            f"--margin {fmt_decimal(ticket_margin)} --leverage {leverage} "
                            f"--target-pct {fmt_decimal(target_pct)} --stop-pct {fmt_decimal(stop_pct)} --live"
                        )
                        cooldown_until = time.time() + cooldown_seconds
                    streak_key = None
                    streak_count = 0
        except BinanceError as exc:
            print(f"ACTION: WAIT error={exc}", file=sys.stderr)

        cycle += 1
        if max_cycles is not None and cycle >= max_cycles:
            print("WATCH STOP: max cycles reached")
            return
        time.sleep(interval_seconds)


def auth_check(client: BinanceClient) -> None:
    print("AUTH CHECK")
    print(f".env present: {os.path.exists('.env')}")
    print(f"api key present: {bool(client.api_key)} length={len(client.api_key)}")
    print(f"api secret present: {bool(client.api_secret)} length={len(client.api_secret)}")
    print(f"public ip: {public_ip()}")
    print(f"allowed symbols: {', '.join(sorted(allowed_symbols()))}")
    if not client.api_key or not client.api_secret:
        raise BinanceError("Missing key or secret. Fill .env first.")

    try:
        hedge_status = client.signed_get("/fapi/v1/positionSide/dual")
        print(f"futures signed read: OK positionSide={hedge_status}")
        multi_assets_status = client.signed_get("/fapi/v1/multiAssetsMargin")
        print(f"multi-assets status: OK {multi_assets_status}")
        print(f"available USDT margin: {fmt_decimal(account_available_usdt(client))}")
    except BinanceError as exc:
        print(f"futures signed read: FAILED {exc}")
        print("Most likely causes:")
        print("- .env still has the old/deleted API key")
        print("- Futures permission is not enabled or was not saved on the Binance API key")
        print("- The key IP whitelist does not include the public IP above")
        print("- The API key was created under a different account/sub-account than the futures wallet")
        raise


def print_levels(client: BinanceClient, symbol: str) -> None:
    symbol = symbol.upper()
    klines = recent_klines(client, symbol, "5m", 96)
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    volumes = [float(k[7]) for k in klines]
    last = closes[-1]
    vwap = sum(closes[i] * volumes[i] for i in range(len(klines))) / sum(volumes) if sum(volumes) else last
    true_ranges = []
    previous_close = closes[-25]
    for i in range(-24, 0):
        h = highs[i]
        l = lows[i]
        c = closes[i]
        true_ranges.append(max(h - l, abs(h - previous_close), abs(l - previous_close)) / c * 100)
        previous_close = c
    atr5m = sum(true_ranges[-12:]) / 12

    print(f"{symbol} last={last:g} vwap8h={vwap:g} atr5m%={atr5m:.2f}")
    for n, label in [(3, "15m"), (6, "30m"), (12, "1h"), (48, "4h"), (96, "8h")]:
        window = klines[-n:]
        hi = max(float(k[2]) for k in window)
        lo = min(float(k[3]) for k in window)
        start = float(window[0][1])
        pos = (last - lo) / (hi - lo) if hi > lo else 0.5
        print(f"  {label:3s} ret={pct_change(start, last):7.2f}% hi={hi:g} lo={lo:g} pos={pos:.2f}")


def current_mark(client: BinanceClient, symbol: str) -> Decimal:
    data = client.public_get("/fapi/v1/premiumIndex", {"symbol": symbol.upper()})
    return Decimal(str(data["markPrice"]))


def build_ticket(
    client: BinanceClient,
    symbol: str,
    side: str,
    margin: Decimal,
    leverage: int,
    target_pct: Decimal,
    stop_pct: Decimal,
) -> dict[str, Any]:
    symbol = symbol.upper()
    side = side.upper()
    if side not in {"LONG", "SHORT"}:
        raise BinanceError("side must be LONG or SHORT")
    rules = rules_for_symbol(client, symbol)
    mark = current_mark(client, symbol)
    notional = margin * Decimal(leverage)
    qty = floor_to_step(notional / mark, rules.market_lot_step)
    if qty < rules.min_qty:
        raise BinanceError(f"Computed quantity {qty} is below market minQty {rules.min_qty}.")
    actual_notional = qty * mark
    if actual_notional < rules.min_notional:
        raise BinanceError(f"Computed notional {actual_notional} is below minNotional {rules.min_notional}. Increase margin/leverage.")

    direction = Decimal("1") if side == "LONG" else Decimal("-1")
    target = round_to_tick(mark * (Decimal("1") + direction * target_pct / Decimal("100")), rules.tick_size)
    stop = round_to_tick(mark * (Decimal("1") - direction * stop_pct / Decimal("100")), rules.tick_size)
    entry_side = "BUY" if side == "LONG" else "SELL"
    exit_side = "SELL" if side == "LONG" else "BUY"
    fee_warning = actual_notional * Decimal("0.001")
    return {
        "symbol": symbol,
        "bias": side,
        "entry_side": entry_side,
        "exit_side": exit_side,
        "direction": direction,
        "tick_size": rules.tick_size,
        "mark": mark,
        "margin": margin,
        "leverage": leverage,
        "quantity": qty,
        "notional": actual_notional,
        "target_stop_price": target,
        "protective_stop_price": stop,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "estimated_round_trip_taker_fee": fee_warning,
    }


def print_ticket(ticket: dict[str, Any]) -> None:
    print("ORDER TICKET")
    print(f"symbol: {ticket['symbol']}")
    print(f"bias: {ticket['bias']}")
    print(f"entry: MARKET {ticket['entry_side']} {fmt_decimal(ticket['quantity'])}")
    print(f"margin/leverage: {fmt_decimal(ticket['margin'])} USDT x {ticket['leverage']} = {fmt_decimal(ticket['notional'])} notional")
    print(f"mark reference: {fmt_decimal(ticket['mark'])}")
    print(f"take profit trigger: {fmt_decimal(ticket['target_stop_price'])} ({ticket['target_pct']}% underlying move)")
    print(f"stop trigger: {fmt_decimal(ticket['protective_stop_price'])} ({ticket['stop_pct']}% underlying move)")
    print(f"exit side: {ticket['exit_side']}")
    print(f"rough round-trip taker fee at 0.05% in/out: {fmt_decimal(ticket['estimated_round_trip_taker_fee'])} USDT")


def ignore_margin_type_error(exc: BinanceError) -> bool:
    text = str(exc)
    return "-4046" in text or "No need to change margin type" in text


def place_order(client: BinanceClient, ticket: dict[str, Any], live: bool, reserve: Decimal, require_confirmation: bool = True) -> None:
    print_ticket(ticket)
    if not live:
        print("\nDRY RUN ONLY. Add --live to place orders.")
        return

    allowed = allowed_symbols()
    if allowed and ticket["symbol"] not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise BinanceError(f"{ticket['symbol']} is not in local ALLOWED_SYMBOLS. Current whitelist: {allowed_text}")

    adjusted = adjusted_margin(client, ticket["margin"], reserve)
    if adjusted != ticket["margin"]:
        ticket.update(build_ticket(client, ticket["symbol"], ticket["bias"], adjusted, ticket["leverage"], ticket["target_pct"], ticket["stop_pct"]))
        print("\nRESIZED LIVE TICKET")
        print_ticket(ticket)

    phrase = f"PLACE {ticket['symbol']} {ticket['bias']} {fmt_decimal(ticket['margin'])}"
    if require_confirmation:
        typed = input(f"\nType exactly '{phrase}' to place LIVE Binance futures orders: ").strip()
        if typed != phrase:
            raise BinanceError("Confirmation did not match. No live order placed.")
    else:
        print(f"\nAUTO LIVE CONFIRMATION: {phrase}")

    symbol = ticket["symbol"]
    qty = fmt_decimal(ticket["quantity"])
    hedge_status = client.signed_get("/fapi/v1/positionSide/dual")
    if str(hedge_status.get("dualSidePosition", "")).lower() == "true":
        raise BinanceError("Account is in Hedge Mode. Switch Binance futures to One-way Mode before using this copilot.")
    multi_assets_status = client.signed_get("/fapi/v1/multiAssetsMargin")
    if str(multi_assets_status.get("multiAssetsMargin", "")).lower() == "true":
        raise BinanceError("Account is in Multi-Assets Mode. Turn it off in Binance futures settings so isolated margin can be used.")

    try:
        client.signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        print("margin type: ISOLATED")
    except BinanceError as exc:
        if ignore_margin_type_error(exc):
            print("margin type: already ISOLATED")
        else:
            raise

    leverage_response = client.signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": ticket["leverage"]})
    print(f"leverage set: {leverage_response}")

    entry = client.signed_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": symbol,
            "side": ticket["entry_side"],
            "type": "MARKET",
            "quantity": qty,
            "newOrderRespType": "RESULT",
        },
    )
    print(f"entry order: {entry}")
    avg_price = Decimal(str(entry.get("avgPrice") or "0"))
    if avg_price > 0:
        ticket["target_stop_price"] = round_to_tick(
            avg_price * (Decimal("1") + ticket["direction"] * ticket["target_pct"] / Decimal("100")),
            ticket["tick_size"],
        )
        ticket["protective_stop_price"] = round_to_tick(
            avg_price * (Decimal("1") - ticket["direction"] * ticket["stop_pct"] / Decimal("100")),
            ticket["tick_size"],
        )
        print(
            "exit triggers recalculated from fill: "
            f"target={fmt_decimal(ticket['target_stop_price'])} stop={fmt_decimal(ticket['protective_stop_price'])}"
        )

    try:
        stop = client.signed_request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": ticket["exit_side"],
                "type": "STOP_MARKET",
                "quantity": qty,
                "stopPrice": fmt_decimal(ticket["protective_stop_price"]),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
            },
        )
        print(f"stop order: {stop}")

        target = client.signed_request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": ticket["exit_side"],
                "type": "TAKE_PROFIT_MARKET",
                "quantity": qty,
                "stopPrice": fmt_decimal(ticket["target_stop_price"]),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
            },
        )
        print(f"take-profit order: {target}")
    except BinanceError:
        print("exit order placement failed; sending reduce-only emergency market close", file=sys.stderr)
        try:
            close = client.signed_request(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": symbol,
                    "side": ticket["exit_side"],
                    "type": "MARKET",
                    "quantity": qty,
                    "reduceOnly": "true",
                    "newOrderRespType": "RESULT",
                },
            )
            print(f"emergency close order: {close}", file=sys.stderr)
        finally:
            raise


def decimal_arg(value: str) -> Decimal:
    try:
        return Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value}") from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance USD-M futures scanner and guarded order helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="scan active Binance USD-M perpetuals")
    scan_parser.add_argument("--min-volume", type=float, default=5_000_000, help="minimum 24h quote volume in USD")
    scan_parser.add_argument("--limit", type=int, default=10, help="rows to print")
    scan_parser.add_argument("--candidates", type=int, default=70, help="candidate symbols to inspect with klines")

    judge_parser = sub.add_parser("judge", help="choose the best allowed setup or say WAIT")
    judge_parser.add_argument("--margin", type=decimal_arg, default=Decimal("5"), help="USDT margin to allocate")
    judge_parser.add_argument("--leverage", type=int, default=15)
    judge_parser.add_argument("--target-pct", type=decimal_arg, default=Decimal("6.8"))
    judge_parser.add_argument("--stop-pct", type=decimal_arg, default=Decimal("3"))
    judge_parser.add_argument("--reserve", type=decimal_arg, default=Decimal("0.5"), help="USDT to leave unused when account sizing is available")

    watch_parser = sub.add_parser("watch", help="run judgement in a loop and optionally trade rare clean setups")
    watch_parser.add_argument("--margin", type=decimal_arg, default=Decimal("5"), help="USDT margin to allocate")
    watch_parser.add_argument("--leverage", type=int, default=15)
    watch_parser.add_argument("--target-pct", type=decimal_arg, default=Decimal("6.8"))
    watch_parser.add_argument("--stop-pct", type=decimal_arg, default=Decimal("3"))
    watch_parser.add_argument("--reserve", type=decimal_arg, default=Decimal("0.5"), help="USDT to leave unused when account sizing is available")
    watch_parser.add_argument("--interval", type=int, default=60, help="seconds between judgement cycles")
    watch_parser.add_argument("--min-score", type=decimal_arg, default=Decimal("25"), help="minimum passing score required for watch signals")
    watch_parser.add_argument("--confirmations", type=int, default=2, help="number of consecutive matching passes required")
    watch_parser.add_argument("--max-trades-per-day", type=int, default=3)
    watch_parser.add_argument("--cooldown", type=int, default=900, help="seconds to wait after a signal/trade")
    watch_parser.add_argument("--max-cycles", type=int, default=None, help="stop after this many cycles; omit to run until Ctrl+C")
    watch_parser.add_argument("--live-auto", action="store_true", help="place live orders without typed confirmation when all watch gates pass")

    sub.add_parser("auth", help="check .env, current public IP, and signed Binance futures access")

    levels_parser = sub.add_parser("levels", help="print current levels for a symbol")
    levels_parser.add_argument("symbol")

    for name in ("ticket", "place"):
        p = sub.add_parser(name, help="build an order ticket" if name == "ticket" else "dry-run or place a guarded live order")
        p.add_argument("symbol")
        p.add_argument("side", choices=["LONG", "SHORT", "long", "short"])
        p.add_argument("--margin", type=decimal_arg, default=Decimal("5"), help="USDT margin to risk/allocate")
        p.add_argument("--leverage", type=int, default=15)
        p.add_argument("--target-pct", type=decimal_arg, default=Decimal("6.8"), help="underlying move needed for take profit")
        p.add_argument("--stop-pct", type=decimal_arg, default=Decimal("3"), help="underlying move against entry for stop")
        p.add_argument("--reserve", type=decimal_arg, default=Decimal("0.5"), help="USDT to leave unused in live mode")
        if name == "place":
            p.add_argument("--live", action="store_true", help="place live orders after typed confirmation")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv or sys.argv[1:])
    client = BinanceClient()
    try:
        if args.command == "scan":
            scan(client, min_volume=args.min_volume, limit=args.limit, candidates=args.candidates)
        elif args.command == "auth":
            auth_check(client)
        elif args.command == "judge":
            judge(client, args.margin, args.leverage, args.target_pct, args.stop_pct, args.reserve)
        elif args.command == "watch":
            watch(
                client,
                args.margin,
                args.leverage,
                args.target_pct,
                args.stop_pct,
                args.reserve,
                args.interval,
                args.min_score,
                args.confirmations,
                args.live_auto,
                args.max_trades_per_day,
                args.cooldown,
                args.max_cycles,
            )
        elif args.command == "levels":
            print_levels(client, args.symbol)
        elif args.command in {"ticket", "place"}:
            ticket = build_ticket(client, args.symbol, args.side, args.margin, args.leverage, args.target_pct, args.stop_pct)
            if args.command == "ticket":
                print_ticket(ticket)
            else:
                place_order(client, ticket, args.live, args.reserve)
        else:
            raise BinanceError(f"Unknown command: {args.command}")
    except BinanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
