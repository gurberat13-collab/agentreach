# -*- coding: utf-8 -*-
"""
Teknik sinyal motoru — MEXC Spot + MEXC Futures + (opsiyonel) Yahoo.

Desteklenen varliklar:
  Kripto spot : MEXC spot ciftleri (BTCUSDT, ENJUSDT, ...)
  Makro/futures: MEXC futures kontratlari
      GOLD    -> XAUT_USDT
      OIL     -> USOIL_USDT
      NASDAQ  -> NAS100_USDT
      (ayrica SP500/SPX, US30, SILVER, BRENT, DXY)
  Opsiyonel: Yahoo ticker (ornek: GC=F, CL=F, ^IXIC, AAPL)

Kullanim:
  python scripts/crypto_signal.py signal --coin ENJUSDT
  python scripts/crypto_signal.py signal --coin GOLD
  python scripts/crypto_signal.py signal --coin OIL --interval 60m
  python scripts/crypto_signal.py signal --coin NASDAQ
  python scripts/crypto_signal.py watchlist --coins BTCUSDT,ETHUSDT,GOLD,OIL,NASDAQ
  python scripts/crypto_signal.py watchlist --coins ENJUSDT,GOLD,OIL --watch --every 120
  python scripts/crypto_signal.py watchlist --preset macro
  python scripts/crypto_signal.py watchlist --preset hybrid
  python scripts/crypto_signal.py watchlist --preset scalping --interval 5m
  python scripts/crypto_signal.py watchlist --preset scalping --add-top-alts 8
  python scripts/crypto_signal.py signal --coin BTCUSDT --mtf --interval 5m
  python scripts/crypto_signal.py watchlist --preset scalping --interval 5m --mtf --smart-telegram
  python scripts/crypto_signal.py watchlist --coins BTCUSDT,ETHUSDT --mtf --telegram-all

Ortam:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  AGENT_REACH_DATA_DIR — durum/log dosyalari (varsayilan: calisma dizini)
  AGENT_REACH_SMART_TELEGRAM=1 — watchlist'te --smart-telegram ile ayni etki
  AGENT_REACH_ALERT_MIN_CONF — akilli alarm icin minimum guven (varsayilan 55)
  AGENT_REACH_ACCOUNT_USDT, AGENT_REACH_RISK_PCT — islem plani pozisyon boyutu
  AGENT_REACH_WEBHOOK_URL — harici otomasyon webhook (n8n, Make, Zapier, kendi API’n…); JSON POST
      (Eski isim: AGENT_REACH_RELAY_WEBHOOK_URL — hala calisir.)
  AGENT_REACH_WEBHOOK_DEDUP=1 — deduplication anahtari ekler (varsayilan acik)
  AGENT_REACH_TELEGRAM_VIA_WEBHOOK=1 — Telegram’i script’ten gonderme; webhook zincirinde gonder
      (Eski isim: AGENT_REACH_TELEGRAM_VIA_RELAY)

Railway (https://railway.com/):
  Repoyu bagla; Variables: TELEGRAM_*, istege AGENT_REACH_*.
  Start Command: pip install -r requirements-crypto-worker.txt && python scripts/crypto_signal.py
    watchlist --preset scalping --interval 5m --mtf --smart-telegram --watch --every 120
  Start: repo kokunde railpack.json (deploy.startCommand) — Railway Railpack bunu okur.
  Alternatif: Service Settings > Start Command veya crypto-signal.railway.toml satiri.
  Kalici state icin Volume + AGENT_REACH_DATA_DIR=/data (yoksa redeploy’da state sifirlanir).

Onemli: Hicbir cikti yatirim tavsiyesi degildir. Karar size aittir.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ---------------------------------------------------------------------------
# Kisayol eslestirme
# ---------------------------------------------------------------------------
# GOLD/OIL/NASDAQ ve benzerleri once MEXC futures'a gitsin
_MEXC_FUTURES_ALIASES: dict[str, str] = {
    "GOLD": "XAUT_USDT",
    "ALTIN": "XAUT_USDT",
    "XAU": "XAUT_USDT",
    "XAUUSD": "XAUT_USDT",
    "OIL": "USOIL_USDT",
    "PETROL": "USOIL_USDT",
    "CRUDE": "USOIL_USDT",
    "BRENT": "UKOIL_USDT",
    "NASDAQ": "NAS100_USDT",
    "NQ": "NAS100_USDT",
    "SP500": "SPX500_USDT",
    "SPX": "SPX500_USDT",
    "US30": "US30_USDT",
    "DJI": "US30_USDT",
    "SILVER": "XAG_USDT",
    "GUMUS": "XAG_USDT",
    "DXY": "DXY_USDT",
}

_YAHOO_ALIASES: dict[str, str] = {}

_WATCHLIST_PRESETS: dict[str, list[str]] = {
    # Kullanici istegi: SP500 + US30 + BRENT dahil makro sepet
    "macro": ["GOLD", "OIL", "BRENT", "NASDAQ", "SP500", "US30"],
    # Majors + makro bir arada
    "hybrid": [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "GOLD",
        "OIL",
        "BRENT",
        "NASDAQ",
        "SP500",
        "US30",
    ],
    # Kisa vadeli hizli takip: major kripto + secili makro
    "scalping": [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "ENJUSDT",
        "GOLD",
        "OIL",
        "NASDAQ",
    ],
}

_DEFAULT_SCALPING_TOP_ALTS = 5

# Kalici durum / log (repo kokune veya cwd'ye)
def _data_dir() -> Path:
    return Path(os.environ.get("AGENT_REACH_DATA_DIR", Path.cwd())).resolve()


def _data_path(name: str) -> Path:
    return _data_dir() / name


_ALERT_STATE_FILE = "agent_reach_signal_alert_state.json"
_FLOW_STATE_FILE = "agent_reach_flow_holdvol.json"
_SIGNAL_LOG_FILE = "agent_reach_signal_log.jsonl"
_PAPER_TRADES_FILE = "agent_reach_paper_trades.json"
_NEWS_BOOST_FILE = "agent_reach_news_boost.json"

# Spot baz -> sektor (rotasyon ozeti icin)
_SECTOR_BY_BASE: dict[str, str] = {
    "BTC": "MAJOR",
    "ETH": "MAJOR",
    "SOL": "L1",
    "XRP": "MAJOR",
    "DOGE": "MEME",
    "PEPE": "MEME",
    "SHIB": "MEME",
    "BONK": "MEME",
    "WIF": "MEME",
    "FET": "AI",
    "RENDER": "AI",
    "TAO": "AI",
    "NEAR": "L1",
    "ATOM": "L1",
    "SUI": "L1",
    "APT": "L1",
    "ARB": "L2",
    "OP": "L2",
    "MATIC": "L2",
    "POL": "L2",
    "LINK": "INFRA",
    "ENJ": "GAMING",
}


def _load_json_file(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def spot_to_futures_contract(symbol: str) -> str | None:
    """BTCUSDT -> BTC_USDT (MEXC futures). Ozel alias'lar haric."""
    s = symbol.upper()
    if _is_mexc_futures_symbol(s):
        return _resolve_mexc_futures(s)
    if not s.endswith("USDT") or _is_yahoo_symbol(s):
        return None
    base = s[:-4]
    if not base or not base.isalnum():
        return None
    return f"{base}_USDT"


def fetch_futures_ticker_raw(contract: str) -> dict[str, Any] | None:
    url = "https://contract.mexc.com/api/v1/contract/ticker"
    try:
        r = requests.get(url, params={"symbol": contract}, timeout=12)
        r.raise_for_status()
        raw = r.json()
    except Exception:
        return None
    if not raw.get("success"):
        return None
    return raw.get("data") or {}


def fetch_flow_context(symbol: str) -> dict[str, Any]:
    """
    Funding + holdVol + bir onceki taramaya gore holdVol degisimi.
    Spot coinlerde ayni isimli USDT-M kontrat denenir.
    """
    contract = spot_to_futures_contract(symbol)
    if not contract:
        return {"available": False, "reason": "Futures eslestirmesi yok"}

    data = fetch_futures_ticker_raw(contract)
    if not data:
        return {"available": False, "reason": "Ticker alinamadi", "contract": contract}

    hold = float(data.get("holdVol", 0) or 0)
    fr = float(data.get("fundingRate", 0) or 0)
    path = _data_path(_FLOW_STATE_FILE)
    prev_map: dict[str, Any] = _load_json_file(path, {})
    prev_h = float(prev_map.get(contract, {}).get("hold_vol", 0) or 0)
    delta_pct = None
    if prev_h > 0:
        delta_pct = round((hold - prev_h) / prev_h * 100, 3)
    prev_map[contract] = {"hold_vol": hold, "ts": time.time()}
    _save_json_file(path, prev_map)

    # Funding: pozitif long odeme (genelde short bias yorumu), negatif short odeme
    bias = "NEUTRAL"
    if fr > 0.0001:
        bias = "SHORT_PRESSURE"  # long'lar fee oduyor
    elif fr < -0.0001:
        bias = "LONG_PRESSURE"

    return {
        "available": True,
        "contract": contract,
        "funding_rate": fr,
        "hold_vol": hold,
        "hold_vol_delta_pct": delta_pct,
        "bias": bias,
        "last_price": float(data.get("lastPrice", 0) or 0),
    }


def apply_flow_to_signal(sig: dict[str, Any], flow: dict[str, Any]) -> dict[str, Any]:
    """Skora hafif futures akisi ekler; kopya uzerinde calisir."""
    out = dict(sig)
    reasons = list(out.get("reasons", []))
    if not flow.get("available"):
        out["flow_note"] = flow.get("reason", "")
        return out
    if out.get("direction") == "NOTR":
        out["reasons"] = reasons
        out["flow"] = flow
        return out

    d = out["direction"]
    fr = float(flow.get("funding_rate", 0) or 0)
    dv = flow.get("hold_vol_delta_pct")
    adj = 0.0

    if d == "LONG":
        if fr < -0.00005:
            adj += 0.5
            reasons.append("Funding short tarafinda (long icin tailwind)")
        elif fr > 0.00015:
            adj -= 0.5
            reasons.append("Funding yuksek (long tasima maliyeti)")
        if dv is not None and dv > 1.0:
            adj += 0.5
            reasons.append(f"Acik pozisyon artisi (%{dv:+.2f} holdVol)")
        elif dv is not None and dv < -1.0:
            adj -= 0.5
            reasons.append(f"Acik pozisyon azalisi (%{dv:+.2f} holdVol)")
    elif d == "SHORT":
        if fr > 0.00005:
            adj += 0.5
            reasons.append("Funding long tarafinda (short icin tailwind)")
        elif fr < -0.00015:
            adj -= 0.5
            reasons.append("Funding negatif (short tasima maliyeti)")
        if dv is not None and dv > 1.0:
            adj -= 0.5
            reasons.append(f"OI artisi — short squeeze riski (holdVol %{dv:+.2f})")
        elif dv is not None and dv < -1.0:
            adj += 0.5
            reasons.append("OI dususu — trend destegi zayiflayabilir")

    sc = float(out["score"]) + adj
    sc = max(-10.0, min(10.0, sc))
    out["score"] = round(sc, 1)
    if out["score"] >= 3:
        out["direction"] = "LONG"
    elif out["score"] <= -3:
        out["direction"] = "SHORT"
    else:
        out["direction"] = "NOTR"
    st = abs(out["score"])
    out["strength"] = "GUCLU" if st >= 6 else "ORTA" if st >= 3 else "ZAYIF"
    out["reasons"] = reasons
    out["flow"] = flow
    return out


def calc_atr(candles: list[dict[str, float]], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 8)


def build_trade_plan(
    candles: list[dict[str, float]],
    direction: str,
    price: float,
    atr_mult_sl: float = 1.5,
    atr_mult_tp1: float = 1.0,
    atr_mult_tp2: float = 2.0,
) -> dict[str, Any]:
    atr = calc_atr(candles, 14)
    if atr <= 0 or direction == "NOTR":
        return {"atr": atr, "available": False}

    if direction == "LONG":
        sl = price - atr * atr_mult_sl
        tp1 = price + atr * atr_mult_tp1
        tp2 = price + atr * atr_mult_tp2
        risk = price - sl
        reward = tp1 - price
    else:
        sl = price + atr * atr_mult_sl
        tp1 = price - atr * atr_mult_tp1
        tp2 = price - atr * atr_mult_tp2
        risk = sl - price
        reward = price - tp1

    rr = round(reward / risk, 2) if risk > 0 else 0.0
    account = float(os.environ.get("AGENT_REACH_ACCOUNT_USDT", "10000") or 10000)
    risk_pct = float(os.environ.get("AGENT_REACH_RISK_PCT", "0.01") or 0.01)
    risk_usd = account * risk_pct
    pos_units = risk_usd / risk if risk > 0 else 0.0

    return {
        "available": True,
        "atr": atr,
        "entry": round(price, 8),
        "sl": round(sl, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "rr_tp1": rr,
        "risk_per_unit": round(risk, 8),
        "suggested_units": round(pos_units, 6),
        "risk_usd": round(risk_usd, 2),
    }


def mtf_alignment(primary_dir: str, d15: str, d60: str) -> tuple[str, str]:
    if primary_dir == "NOTR":
        return "N/A", "Primary NOTR — MTF teyit anlamsiz"
    opp_p = "SHORT" if primary_dir == "LONG" else "LONG"
    if d15 == opp_p or d60 == opp_p:
        return "CONFLICT", "Ust periyotlar yone karsi — dikkat"
    if d15 == primary_dir and d60 == primary_dir:
        return "FULL", "15m ve 1h ayni yonde — guclu teyit"
    if d15 == primary_dir or d60 == primary_dir:
        return "PARTIAL", "Kismi teyit (bir periyot uyumlu)"
    return "WEAK", "Ust periyotlar NOTR / zayif uyum"


def compute_confidence(
    sig: dict[str, Any],
    mtf_label: str,
    flow: dict[str, Any],
    news_boost: float,
) -> int:
    base = 35 + min(abs(float(sig.get("score", 0))) * 4, 28)
    conf = base
    if mtf_label == "FULL":
        conf += 22
    elif mtf_label == "PARTIAL":
        conf += 12
    elif mtf_label == "WEAK":
        conf += 4
    elif mtf_label == "CONFLICT":
        conf -= 28

    if flow.get("available"):
        d = sig.get("direction")
        b = flow.get("bias")
        if d == "LONG" and b == "LONG_PRESSURE":
            conf += 6
        elif d == "SHORT" and b == "SHORT_PRESSURE":
            conf += 6
        elif d in ("LONG", "SHORT") and b != "NEUTRAL":
            fr = float(flow.get("funding_rate", 0) or 0)
            if (d == "LONG" and fr > 0.0002) or (d == "SHORT" and fr < -0.0002):
                conf -= 5

    conf += max(-15, min(15, news_boost * 10))
    return int(max(0, min(100, round(conf))))


def load_news_boost_for_symbol(symbol: str) -> float:
    """Basit JSON: {\"BTC\": 1.2, \"ETH\": 0.5} — carpim confidence'a eklenir."""
    p = _data_path(_NEWS_BOOST_FILE)
    data = _load_json_file(p, {})
    if not isinstance(data, dict):
        return 0.0
    s = symbol.upper()
    base = s.replace("USDT", "").replace("_USDT", "").strip()
    for k, v in data.items():
        if str(k).upper() == base or str(k).upper() == s:
            try:
                return float(v)
            except Exception:
                return 0.0
    return 0.0


def analyze_mtf(symbol: str, primary_iv: str) -> dict[str, Any]:
    """5m/15m/60m sinyalleri + birlestirme + guven + plan."""
    sig_p = analyze(symbol, primary_iv)
    sig_15 = analyze(symbol, "15m")
    sig_60 = analyze(symbol, "60m")
    mtf_lbl, mtf_txt = mtf_alignment(sig_p["direction"], sig_15["direction"], sig_60["direction"])

    flow = fetch_flow_context(symbol)
    sig_f = apply_flow_to_signal(sig_p, flow)
    nb = load_news_boost_for_symbol(symbol)
    conf = compute_confidence(sig_f, mtf_lbl, flow, nb)

    candles_p = fetch_klines(symbol, interval=primary_iv, limit=100)
    plan = build_trade_plan(candles_p, sig_f["direction"], float(sig_f["price"]))

    return {
        "symbol": symbol.upper(),
        "interval_primary": primary_iv,
        "signal": sig_f,
        "mtf": {
            "15m": {"direction": sig_15["direction"], "score": sig_15["score"]},
            "60m": {"direction": sig_60["direction"], "score": sig_60["score"]},
            "label": mtf_lbl,
            "detail": mtf_txt,
        },
        "confidence": conf,
        "trade_plan": plan,
        "news_boost": nb,
    }


def _score_bucket(score: float) -> int:
    return int(min(10, max(0, abs(float(score)))))


def _alert_state_path() -> Path:
    return _data_path(_ALERT_STATE_FILE)


def should_send_smart_alert(symbol: str, pack: dict[str, Any]) -> bool:
    """Yon degisimi, NOTR->yonlu, veya guven esigi gecisi."""
    sig = pack.get("signal") or {}
    direction = str(sig.get("direction", "NOTR"))
    conf = int(pack.get("confidence") or 0)
    score = float(sig.get("score") or 0)
    min_conf = int(os.environ.get("AGENT_REACH_ALERT_MIN_CONF", "55") or 55)
    strong_score = float(os.environ.get("AGENT_REACH_ALERT_STRONG_SCORE", "4") or 4)

    path = _alert_state_path()
    st: dict[str, Any] = _load_json_file(path, {})
    key = symbol.upper()
    prev = st.get(key) or {}
    prev_dir = str(prev.get("direction", ""))
    prev_conf = int(prev.get("confidence", 0))
    prev_bucket = int(prev.get("bucket", 0))

    st[key] = {
        "direction": direction,
        "confidence": conf,
        "bucket": _score_bucket(score),
        "ts": time.time(),
    }
    _save_json_file(path, st)

    if not prev:
        return conf >= min_conf and direction != "NOTR"
    if direction != prev_dir:
        return direction != "NOTR" or prev_dir != "NOTR"
    if prev_dir == "NOTR" and direction in ("LONG", "SHORT"):
        return conf >= min_conf
    if conf >= min_conf and prev_conf < min_conf:
        return True
    b_now = _score_bucket(score)
    if b_now >= strong_score and prev_bucket < strong_score and conf >= min_conf:
        return True
    return False


def append_signal_log(entry: dict[str, Any]) -> None:
    path = _data_path(_SIGNAL_LOG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def paper_record(symbol: str, outcome: str, pnl_pct: float | None, note: str = "") -> None:
    path = _data_path(_PAPER_TRADES_FILE)
    rows: list[Any] = _load_json_file(path, [])
    if not isinstance(rows, list):
        rows = []
    rows.append({
        "symbol": symbol.upper(),
        "outcome": outcome,
        "pnl_pct": pnl_pct,
        "note": note,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    _save_json_file(path, rows)


def compute_stats_from_paper() -> dict[str, Any]:
    path = _data_path(_PAPER_TRADES_FILE)
    rows: list[Any] = _load_json_file(path, [])
    if not isinstance(rows, list) or not rows:
        return {"n": 0, "msg": "Paper trade kaydi yok"}
    pnls = [float(r["pnl_pct"]) for r in rows if r.get("pnl_pct") is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses_ct = sum(1 for p in pnls if p < 0)
    n = len(pnls)
    win_rate = round(wins / n * 100, 1) if n else 0.0
    avg = round(sum(pnls) / n, 4) if n else 0.0
    equity = 100.0
    peak = 100.0
    max_dd = 0.0
    for p in pnls:
        equity *= 1.0 + p / 100.0
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100 if peak else 0)
    return {
        "trades": n,
        "win_rate_pct": win_rate,
        "wins": wins,
        "losses": losses_ct,
        "expectancy_pct_per_trade": avg,
        "max_drawdown_pct_approx": round(max_dd, 2),
    }


def sector_for_symbol(symbol: str) -> str:
    s = symbol.upper().replace("USDT", "")
    if "(" in s:
        s = s.split("(")[0]
    return _SECTOR_BY_BASE.get(s, "DIGER")


def run_backtest_simple(symbol: str, interval: str, bars: int = 400) -> dict[str, Any]:
    """Gecmis mumlarda sinyal yonunun kisa vadeli sonucu (kaba simulasyon)."""
    candles = fetch_klines(symbol, interval=interval, limit=min(bars, 1000))
    if len(candles) < 80:
        return {"error": "Yetersiz veri"}
    wins = 0
    losses = 0
    ticker = fetch_ticker(symbol)
    fwd = 8
    for i in range(60, len(candles) - fwd - 1):
        sub = candles[: i + 1]
        sig = generate_signal(sub, ticker)
        d = sig["direction"]
        if d == "NOTR":
            continue
        entry = sub[-1]["close"]
        future = candles[i + 1 : i + 1 + fwd]
        hi = max(c["high"] for c in future)
        lo = min(c["low"] for c in future)
        if d == "LONG":
            if hi >= entry * 1.002:
                wins += 1
            elif lo <= entry * 0.998:
                losses += 1
        elif d == "SHORT":
            if lo <= entry * 0.998:
                wins += 1
            elif hi >= entry * 1.002:
                losses += 1
    tot = wins + losses
    wr = round(wins / tot * 100, 1) if tot else 0.0
    return {"symbol": symbol.upper(), "interval": interval, "samples": tot, "win_rate_pct": wr, "wins": wins, "losses": losses}


# yfinance interval eslestirme
_YF_INTERVAL_MAP: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "60m": "60m", "1h": "60m", "4h": "60m",  # Yahoo 4h desteklemez, en yakin 60m
    "1d": "1d",
}

# MEXC futures interval eslestirme
_MEXC_FUTURES_INTERVAL_MAP: dict[str, str] = {
    "1m": "Min1",
    "5m": "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "60m": "Min60",
    "1h": "Min60",
    "4h": "Hour4",
    "1d": "Day1",
}


def _resolve_mexc_futures(symbol: str) -> str:
    return _MEXC_FUTURES_ALIASES.get(symbol.upper(), symbol.upper())


def _is_mexc_futures_symbol(symbol: str) -> bool:
    s = symbol.upper()
    if s in _MEXC_FUTURES_ALIASES:
        return True
    # Ham futures sembolu: BTC_USDT, XAUT_USDT, NAS100_USDT...
    if "_" in s and s.endswith(("_USDT", "_USDC", "_USD1")):
        return True
    return False


def _is_yahoo_symbol(symbol: str) -> bool:
    s = symbol.upper()
    if _is_mexc_futures_symbol(s):
        return False
    if s in _YAHOO_ALIASES:
        return True
    if "=" in s or s.startswith("^") or s.startswith("$"):
        return True
    return False


def _resolve_yahoo(symbol: str) -> str:
    return _YAHOO_ALIASES.get(symbol.upper(), symbol)


def _resolve_watchlist_symbols(coins_arg: str | None, preset_arg: str | None) -> list[str]:
    if coins_arg:
        coins = [c.strip().upper() for c in coins_arg.split(",") if c.strip()]
        if coins:
            return coins
    if preset_arg:
        return _WATCHLIST_PRESETS[preset_arg]
    raise ValueError("watchlist icin --coins veya --preset gerekli")


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def fetch_top_volume_altcoins(limit: int, exclude: set[str] | None = None) -> list[str]:
    """
    MEXC spot'tan 24h quoteVolume'a gore en yuksek hacimli altcoinleri getirir.
    - Sadece USDT spot ciftleri
    - Leveraged tokenlari ve majorlari dislar
    """
    if limit <= 0:
        return []

    exclude_u = {x.upper() for x in (exclude or set())}
    major_exclude = {
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "TRXUSDT",
        "TONUSDT",
        "ADAUSDT",
    }
    exclude_u |= major_exclude

    stable_bases = {
        "USDT",
        "USDC",
        "FDUSD",
        "TUSD",
        "USDD",
        "USDE",
        "USDP",
        "DAI",
        "USD1",
        "PYUSD",
        "BUSD",
    }

    url = f"{MEXC_BASE}/api/v3/ticker/24hr"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        return []

    # Sembol kalitesi icin basit filtre:
    # - yalnizca ALNUM + USDT (ornek: SUIUSDT)
    # - parantezli/ozel formatlar dislansin
    # - leveraged token desenlerini disla
    sym_re = re.compile(r"^[A-Z0-9]+USDT$")
    lev_re = re.compile(r"(UP|DOWN|BULL|BEAR|[235]L|[235]S)USDT$")

    cands: list[tuple[str, float]] = []
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol.endswith("USDT"):
            continue
        if not sym_re.match(symbol):
            continue
        if lev_re.search(symbol):
            continue
        if symbol in exclude_u:
            continue
        base = symbol[:-4]  # USDT quote'u cikar
        if base in stable_bases:
            continue
        # USD* formatli sentetik/stable benzeri coinleri de disla
        if base.startswith("USD"):
            continue

        qv = row.get("quoteVolume")
        vol = 0.0
        try:
            vol = float(qv) if qv is not None else 0.0
        except Exception:
            vol = 0.0
        if vol <= 0:
            continue
        cands.append((symbol, vol))

    cands.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in cands[:limit]]


# ---------------------------------------------------------------------------
# MEXC API
# ---------------------------------------------------------------------------
MEXC_BASE = "https://api.mexc.com"


def fetch_klines_mexc(symbol: str, interval: str = "15m", limit: int = 100) -> list[dict[str, float]]:
    url = f"{MEXC_BASE}/api/v3/klines"
    params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    raw = r.json()
    candles: list[dict[str, float]] = []
    for c in raw:
        candles.append({
            "time": float(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })
    return candles


def fetch_ticker_mexc(symbol: str) -> dict[str, Any]:
    url = f"{MEXC_BASE}/api/v3/ticker/24hr"
    r = requests.get(url, params={"symbol": symbol.upper()}, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_klines_mexc_futures(symbol: str, interval: str = "15m", limit: int = 100) -> list[dict[str, float]]:
    fsym = _resolve_mexc_futures(symbol)
    mexc_interval = _MEXC_FUTURES_INTERVAL_MAP.get(interval, "Min15")
    url = f"https://contract.mexc.com/api/v1/contract/kline/{fsym}"
    r = requests.get(url, params={"interval": mexc_interval, "limit": limit}, timeout=15)
    r.raise_for_status()
    raw = r.json()
    data = raw.get("data") or {}

    times = data.get("time") or []
    opens = data.get("open") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    vols = data.get("vol") or []
    n = min(len(times), len(opens), len(highs), len(lows), len(closes), len(vols))
    if n == 0:
        raise ValueError(f"MEXC futures verisi alinamadi: {fsym}")

    candles: list[dict[str, float]] = []
    for i in range(n):
        t = float(times[i])
        # Futures endpoint saniye donuyor; milisaniye standardina yaklastir
        if t < 10_000_000_000:
            t *= 1000
        candles.append({
            "time": t,
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
            "volume": float(vols[i]),
        })
    return candles


def fetch_ticker_mexc_futures(symbol: str) -> dict[str, Any]:
    fsym = _resolve_mexc_futures(symbol)
    url = "https://contract.mexc.com/api/v1/contract/ticker"
    r = requests.get(url, params={"symbol": fsym}, timeout=10)
    r.raise_for_status()
    raw = r.json()
    data = raw.get("data") or {}
    # riseFallRate: 0.0123 => +1.23%
    change = float(data.get("riseFallRate", 0) or 0) * 100
    return {"priceChangePercent": round(change, 2)}


# ---------------------------------------------------------------------------
# Yahoo Finance (yfinance)
# ---------------------------------------------------------------------------

def fetch_klines_yahoo(yf_symbol: str, interval: str = "15m", limit: int = 100) -> list[dict[str, float]]:
    import yfinance as yf

    yf_int = _YF_INTERVAL_MAP.get(interval, "15m")
    # yfinance intraday: 1m -> 7 gun, 5m/15m/30m/60m -> 60 gun, 1d -> max
    period = "60d" if yf_int in ("5m", "15m", "30m", "60m") else "7d" if yf_int == "1m" else "1y"
    tk = yf.Ticker(yf_symbol)
    df = tk.history(period=period, interval=yf_int)
    if df.empty:
        raise ValueError(f"Yahoo'dan veri alinamadi: {yf_symbol} ({yf_int})")

    df = df.tail(limit)
    candles: list[dict[str, float]] = []
    for idx, row in df.iterrows():
        candles.append({
            "time": float(idx.timestamp()) if hasattr(idx, "timestamp") else 0,
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": float(row.get("Volume", 0)),
        })
    return candles


def fetch_ticker_yahoo(yf_symbol: str) -> dict[str, Any]:
    import yfinance as yf

    tk = yf.Ticker(yf_symbol)
    info = tk.fast_info
    try:
        prev = info.previous_close
        last = info.last_price
        change = ((last - prev) / prev) * 100 if prev else 0
    except Exception:
        change = 0
    return {"priceChangePercent": round(change, 2)}


# ---------------------------------------------------------------------------
# Birlestirici: sembol'e gore dogru API'yi sec
# ---------------------------------------------------------------------------

def fetch_klines(symbol: str, interval: str = "15m", limit: int = 100) -> list[dict[str, float]]:
    if _is_mexc_futures_symbol(symbol):
        return fetch_klines_mexc_futures(symbol, interval, limit)
    if _is_yahoo_symbol(symbol):
        return fetch_klines_yahoo(_resolve_yahoo(symbol), interval, limit)
    return fetch_klines_mexc(symbol, interval, limit)


def fetch_ticker(symbol: str) -> dict[str, Any]:
    if _is_mexc_futures_symbol(symbol):
        return fetch_ticker_mexc_futures(symbol)
    if _is_yahoo_symbol(symbol):
        return fetch_ticker_yahoo(_resolve_yahoo(symbol))
    return fetch_ticker_mexc(symbol)


def display_name(symbol: str) -> str:
    s = symbol.upper()
    if s in _YAHOO_ALIASES:
        return s
    return s


# ---------------------------------------------------------------------------
# Teknik gostergeler
# ---------------------------------------------------------------------------

def calc_ema(closes: list[float], period: int) -> list[float]:
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def calc_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calc_macd(closes: list[float]) -> dict[str, float]:
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if not ema12 or not ema26:
        return {"macd": 0, "signal": 0, "hist": 0}
    offset = len(ema12) - len(ema26)
    macd_line = [ema12[offset + i] - ema26[i] for i in range(len(ema26))]
    signal_line = calc_ema(macd_line, 9)
    if not signal_line:
        return {"macd": macd_line[-1] if macd_line else 0, "signal": 0, "hist": 0}
    hist = macd_line[-1] - signal_line[-1]
    return {
        "macd": round(macd_line[-1], 6),
        "signal": round(signal_line[-1], 6),
        "hist": round(hist, 6),
    }


def calc_volume_ratio(candles: list[dict[str, float]], lookback: int = 20) -> float:
    if len(candles) < lookback + 1:
        return 1.0
    avg = sum(c["volume"] for c in candles[-(lookback + 1):-1]) / lookback
    if avg == 0:
        return 1.0
    return round(candles[-1]["volume"] / avg, 2)


def calc_support_resistance(candles: list[dict[str, float]], window: int = 20) -> dict[str, float]:
    recent = candles[-window:]
    lows = [c["low"] for c in recent]
    highs = [c["high"] for c in recent]
    return {
        "support": round(min(lows), 6),
        "resistance": round(max(highs), 6),
    }


# ---------------------------------------------------------------------------
# Sinyal motoru
# ---------------------------------------------------------------------------

def generate_signal(candles: list[dict[str, float]], ticker: dict[str, Any]) -> dict[str, Any]:
    closes = [c["close"] for c in candles]
    current = closes[-1]

    rsi = calc_rsi(closes, 14)
    macd = calc_macd(closes)
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    vol_ratio = calc_volume_ratio(candles)
    sr = calc_support_resistance(candles)

    score = 0.0
    reasons: list[str] = []

    # RSI
    if rsi < 30:
        score += 2.5
        reasons.append(f"RSI asiri satim ({rsi})")
    elif rsi < 40:
        score += 1.0
        reasons.append(f"RSI dusuk ({rsi})")
    elif rsi > 70:
        score -= 2.5
        reasons.append(f"RSI asiri alim ({rsi})")
    elif rsi > 60:
        score -= 1.0
        reasons.append(f"RSI yuksek ({rsi})")

    # EMA cross
    if ema9 and ema21:
        if ema9[-1] > ema21[-1]:
            score += 1.5
            reasons.append("EMA9 > EMA21 (yukari trend)")
        else:
            score -= 1.5
            reasons.append("EMA9 < EMA21 (asagi trend)")

    if ema50 and current > ema50[-1]:
        score += 1.0
        reasons.append("Fiyat > EMA50")
    elif ema50:
        score -= 1.0
        reasons.append("Fiyat < EMA50")

    # MACD
    if macd["hist"] > 0:
        score += 1.5
        reasons.append("MACD histogram pozitif")
    else:
        score -= 1.5
        reasons.append("MACD histogram negatif")

    if macd["macd"] > macd["signal"]:
        score += 1.0
        reasons.append("MACD > sinyal cizgisi")
    else:
        score -= 1.0
        reasons.append("MACD < sinyal cizgisi")

    # Hacim
    if vol_ratio > 2.0:
        reasons.append(f"Hacim patlamasi ({vol_ratio}x)")
        if candles[-1]["close"] > candles[-1]["open"]:
            score += 2.0
            reasons.append("Hacimli ALIS baskisi")
        else:
            score -= 2.0
            reasons.append("Hacimli SATIS baskisi")
    elif vol_ratio > 1.5:
        reasons.append(f"Hacim ortalamanin uzerinde ({vol_ratio}x)")

    # Destek / Direnc
    dist_support = (current - sr["support"]) / current if current else 0
    dist_resist = (sr["resistance"] - current) / current if current else 0
    if dist_support < 0.01:
        score += 1.0
        reasons.append("Destege cok yakin (dip alim?)")
    if dist_resist < 0.01:
        score -= 1.0
        reasons.append("Dirence cok yakin (tepe?)")

    # 24h degisim
    change_24h = float(ticker.get("priceChangePercent", 0) or 0)
    if change_24h > 10:
        score -= 0.5
        reasons.append(f"24h +{change_24h:.1f}% (asiri yukseldi, geri cekilme riski)")
    elif change_24h < -10:
        score += 0.5
        reasons.append(f"24h {change_24h:.1f}% (asiri dustu, toparlanma mumkun)")

    score = max(-10, min(10, score))
    if score >= 3:
        direction = "LONG"
    elif score <= -3:
        direction = "SHORT"
    else:
        direction = "NOTR"

    strength = abs(score)
    if strength >= 6:
        strength_label = "GUCLU"
    elif strength >= 3:
        strength_label = "ORTA"
    else:
        strength_label = "ZAYIF"

    return {
        "direction": direction,
        "score": round(score, 1),
        "strength": strength_label,
        "rsi": rsi,
        "macd": macd,
        "ema9": round(ema9[-1], 6) if ema9 else None,
        "ema21": round(ema21[-1], 6) if ema21 else None,
        "ema50": round(ema50[-1], 6) if ema50 else None,
        "volume_ratio": vol_ratio,
        "support": sr["support"],
        "resistance": sr["resistance"],
        "price": current,
        "change_24h": change_24h,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Gorsellestime
# ---------------------------------------------------------------------------

_DIR_STYLE = {"LONG": "bold green", "SHORT": "bold red", "NOTR": "bold yellow"}
_DIR_EMOJI = {"LONG": "^", "SHORT": "v", "NOTR": "-"}

# Fiyat format: buyuk fiyatlar icin az ondalik, kucukler icin cok
def _fmt_price(p: float | None) -> str:
    if p is None:
        return "N/A"
    if abs(p) >= 100:
        return f"${p:,.2f}"
    if abs(p) >= 1:
        return f"${p:,.4f}"
    return f"${p:,.6f}"


def render_signal(console: Console, symbol: str, sig: dict[str, Any], interval: str) -> None:
    d = sig["direction"]
    style = _DIR_STYLE.get(d, "white")
    arrow = _DIR_EMOJI.get(d, "?")

    lines = [
        f"[{style}]{arrow} {d} -- {sig['strength']} (skor: {sig['score']:+.1f})[/{style}]",
        "",
        f"Fiyat: [bold]{_fmt_price(sig['price'])}[/bold]   24h: {'+' if sig['change_24h'] >= 0 else ''}{sig['change_24h']:.2f}%",
        f"RSI(14): {sig['rsi']}   |   Hacim: {sig['volume_ratio']}x ortalama",
        f"EMA9: {_fmt_price(sig['ema9'])}   EMA21: {_fmt_price(sig['ema21'])}   EMA50: {_fmt_price(sig['ema50'])}",
        f"MACD: {sig['macd']['macd']:.4f}  Sinyal: {sig['macd']['signal']:.4f}  Hist: {sig['macd']['hist']:.4f}",
        f"Destek: {_fmt_price(sig['support'])}   Direnc: {_fmt_price(sig['resistance'])}",
        "",
        "[dim]Nedenler:[/dim]",
    ]
    for r in sig["reasons"]:
        color = "green" if any(w in r.lower() for w in ["yukari", "pozitif", "alis", "alim", ">", "dusuk"]) else \
                "red" if any(w in r.lower() for w in ["asagi", "negatif", "satis", "tepe", "<", "asiri alim", "risk"]) else "white"
        lines.append(f"  [{color}]- {r}[/{color}]")

    border = {"LONG": "green", "SHORT": "red", "NOTR": "yellow"}.get(d, "white")
    console.print()
    console.print(Panel.fit(
        "\n".join(lines),
        title=f"[bold] {display_name(symbol)} | {interval} | Teknik Sinyal [/bold]",
        border_style=border,
    ))


def render_watchlist(console: Console, results: list[tuple[str, dict[str, Any]]], interval: str) -> None:
    table = Table(title=f"Watchlist Sinyalleri ({interval})", show_lines=True)
    table.add_column("Varlik", style="bold", max_width=12)
    table.add_column("Fiyat", max_width=16)
    table.add_column("24h", max_width=10)
    table.add_column("RSI", max_width=8)
    table.add_column("MACD H.", max_width=12)
    table.add_column("Hacim", max_width=8)
    table.add_column("Sinyal", max_width=18)
    table.add_column("Guc", max_width=8)
    table.add_column("Skor", max_width=8)

    for sym, sig in results:
        d = sig["direction"]
        style = _DIR_STYLE.get(d, "white")
        ch = sig["change_24h"]
        ch_style = "green" if ch >= 0 else "red"
        table.add_row(
            display_name(sym),
            _fmt_price(sig["price"]),
            f"[{ch_style}]{'+' if ch >= 0 else ''}{ch:.1f}%[/{ch_style}]",
            str(sig["rsi"]),
            f"{sig['macd']['hist']:.4f}",
            f"{sig['volume_ratio']}x",
            f"[{style}]{sig['direction']}[/{style}]",
            sig["strength"],
            f"{sig['score']:+.1f}",
        )
    console.print(table)

    strong = [(s, sig) for s, sig in results if abs(sig["score"]) >= 4]
    if strong:
        for sym, sig in sorted(strong, key=lambda x: -abs(x[1]["score"])):
            render_signal(console, sym, sig, interval)


def _telegram_direct_from_script() -> bool:
    """False ise Telegram API script icinde cagrilmaz; webhook zinciri (or. n8n -> Telegram) kullanilir."""
    v = (
        os.environ.get("AGENT_REACH_TELEGRAM_VIA_WEBHOOK")
        or os.environ.get("AGENT_REACH_TELEGRAM_VIA_RELAY")
        or ""
    )
    return v.lower() not in ("1", "true", "yes")


def _outbound_webhook_url() -> str:
    return (
        (os.environ.get("AGENT_REACH_WEBHOOK_URL") or "").strip()
        or (os.environ.get("AGENT_REACH_RELAY_WEBHOOK_URL") or "").strip()
    )


def post_webhook_if_configured(payload: dict[str, Any]) -> None:
    """Harici otomasyon webhook’una JSON POST (n8n, Make, Zapier, vb.)."""
    url = _outbound_webhook_url()
    if not url:
        return
    body: dict[str, Any] = {
        "source": "agent-reach-crypto-signal",
        **payload,
    }
    dedup_raw = os.environ.get("AGENT_REACH_WEBHOOK_DEDUP") or os.environ.get("AGENT_REACH_RELAY_DEDUP", "1")
    dedup = (dedup_raw or "1").lower() not in ("0", "false", "no")
    if dedup and "deduplicationKey" not in body and "relayDeduplicationKey" not in body:
        sym = str(payload.get("symbol") or "")
        if payload.get("kind") == "classic":
            sig = payload.get("signal") or {}
            d = str(sig.get("direction") or "")
            sc = float(sig.get("score") or 0)
            key = f"{sym}-{d}-{sc:.2f}"
        else:
            pack = payload.get("pack") or {}
            sig = pack.get("signal") or {}
            conf = int(pack.get("confidence") or 0)
            d = str(sig.get("direction") or "")
            key = f"{sym}-{d}-{conf}"
        body["deduplicationKey"] = key
        body["relayDeduplicationKey"] = key
    try:
        requests.post(url, json=body, timeout=15)
    except OSError:
        pass


# Geriye donuk isim
post_relay_if_configured = post_webhook_if_configured


def notify_telegram_signal(symbol: str, sig: dict[str, Any], interval: str) -> None:
    if abs(sig["score"]) < 4:
        return
    d = sig["direction"]
    arrow = "+" if d == "LONG" else "-" if d == "SHORT" else "="
    lines = [
        f"{arrow} {display_name(symbol)} | {d} {sig['strength']}",
        f"Skor: {sig['score']:+.1f}  |  Fiyat: {_fmt_price(sig['price'])}",
        f"RSI: {sig['rsi']}  |  Hacim: {sig['volume_ratio']}x",
        f"24h: {sig['change_24h']:+.1f}%",
        "",
    ]
    for r in sig["reasons"][:8]:
        lines.append(f"  - {r}")
    text = "\n".join(lines)[:4000]
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if _telegram_direct_from_script() and token and chat:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat, "text": text}, timeout=15)
    post_webhook_if_configured(
        {
            "symbol": symbol,
            "interval": interval,
            "kind": "classic",
            "signal": sig,
            "telegram_text": text,
        },
    )


def render_full_pack(console: Console, pack: dict[str, Any], show_plan: bool = True) -> None:
    sym = pack["symbol"]
    iv = pack["interval_primary"]
    sig = pack["signal"]
    mtf = pack.get("mtf") or {}
    conf = int(pack.get("confidence") or 0)
    plan = pack.get("trade_plan") or {}
    flow = sig.get("flow") or {}

    render_signal(console, sym, sig, iv)
    extra = [
        "",
        f"[bold]Guven:[/bold] {conf}/100   |   [bold]MTF:[/bold] {mtf.get('label', '?')} — {mtf.get('detail', '')}",
        f"15m: {mtf.get('15m', {}).get('direction')} ({mtf.get('15m', {}).get('score')})"
        f"   |   1h: {mtf.get('60m', {}).get('direction')} ({mtf.get('60m', {}).get('score')})",
    ]
    if flow.get("available"):
        fr = float(flow.get("funding_rate", 0) or 0)
        dv = flow.get("hold_vol_delta_pct")
        extra.append(
            f"[dim]Flow {flow.get('contract')}:[/dim] funding {fr:.6f}  holdVol {flow.get('hold_vol', 0):.0f}"
            + (f"  dHV %{dv}" if dv is not None else "")
        )
    console.print(Panel.fit("\n".join(extra), title="MTF + Flow", border_style="cyan"))

    if show_plan and plan.get("available"):
        console.print(Panel.fit(
            f"Entry: {_fmt_price(plan.get('entry'))}\n"
            f"SL: {_fmt_price(plan.get('sl'))}\n"
            f"TP1: {_fmt_price(plan.get('tp1'))}   TP2: {_fmt_price(plan.get('tp2'))}\n"
            f"R:R (TP1): {plan.get('rr_tp1')}   |   Tahmini boyut (risk {plan.get('risk_usd')} USD): {plan.get('suggested_units')} adet\n"
            f"ATR: {plan.get('atr')}",
            title="Islem plani (ATR)",
            border_style="blue",
        ))


def render_watchlist_mtf(
    console: Console,
    rows: list[tuple[str, dict[str, Any]]],
    interval: str,
) -> None:
    table = Table(title=f"Watchlist MTF ({interval})", show_lines=True)
    table.add_column("Varlik", style="bold", max_width=11)
    table.add_column("Fiyat", max_width=14)
    table.add_column("Guven", max_width=6)
    table.add_column("MTF", max_width=10)
    table.add_column("Sinyal", max_width=8)
    table.add_column("Skor", max_width=7)
    table.add_column("Sektor", max_width=8)

    for sym, pack in rows:
        sig = pack.get("signal") or {}
        mtf = pack.get("mtf") or {}
        conf = int(pack.get("confidence") or 0)
        d = sig.get("direction", "NOTR")
        style = _DIR_STYLE.get(d, "white")
        sec = sector_for_symbol(sym)
        table.add_row(
            display_name(sym),
            _fmt_price(sig.get("price")),
            str(conf),
            str(mtf.get("label", "?")),
            f"[{style}]{d}[/{style}]",
            f"{sig.get('score'):+.1f}",
            sec,
        )
    console.print(table)


def notify_telegram_smart_pack(symbol: str, pack: dict[str, Any], interval: str) -> None:
    sig = pack.get("signal") or {}
    mtf = pack.get("mtf") or {}
    conf = int(pack.get("confidence") or 0)
    lines = [
        f"*{display_name(symbol)}* MTF | {interval} | guven {conf}/100",
        f"Yon: {sig.get('direction')}  skor: {sig.get('score'):+.1f}",
        f"MTF: {mtf.get('label')} — 15m {mtf.get('15m', {}).get('direction')}  1h {mtf.get('60m', {}).get('direction')}",
    ]
    plan = pack.get("trade_plan") or {}
    if plan.get("available"):
        lines.append(
            f"Plan: SL {_fmt_price(plan.get('sl'))} TP1 {_fmt_price(plan.get('tp1'))} R:R {plan.get('rr_tp1')}"
        )
    text = "\n".join(lines)[:4000]
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if _telegram_direct_from_script() and token and chat:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat, "text": text}, timeout=15)
    post_webhook_if_configured(
        {
            "symbol": symbol,
            "interval": interval,
            "kind": "mtf_pack",
            "pack": pack,
            "telegram_text": text,
        },
    )


def run_telegram_command_loop(console: Console) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        console.print("[red]TELEGRAM_BOT_TOKEN gerekli[/red]")
        return
    offset = 0
    console.print("[green]Telegram komut dinleyici[/green] — /signal BTCUSDT, /watch hybrid, /stop")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"timeout": 25, "offset": offset},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = msg.get("chat", {}).get("id")
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                parts = text.split()
                cmd = parts[0].split("@")[0].lower()
                reply = ""
                if cmd == "/signal" and len(parts) >= 2:
                    coin = parts[1].upper()
                    try:
                        pack = analyze_mtf(coin, "15m")
                        reply = f"{coin} guven {pack['confidence']} MTF {pack['mtf']['label']} {pack['signal']['direction']}"
                    except Exception as e:
                        reply = f"Hata: {e}"
                elif cmd == "/watch" and len(parts) >= 2:
                    preset = parts[1].lower()
                    if preset in _WATCHLIST_PRESETS:
                        coins = _WATCHLIST_PRESETS[preset][:5]
                        bits = []
                        for c in coins:
                            try:
                                p = analyze_mtf(c, "15m")
                                bits.append(f"{c}:{p['signal']['direction']}({p['confidence']})")
                            except Exception:
                                bits.append(f"{c}:?")
                        reply = " | ".join(bits)
                    else:
                        reply = "Preset yok: macro hybrid scalping"
                elif cmd in ("/stop", "/quit"):
                    return
                else:
                    reply = "Komutlar: /signal BTCUSDT | /watch hybrid | /stop"
                if chat and reply:
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat, "text": reply[:4000]},
                        timeout=15,
                    )
        except KeyboardInterrupt:
            return
        except Exception as e:
            console.print(f"[yellow]poll: {e}[/yellow]")
            time.sleep(2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def analyze(symbol: str, interval: str) -> dict[str, Any]:
    candles = fetch_klines(symbol, interval=interval, limit=100)
    ticker = fetch_ticker(symbol)
    return generate_signal(candles, ticker)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Teknik sinyal — MEXC Spot + MEXC Futures (+ opsiyonel Yahoo)",
        epilog=(
            "MTF: --mtf (5m/15m/1h teyit + funding/holdVol + guven + plan) | "
            "Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, AGENT_REACH_ALERT_MIN_CONF | "
            "Veri: AGENT_REACH_DATA_DIR | "
            "Haber boost: agent_reach_news_boost.json"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    sig_p = sub.add_parser("signal", help="Tek varlik sinyal analizi")
    sig_p.add_argument(
        "--coin",
        required=True,
        help="Sembol: ENJUSDT (spot), GOLD/OIL/NASDAQ (futures), ya da GC=F/^IXIC (Yahoo)",
    )
    sig_p.add_argument("--interval", default="15m", help="Mum araligi (1m,5m,15m,30m,60m,4h,1d)")
    sig_p.add_argument(
        "--mtf",
        action="store_true",
        help="15m + 1h teyit, futures akisi, guven skoru, islem plani",
    )
    sig_p.add_argument("--min-confidence", type=int, default=0, help="Raporda gosterim esigi (0-100)")
    sig_p.add_argument("--no-plan", action="store_true", help="Islem plani panelini gizle")

    wl_p = sub.add_parser("watchlist", help="Birden cok varlik izle")
    wl_p.add_argument(
        "--coins",
        help="Virgul ile: ENJUSDT,BTCUSDT,GOLD,OIL,NASDAQ,XAUT_USDT,USOIL_USDT ...",
    )
    wl_p.add_argument(
        "--preset",
        choices=sorted(_WATCHLIST_PRESETS.keys()),
        help="Hazir sepet (ornek: macro)",
    )
    wl_p.add_argument("--interval", default="15m")
    wl_p.add_argument("--watch", action="store_true", help="Surekli izle")
    wl_p.add_argument("--every", type=int, default=120, help="Tekrar suresi (sn)")
    wl_p.add_argument(
        "--add-top-alts",
        type=int,
        default=0,
        help=(
            "MEXC spot 24h hacme gore en yuksek altcoinleri otomatik ekler. "
            "Scalping preset'inde varsayilan 5."
        ),
    )
    wl_p.add_argument("--mtf", action="store_true", help="MTF + flow + guven + sektor sutunu")
    wl_p.add_argument("--min-confidence", type=int, default=0, help="Listede gosterim esigi")
    wl_p.add_argument(
        "--smart-telegram",
        action="store_true",
        help="Sadece durum/guven degisiminde Telegram (spam azaltir)",
    )
    wl_p.add_argument(
        "--telegram-all",
        action="store_true",
        help="MTF watchlist'te her taramada Telegram (dikkat: cok bildirim)",
    )

    bt_p = sub.add_parser("backtest", help="Kaba gecmis performans simulasyonu")
    bt_p.add_argument("--coin", required=True)
    bt_p.add_argument("--interval", default="15m")
    bt_p.add_argument("--bars", type=int, default=400)

    st_p = sub.add_parser("stats", help="Paper trade istatistikleri")
    sec_p = sub.add_parser("sectors", help="Watchlist'i sektore gore ozetle")
    sec_p.add_argument("--coins", help="Virgul ile sembol listesi")
    sec_p.add_argument("--preset", choices=sorted(_WATCHLIST_PRESETS.keys()))
    sec_p.add_argument("--interval", default="15m")

    paper_p = sub.add_parser("paper", help="Paper trade sonuc kaydi")
    paper_p.add_argument("--symbol", required=True)
    paper_p.add_argument("--outcome", default="close", help="close, win, loss")
    paper_p.add_argument("--pnl", type=float, default=None, help="Kar/zarar %")
    paper_p.add_argument("--note", default="")

    tg_p = sub.add_parser("telegram-bot", help="Telegram /signal ve /watch komutlari (polling)")

    args = parser.parse_args()
    console = Console()

    if args.command == "signal":
        sym = args.coin.upper()
        try:
            if args.mtf:
                pack = analyze_mtf(sym, args.interval)
                if int(pack.get("confidence") or 0) < args.min_confidence:
                    console.print(
                        f"[yellow]Guven {pack['confidence']} < min {args.min_confidence} — yine de gosteriliyor.[/yellow]"
                    )
                render_full_pack(console, pack, show_plan=not args.no_plan)
                append_signal_log({"ts": time.time(), "symbol": sym, "pack": pack})
                if should_send_smart_alert(sym, pack):
                    notify_telegram_smart_pack(sym, pack, args.interval)
            else:
                sig = analyze(sym, args.interval)
                render_signal(console, sym, sig, args.interval)
                notify_telegram_signal(sym, sig, args.interval)
        except Exception as e:
            console.print(f"[red]Hata: {e}[/red]")

    elif args.command == "watchlist":
        try:
            coins = _resolve_watchlist_symbols(args.coins, args.preset)
        except ValueError as e:
            parser.error(str(e))
            return

        top_alts_to_add = args.add_top_alts
        if (args.preset or "").lower() == "scalping" and top_alts_to_add == 0:
            top_alts_to_add = _DEFAULT_SCALPING_TOP_ALTS

        if top_alts_to_add > 0:
            try:
                top_alts = fetch_top_volume_altcoins(top_alts_to_add, exclude=set(coins))
                coins = _dedupe_keep_order(coins + top_alts)
                if top_alts:
                    console.print(
                        "[dim]Otomatik eklendi (top alt hacim): "
                        + ",".join(top_alts)
                        + "[/dim]"
                    )
            except Exception as e:
                console.print(f"[yellow]Top altcoin hacim listesi alinamadi: {e}[/yellow]")

        use_smart = args.smart_telegram or (
            os.environ.get("AGENT_REACH_SMART_TELEGRAM", "").lower() in ("1", "true", "yes")
        )

        def run_wl() -> None:
            if args.mtf:
                mtf_rows: list[tuple[str, dict[str, Any]]] = []
                for c in coins:
                    try:
                        pack = analyze_mtf(c, args.interval)
                        if int(pack.get("confidence") or 0) < args.min_confidence:
                            continue
                        mtf_rows.append((c, pack))
                        append_signal_log({"ts": time.time(), "symbol": c, "pack": pack})
                        min_c = int(os.environ.get("AGENT_REACH_ALERT_MIN_CONF", "55") or 55)
                        if use_smart:
                            if should_send_smart_alert(c, pack):
                                notify_telegram_smart_pack(c, pack, args.interval)
                        elif args.telegram_all and int(pack.get("confidence") or 0) >= min_c:
                            notify_telegram_smart_pack(c, pack, args.interval)
                    except Exception as e:
                        console.print(f"[red]{display_name(c)} hatasi: {e}[/red]")
                if mtf_rows:
                    render_watchlist_mtf(console, mtf_rows, args.interval)
            else:
                results: list[tuple[str, dict[str, Any]]] = []
                for c in coins:
                    try:
                        sig = analyze(c, args.interval)
                        results.append((c, sig))
                        notify_telegram_signal(c, sig, args.interval)
                    except Exception as e:
                        console.print(f"[red]{display_name(c)} hatasi: {e}[/red]")
                if results:
                    render_watchlist(console, results, args.interval)

        if args.watch:
            mode = "MTF+smart" if args.mtf and use_smart else "normal"
            console.print(
                f"[green]Izleme[/green] -- {len(coins)} varlik, {args.interval}, her {args.every}s ({mode})"
            )
            while True:
                run_wl()
                time.sleep(max(30, args.every))
        else:
            run_wl()

    elif args.command == "backtest":
        try:
            out = run_backtest_simple(args.coin.upper(), args.interval, bars=args.bars)
            console.print_json(data=out)
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    elif args.command == "stats":
        s = compute_stats_from_paper()
        console.print_json(data=s)

    elif args.command == "sectors":
        try:
            sc = _resolve_watchlist_symbols(args.coins, args.preset)
        except ValueError as e:
            parser.error(str(e))
            return
        buckets: dict[str, list[tuple[str, int]]] = {}
        for c in sc:
            try:
                pack = analyze_mtf(c, args.interval)
                sec = sector_for_symbol(c)
                conf = int(pack.get("confidence") or 0)
                buckets.setdefault(sec, []).append((c, conf))
            except Exception as e:
                console.print(f"[dim]{c}: {e}[/dim]")
        for sec in sorted(buckets.keys()):
            items = sorted(buckets[sec], key=lambda x: -x[1])
            console.print(f"[bold]{sec}[/bold]: " + ", ".join(f"{display_name(a)}({b})" for a, b in items[:12]))

    elif args.command == "paper":
        paper_record(args.symbol.upper(), args.outcome, args.pnl, args.note)
        console.print("[green]Paper kayit eklendi.[/green] stats ile bakin.")

    elif args.command == "telegram-bot":
        run_telegram_command_loop(console)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
