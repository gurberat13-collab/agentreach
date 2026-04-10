#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal Telegram bot — yalnizca TELEGRAM_BOT_TOKEN gerekir.

TELEGRAM_CHAT_ID kullanmaz: cevap, mesaji gonderen sohbete otomatik gider.

Yerel:
  set TELEGRAM_BOT_TOKEN=123456:ABC...
  python scripts/telegram_standalone_bot.py

Railway:
  Variables: TELEGRAM_BOT_TOKEN
  Start Command: pip install -r requirements-crypto-worker.txt && python scripts/telegram_standalone_bot.py

TELEGRAM_DEBUG=1 — her guncellemeyi konsola yazar.

Durdurmak: Ctrl+C veya /stop
"""

from __future__ import annotations

import os
import sys
import time

import requests

TIMEOUT = 35


def _norm_text(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("\u200b", "").replace("\ufeff", "")
    return s.strip()


def main() -> None:
    debug = os.environ.get("TELEGRAM_DEBUG", "").lower() in ("1", "true", "yes")
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("HATA: Ortam degiskeni TELEGRAM_BOT_TOKEN tanimli degil.", file=sys.stderr)
        sys.exit(1)

    if ":" not in token or len(token) < 20:
        print("HATA: TELEGRAM_BOT_TOKEN '123456:ABC...' formatinda olmali.", file=sys.stderr)
        sys.exit(1)

    base = f"https://api.telegram.org/bot{token}"

    # Webhook aciksa getUpdates bos doner
    try:
        wh = requests.get(f"{base}/getWebhookInfo", timeout=15).json()
        if wh.get("ok") and wh.get("result", {}).get("url"):
            print(f"Webhook kapatiliyor: {wh['result']['url'][:70]}...")
        dw = requests.post(
            f"{base}/deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=15,
        ).json()
        if not dw.get("ok"):
            print(f"Uyari deleteWebhook: {dw}", file=sys.stderr)
    except Exception as e:
        print(f"Webhook temizligi: {e}", file=sys.stderr)

    me = requests.get(f"{base}/getMe", timeout=15).json()
    if not me.get("ok"):
        print(f"HATA: Token gecersiz — {me.get('description', me)}", file=sys.stderr)
        sys.exit(1)
    username = me.get("result", {}).get("username", "?")
    print(f"Calisiyor: @{username}")
    print("Telegram'da bu bota OZELDEN /start yazin (grup degil).")
    print("Komutlar: /start /help /ping | /stop")
    print("--- Log: her mesajda 'gelen:' satiri gorursunuz (Railway Logs). ---")

    help_text = (
        "Merhaba! Bu bot sadece TELEGRAM_BOT_TOKEN ile calisir.\n\n"
        "/ping — baglanti testi\n"
        "/help — bu mesaj\n"
        "/stop — prosesi durdurur (Railway yeniden baslatabilir)"
    )

    offset = 0
    while True:
        try:
            # allowed_updates KULLANMA — bazi istemcilerde guncelleme gelmeyebilir
            r = requests.get(
                f"{base}/getUpdates",
                params={"timeout": 25, "offset": offset},
                timeout=TIMEOUT,
            )

            if r.status_code == 409:
                print(
                    "HATA HTTP 409: Ayni bot baska bir yerde de getUpdates ile dinleniyor. "
                    "Railway + PC ayni anda, veya iki Railway servisi — birini durdurun.",
                    file=sys.stderr,
                )
                time.sleep(5)
                continue

            try:
                data = r.json()
            except Exception:
                print(f"getUpdates JSON degil: {r.text[:300]}", file=sys.stderr)
                time.sleep(3)
                continue

            if not data.get("ok"):
                ec = data.get("error_code")
                desc = data.get("description", data)
                print(f"getUpdates ok=false: {desc}", file=sys.stderr)
                if ec == 409:
                    time.sleep(5)
                else:
                    time.sleep(2)
                continue

            updates = data.get("result", [])
            if debug and updates:
                print(f"[debug] {len(updates)} guncelleme")

            for u in updates:
                offset = u["update_id"] + 1
                if debug:
                    print(f"[debug] update_id={u['update_id']} keys={list(u.keys())}")

                msg = u.get("message") or u.get("edited_message") or {}
                chat_id = msg.get("chat", {}).get("id")
                chat_type = (msg.get("chat") or {}).get("type", "?")
                raw = _norm_text(msg.get("text") or "")
                if debug or raw:
                    print(f"gelen: chat_id={chat_id} tip={chat_type} metin={raw!r}")

                # Metin yok (sticker, foto vb.)
                if not raw:
                    continue

                tl = raw.lower()
                if tl in ("start", "help", "yardım", "yardim", "basla", "başla"):
                    raw = "/start"
                elif not raw.startswith("/"):
                    if chat_type == "private":
                        raw = "/start"
                    else:
                        continue

                parts = raw.split()
                cmd = parts[0].split("@")[0].lower()
                reply = ""

                if cmd in ("/start", "/help"):
                    reply = help_text
                elif cmd == "/ping":
                    reply = "pong — bot ayakta."
                elif cmd in ("/stop", "/quit"):
                    if chat_id is not None:
                        requests.post(
                            f"{base}/sendMessage",
                            json={"chat_id": chat_id, "text": "Tamam, kapaniyorum."},
                            timeout=15,
                        )
                    return
                else:
                    reply = help_text

                if chat_id is not None and reply:
                    sm = requests.post(
                        f"{base}/sendMessage",
                        json={"chat_id": chat_id, "text": reply[:4000]},
                        timeout=15,
                    ).json()
                    if not sm.get("ok"):
                        print(
                            f"sendMessage basarisiz: {sm.get('description', sm)}",
                            file=sys.stderr,
                        )
        except KeyboardInterrupt:
            print("\nCikildi.")
            return
        except requests.RequestException as e:
            print(f"poll ag hatasi: {e}", file=sys.stderr)
            time.sleep(3)
        except Exception as e:
            print(f"poll: {e}", file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    main()
