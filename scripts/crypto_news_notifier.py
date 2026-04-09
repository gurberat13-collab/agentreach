# -*- coding: utf-8 -*-
"""
Kripto haber + piyasa bildirimi — Agent Reach deposundan bağımsız yardımcı script.

Özellikler:
  1. RSS haber tarama + etki skoru
  2. Anlık fiyat çekme (CoinGecko, ücretsiz)
  3. Whale Alert (büyük transferler, RSS)
  4. Basit duygu analizi (sentiment — kelime tabanlı)
  5. Haber özeti (Jina Reader ile içerik + extractive özet)
  6. İşlem günlüğü (trade log — işleme giriş/çıkış kayıtları)
  7. Telegram bildirimi (sadece yüksek önem)

Kullanım:
  python scripts/crypto_news_notifier.py --once
  python scripts/crypto_news_notifier.py --watch --interval 300
  python scripts/crypto_news_notifier.py --once --alert-min 7 --quiet-low
  python scripts/crypto_news_notifier.py trade --action long --coin BTC --price 97500 --note "SEC ETF onayı"
  python scripts/crypto_news_notifier.py trade --action close --coin BTC --price 101200
  python scripts/crypto_news_notifier.py trades              # günlüğü göster

Ortam:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  CRYPTO_TELEGRAM_HIGH_ONLY=1
  CRYPTO_ALERT_MIN_SCORE=6
  CRYPTO_STATE_FILE
  CRYPTO_TRADE_LOG           — işlem günlüğü JSON (varsayılan: state yanına)

Önemli: Hiçbir çıktı yatırım tavsiyesi değildir. Karar tamamen size aittir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home()))
_DEFAULT_STATE = _APP_DIR / "agent_reach_crypto_notifier_state.json"
_DEFAULT_TRADE_LOG = _APP_DIR / "agent_reach_crypto_trades.json"

# ---------------------------------------------------------------------------
# RSS kaynakları
# ---------------------------------------------------------------------------
DEFAULT_FEEDS: list[str] = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
]

WHALE_FEEDS: list[str] = [
    "https://whale-alert.io/feed",
]

# ---------------------------------------------------------------------------
# Etki kuralları
# ---------------------------------------------------------------------------
_IMPACT_RULES: list[tuple[str, int, str]] = [
    (r"\bsec\b", 4, "SEC"),
    (r"\betf\b", 3, "ETF"),
    (r"approval|approved|reject|denied", 3, "onay/red"),
    (r"hack|hacked|exploit|drained|stolen|breach", 5, "güvenlik olayı"),
    (r"bankruptcy|bankrupt|insolv", 4, "iflas"),
    (r"liquidat", 3, "likidite"),
    (r"lawsuit|indict|charged|arrest|sued", 3, "hukuk"),
    (r"ban\b|banned|sanction", 4, "yasak/yaptırım"),
    (r"delist|listing\b|listings", 3, "borsa listesi"),
    (r"\bfed\b|fomc|interest rate|rate cut|rate hike", 4, "Fed/faiz"),
    (r"\bcpi\b|inflation", 3, "enflasyon verisi"),
    (r"outage|halt|suspend|paused", 3, "kesinti/durdurma"),
    (r"merger|acquisition|acquire", 2, "birleşme/satın alma"),
    (r"regulat|crackdown|enforcement", 3, "düzenleme"),
    (r"whale|billion|million.+(flow|transfer|move)", 2, "büyük hareket"),
    (r"binance|coinbase|kraken", 2, "büyük borsa"),
    (r"halving", 3, "halving"),
    (r"mainnet|upgrade|fork", 2, "teknik güncelleme"),
]

# ---------------------------------------------------------------------------
# Coin sembolleri → CoinGecko ID eşleme
# ---------------------------------------------------------------------------
_KNOWN_SYMBOLS: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "DOGE": "dogecoin", "ADA": "cardano", "AVAX": "avalanche-2", "DOT": "polkadot",
    "LINK": "chainlink", "MATIC": "matic-network", "POL": "matic-network",
    "ATOM": "cosmos", "UNI": "uniswap", "LTC": "litecoin", "BCH": "bitcoin-cash",
    "NEAR": "near", "APT": "aptos", "OP": "optimism", "ARB": "arbitrum",
    "INJ": "injective-protocol", "TON": "the-open-network", "SUI": "sui",
    "SEI": "sei-network", "PEPE": "pepe", "SHIB": "shiba-inu", "WLD": "worldcoin-wld",
    "FIL": "filecoin", "ICP": "internet-computer", "AAVE": "aave", "MKR": "maker",
    "SNX": "havven", "CRV": "curve-dao-token", "GRT": "the-graph", "ENS": "ethereum-name-service",
    "IMX": "immutable-x", "STX": "blockstack", "TIA": "celestia",
    "BNB": "binancecoin", "TRX": "tron", "ETC": "ethereum-classic",
}

# ---------------------------------------------------------------------------
# Sentiment sözlükleri
# ---------------------------------------------------------------------------
_POS_WORDS = frozenset(
    "surge rally soar jump gain gains bullish uptrend breakout approved approval "
    "partnership launch listing listed adopt adoption record high growth rebound recover "
    "inflow pump moon mooning milestone upgrade".split()
)
_NEG_WORDS = frozenset(
    "crash drop plunge tumble dump decline bearish downtrend hack hacked exploit breach "
    "stolen drained ban banned reject denied lawsuit arrest sued bankrupt insolvency "
    "liquidat suspend halt outage crackdown sanction sell-off selloff fear warning "
    "scam rug fraud indictment".split()
)


# ═══════════════════════════════════════════════════════════════════════════
# 1) Anlık fiyat — CoinGecko (ücretsiz, API key yok)
# ═══════════════════════════════════════════════════════════════════════════

_price_cache: dict[str, tuple[float, float, float]] = {}  # gecko_id -> (usd, change24h, ts)
_PRICE_TTL = 90  # saniye


def fetch_prices(symbols: list[str]) -> dict[str, dict[str, float]]:
    """Sembol listesi → {SYM: {usd, change_24h}} döner. Cache'li."""
    ids_needed: dict[str, str] = {}  # geckoId -> symbol
    result: dict[str, dict[str, float]] = {}
    now = time.time()

    for sym in symbols:
        gid = _KNOWN_SYMBOLS.get(sym.upper())
        if not gid:
            continue
        cached = _price_cache.get(gid)
        if cached and (now - cached[2]) < _PRICE_TTL:
            result[sym] = {"usd": cached[0], "change_24h": cached[1]}
        else:
            ids_needed[gid] = sym

    if ids_needed:
        try:
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {
                "ids": ",".join(ids_needed.keys()),
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            }
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            for gid, sym in ids_needed.items():
                info = data.get(gid, {})
                usd = info.get("usd", 0.0)
                ch = info.get("usd_24h_change", 0.0)
                _price_cache[gid] = (usd, ch, time.time())
                result[sym] = {"usd": usd, "change_24h": round(ch, 2)}
        except Exception as e:
            logger.warning("CoinGecko fiyat hatası: {}", e)

    return result


def format_price_line(prices: dict[str, dict[str, float]]) -> str:
    if not prices:
        return ""
    parts: list[str] = []
    for sym in sorted(prices):
        p = prices[sym]
        ch = p["change_24h"]
        sign = "+" if ch >= 0 else ""
        parts.append(f"{sym}: ${p['usd']:,.2f} ({sign}{ch}%)")
    return "  ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# 2) Whale Alert (RSS)
# ═══════════════════════════════════════════════════════════════════════════

def fetch_whale_entries(seen: set[str]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for url in WHALE_FEEDS:
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries:
                eid = hashlib.sha256(
                    (getattr(entry, "id", "") or getattr(entry, "link", "")).encode()
                ).hexdigest()
                if eid in seen:
                    continue
                items.append({
                    "id": eid,
                    "title": getattr(entry, "title", "") or "",
                    "link": getattr(entry, "link", "") or "",
                    "source": url,
                    "published": getattr(entry, "published", "") or "",
                    "is_whale": "1",
                })
        except Exception as e:
            logger.warning("Whale feed hatası: {}", e)
    return items


# ═══════════════════════════════════════════════════════════════════════════
# 3) Sentiment analizi (kelime tabanlı)
# ═══════════════════════════════════════════════════════════════════════════

def analyze_sentiment(title: str) -> tuple[str, float]:
    """(etiket, skor) döner. skor: -1..+1. etiket: pozitif/negatif/nötr."""
    words = set(re.findall(r"[a-z]+", title.lower()))
    pos = len(words & _POS_WORDS)
    neg = len(words & _NEG_WORDS)
    total = pos + neg
    if total == 0:
        return "nötr", 0.0
    score = (pos - neg) / total
    if score > 0.15:
        return "pozitif", round(score, 2)
    if score < -0.15:
        return "negatif", round(score, 2)
    return "nötr", round(score, 2)


# ═══════════════════════════════════════════════════════════════════════════
# 4) Haber özeti (Jina Reader → extractive)
# ═══════════════════════════════════════════════════════════════════════════

def fetch_article_summary(link: str, max_sentences: int = 3) -> str:
    """Jina Reader ile makaleyi çek, ilk N cümleyi döndür."""
    if not link:
        return ""
    try:
        jina_url = f"https://r.jina.ai/{link}"
        headers = {"Accept": "text/plain", "X-Return-Format": "text"}
        r = requests.get(jina_url, headers=headers, timeout=20)
        if r.status_code != 200:
            return "(özet alınamadı)"
        text = r.text.strip()
        # Basit cümle bölme
        sentences = re.split(r'(?<=[.!?])\s+', text)
        useful = [s.strip() for s in sentences if len(s.strip()) > 40][:max_sentences]
        return " ".join(useful)[:600] if useful else text[:600]
    except Exception as e:
        logger.warning("Özet çekme hatası {}: {}", link[:60], e)
        return "(özet alınamadı)"


# ═══════════════════════════════════════════════════════════════════════════
# 5) İşlem günlüğü (trade log)
# ═══════════════════════════════════════════════════════════════════════════

def _trade_log_path() -> Path:
    return Path(os.environ.get("CRYPTO_TRADE_LOG", str(_DEFAULT_TRADE_LOG)))


def load_trades() -> list[dict[str, Any]]:
    p = _trade_log_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_trades(trades: list[dict[str, Any]]) -> None:
    p = _trade_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8")


def add_trade(action: str, coin: str, price: float, note: str = "") -> dict[str, Any]:
    trades = load_trades()
    entry = {
        "id": len(trades) + 1,
        "time": datetime.now(timezone.utc).isoformat(),
        "action": action,        # long / short / close
        "coin": coin.upper(),
        "price": price,
        "note": note,
    }
    trades.append(entry)
    save_trades(trades)
    return entry


def show_trades(console: Console) -> None:
    trades = load_trades()
    if not trades:
        console.print("[dim]Henüz kayıtlı işlem yok.[/dim]")
        return
    table = Table(title="İşlem Günlüğü", show_lines=True)
    table.add_column("#", style="dim", max_width=4)
    table.add_column("Zaman", max_width=20)
    table.add_column("Aksiyon", max_width=8)
    table.add_column("Coin", style="cyan", max_width=8)
    table.add_column("Fiyat ($)", style="bold", max_width=14)
    table.add_column("Not", max_width=40)
    for t in trades:
        color = {"long": "green", "short": "red", "close": "yellow"}.get(t["action"], "white")
        table.add_row(
            str(t["id"]),
            t["time"][:19].replace("T", " "),
            f"[{color}]{t['action']}[/{color}]",
            t["coin"],
            f"{t['price']:,.2f}",
            t.get("note", ""),
        )
    console.print(table)

    # Basit P&L hesaplama (aynı coin long→close çiftleri)
    opens: dict[str, list[dict]] = {}
    pnl_lines: list[str] = []
    for t in trades:
        if t["action"] in ("long", "short"):
            opens.setdefault(t["coin"], []).append(t)
        elif t["action"] == "close" and opens.get(t["coin"]):
            o = opens[t["coin"]].pop(0)
            diff = t["price"] - o["price"]
            if o["action"] == "short":
                diff = -diff
            pct = (diff / o["price"]) * 100 if o["price"] else 0
            emoji = "[green]+[/green]" if diff >= 0 else "[red]−[/red]"
            pnl_lines.append(
                f"  {o['coin']} {o['action']}@{o['price']:,.2f} -> close@{t['price']:,.2f}  "
                f"{emoji}${abs(diff):,.2f} ({'+' if pct >= 0 else ''}{pct:.1f}%)"
            )
    if pnl_lines:
        console.print(Panel("\n".join(pnl_lines), title="Kapanmış İşlemler (P&L)", border_style="blue"))


# ═══════════════════════════════════════════════════════════════════════════
# Core: haber zenginleştirme
# ═══════════════════════════════════════════════════════════════════════════

def _entry_id(entry: Any) -> str:
    link = getattr(entry, "link", "") or ""
    title = getattr(entry, "title", "") or ""
    raw = getattr(entry, "id", "") or link + "\0" + title
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("seen_ids", []))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("State okunamadı, sıfırdan başlanıyor: {}", e)
        return set()


def save_state(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seen_ids": sorted(seen)}, indent=2), encoding="utf-8")


def extract_tickers(text: str) -> list[str]:
    t = text.upper()
    found: set[str] = set()
    for m in re.finditer(r"\$([A-Z]{2,12})\b", t):
        found.add(m.group(1))
    for sym in _KNOWN_SYMBOLS:
        if re.search(rf"\b{re.escape(sym)}\b", t):
            found.add(sym)
    return sorted(found)


def score_impact(title: str) -> tuple[int, list[str]]:
    low = title.lower()
    score = 0
    reasons: list[str] = []
    for pattern, pts, label in _IMPACT_RULES:
        if re.search(pattern, low, re.I):
            score += pts
            if label not in reasons:
                reasons.append(label)
    return min(score, 25), reasons


def enrich_item(raw: dict[str, str], fetch_summary: bool = False) -> dict[str, Any]:
    title = raw["title"]
    tickers = extract_tickers(title)
    imp, reasons = score_impact(title)
    if tickers:
        imp += min(2, len(tickers))

    sentiment_label, sentiment_score = analyze_sentiment(title)
    prices = fetch_prices(tickers) if tickers else {}

    out: dict[str, Any] = dict(raw)
    out["impact_score"] = imp
    out["impact_reasons"] = reasons
    out["tickers"] = tickers
    out["sentiment"] = sentiment_label
    out["sentiment_score"] = sentiment_score
    out["prices"] = prices
    out["price_line"] = format_price_line(prices)
    out["summary"] = ""

    if fetch_summary and imp >= 6:
        out["summary"] = fetch_article_summary(raw.get("link", ""))

    return out


def fetch_new_entries(feed_url: str, seen: set[str]) -> list[dict[str, str]]:
    parsed = feedparser.parse(feed_url)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        logger.warning("Feed sorunlu: {} — {}", feed_url, getattr(parsed, "bozo_exception", ""))
    items: list[dict[str, str]] = []
    for entry in parsed.entries:
        eid = _entry_id(entry)
        if eid in seen:
            continue
        items.append({
            "id": eid,
            "title": getattr(entry, "title", "(başlıksız)") or "",
            "link": getattr(entry, "link", "") or "",
            "source": feed_url,
            "published": getattr(entry, "published", "") or getattr(entry, "updated", "") or "",
        })
    return items


# ═══════════════════════════════════════════════════════════════════════════
# Bildirim
# ═══════════════════════════════════════════════════════════════════════════

_SENT_COLORS = {"pozitif": "green", "negatif": "red", "nötr": "dim"}


def notify_console(
    console: Console,
    items: list[dict[str, Any]],
    alert_min: int,
    quiet_low: bool,
) -> None:
    if not items:
        console.print("[dim]Yeni haber yok.[/dim]")
        return

    high = [it for it in items if it["impact_score"] >= alert_min]
    low = [it for it in items if it["impact_score"] < alert_min]

    for it in sorted(high, key=lambda x: -x["impact_score"]):
        host = it["source"].split("/")[2] if "//" in it["source"] else it["source"]
        tick = ", ".join(it["tickers"]) if it["tickers"] else "—"
        reasons = ", ".join(it["impact_reasons"][:6]) if it["impact_reasons"] else "—"
        scol = _SENT_COLORS.get(it["sentiment"], "dim")

        body_parts = [
            f"[bold]{it['title'][:280]}[/bold]\n",
            f"Skor: [red]{it['impact_score']}[/red]  |  "
            f"Duygu: [{scol}]{it['sentiment']} ({it['sentiment_score']:+.2f})[/{scol}]",
            f"Coin: [cyan]{tick}[/cyan]",
        ]
        if it.get("price_line"):
            body_parts.append(f"Fiyat: [bold]{it['price_line']}[/bold]")
        body_parts.append(f"Etiketler: [dim]{reasons}[/dim]")
        body_parts.append(f"Kaynak: {host}  |  {it['published'][:40]}")
        if it.get("summary"):
            body_parts.append(f"\n[italic]Özet: {it['summary'][:500]}[/italic]")
        body_parts.append(f"[link={it['link']}]Habere git[/link]")

        console.print()
        console.print(Panel.fit(
            "\n".join(body_parts),
            title="[bold red on black] İŞLEM İÇİN ÖNEMLİ [/bold red on black]",
            border_style="red",
        ))

    if not quiet_low and low:
        table = Table(title="Diğer haberler (düşük skor)", show_lines=False)
        table.add_column("Skor", style="dim", max_width=5)
        table.add_column("Duygu", max_width=8)
        table.add_column("Başlık", max_width=46)
        table.add_column("Site", style="cyan", max_width=14)
        for it in sorted(low, key=lambda x: x["published"], reverse=True):
            h = it["source"].split("/")[2] if "//" in it["source"] else it["source"]
            scol = _SENT_COLORS.get(it["sentiment"], "dim")
            table.add_row(
                str(it["impact_score"]),
                f"[{scol}]{it['sentiment']}[/{scol}]",
                it["title"][:140],
                h,
            )
        console.print(table)
    elif quiet_low and not high:
        console.print("[dim]Eşik üstü haber yok.[/dim]")


def notify_telegram(items: list[dict[str, Any]], alert_min: int, high_only: bool) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    to_send = [it for it in items if it["impact_score"] >= alert_min] if high_only else items
    if not to_send:
        return
    lines = ["🚨 Kripto — önemli haberler:"]
    for it in sorted(to_send, key=lambda x: -x["impact_score"])[:10]:
        tick = ", ".join(it["tickers"]) if it["tickers"] else "?"
        sent = it.get("sentiment", "?")
        price = it.get("price_line", "")
        parts = [f"• [{it['impact_score']}] {it['title'][:160]}"]
        parts.append(f"  {tick}  |  {sent}")
        if price:
            parts.append(f"  {price}")
        if it.get("summary"):
            parts.append(f"  📝 {it['summary'][:200]}")
        parts.append(f"  {it['link']}")
        lines.append("\n".join(parts))
    text = "\n\n".join(lines)[:4000]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=30)
    if r.status_code != 200:
        logger.error("Telegram hatası: {} {}", r.status_code, r.text[:500])


# ═══════════════════════════════════════════════════════════════════════════
# Ana döngü
# ═══════════════════════════════════════════════════════════════════════════

def run_cycle(
    feeds: list[str],
    state_path: Path,
    console: Console,
    alert_min: int,
    quiet_low: bool,
    telegram_high_only: bool,
    include_whales: bool = True,
    fetch_summaries: bool = True,
) -> int:
    seen = load_state(state_path)
    all_new: list[dict[str, str]] = []

    for url in feeds:
        try:
            all_new.extend(fetch_new_entries(url, seen))
        except Exception as e:
            logger.exception("Feed hatası {}: {}", url, e)

    if include_whales:
        whale_items = fetch_whale_entries(seen)
        if whale_items:
            console.print(f"[yellow]🐋 {len(whale_items)} yeni whale hareketi[/yellow]")
        all_new.extend(whale_items)

    # İlk çalıştırma
    if not seen and all_new:
        for it in all_new:
            seen.add(it["id"])
        save_state(state_path, seen)
        console.print(
            f"[yellow]İlk çalıştırma:[/yellow] {len(all_new)} mevcut haber kaydedildi. "
            f"Eşik: [cyan]{alert_min}[/cyan]"
        )
        return 0

    if not all_new:
        notify_console(console, [], alert_min, quiet_low)
        return 0

    enriched = [enrich_item(it, fetch_summary=fetch_summaries) for it in all_new]
    enriched.sort(key=lambda x: -x["impact_score"])

    notify_console(console, enriched, alert_min, quiet_low)
    notify_telegram(enriched, alert_min, telegram_high_only)

    for it in all_new:
        seen.add(it["id"])
    save_state(state_path, seen)
    return len(all_new)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    default_alert = int(os.environ.get("CRYPTO_ALERT_MIN_SCORE", "6"))
    default_tg_high = os.environ.get("CRYPTO_TELEGRAM_HIGH_ONLY", "1").strip() not in ("0", "false", "no")

    parser = argparse.ArgumentParser(description="Kripto RSS + fiyat + whale + sentiment + trade log")
    sub = parser.add_subparsers(dest="command")

    # --- trade komutu ---
    trade_p = sub.add_parser("trade", help="İşlem günlüğüne kayıt ekle")
    trade_p.add_argument("--action", required=True, choices=["long", "short", "close"], help="Pozisyon yönü")
    trade_p.add_argument("--coin", required=True, help="Coin sembolü (BTC, ETH…)")
    trade_p.add_argument("--price", required=True, type=float, help="Giriş/çıkış fiyatı ($)")
    trade_p.add_argument("--note", default="", help="İsteğe bağlı not")

    # --- trades komutu ---
    sub.add_parser("trades", help="İşlem günlüğünü göster")

    # --- scan (varsayılan) ---
    scan_p = sub.add_parser("scan", help="Haber taraması (varsayılan)")
    scan_p.add_argument("--once", action="store_true")
    scan_p.add_argument("--watch", action="store_true")
    scan_p.add_argument("--interval", type=int, default=300)
    scan_p.add_argument("--state-file", type=Path, default=Path(os.environ.get("CRYPTO_STATE_FILE", str(_DEFAULT_STATE))))
    scan_p.add_argument("--feeds-file", type=Path, default=None)
    scan_p.add_argument("--alert-min", type=int, default=default_alert)
    scan_p.add_argument("--quiet-low", action="store_true")
    scan_p.add_argument("--telegram-all", action="store_true")
    scan_p.add_argument("--no-whales", action="store_true", help="Whale uyarılarını atla")
    scan_p.add_argument("--no-summary", action="store_true", help="Haber özetini çekme")

    # argparse: alt komut verilmezse eski davranış (scan gibi)
    # Ama eski --once --watch argümanlarını da üst seviyede kabul et
    parser.add_argument("--once", action="store_true", default=False)
    parser.add_argument("--watch", action="store_true", default=False)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--state-file", type=Path, default=Path(os.environ.get("CRYPTO_STATE_FILE", str(_DEFAULT_STATE))))
    parser.add_argument("--feeds-file", type=Path, default=None)
    parser.add_argument("--alert-min", type=int, default=default_alert)
    parser.add_argument("--quiet-low", action="store_true", default=False)
    parser.add_argument("--telegram-all", action="store_true", default=False)
    parser.add_argument("--no-whales", action="store_true", default=False)
    parser.add_argument("--no-summary", action="store_true", default=False)

    args = parser.parse_args()
    console = Console()
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{level}</level> {message}")

    # -- trade komutu --
    if args.command == "trade":
        entry = add_trade(args.action, args.coin, args.price, args.note)
        console.print(
            f"[green]İşlem kaydedildi:[/green] #{entry['id']} "
            f"{entry['action']} {entry['coin']} @ ${entry['price']:,.2f}"
        )
        return

    # -- trades komutu --
    if args.command == "trades":
        show_trades(console)
        return

    # -- scan (varsayılan) --
    if args.feeds_file and args.feeds_file.exists():
        feeds = [
            ln.strip()
            for ln in args.feeds_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
    else:
        feeds = list(DEFAULT_FEEDS)

    telegram_high_only = default_tg_high and not args.telegram_all

    def one_round() -> int:
        return run_cycle(
            feeds, args.state_file, console,
            alert_min=args.alert_min,
            quiet_low=args.quiet_low,
            telegram_high_only=telegram_high_only,
            include_whales=not args.no_whales,
            fetch_summaries=not args.no_summary,
        )

    if args.watch and not args.once:
        console.print(
            f"[green]İzleme başladı[/green] — eşik {args.alert_min}, "
            f"whale: {not args.no_whales}, özet: {not args.no_summary}, "
            f"aralık: {args.interval}s"
        )
        while True:
            n = one_round()
            logger.info("Tur: {} yeni öğe", n)
            time.sleep(max(60, args.interval))
    else:
        one_round()


if __name__ == "__main__":
    main()
