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
  Start Command: python scripts/telegram_standalone_bot.py

Durdurmak: Ctrl+C veya /stop
"""

from __future__ import annotations

import json
import os
import sys
import time
import requests

TIMEOUT = 30


def main() -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("HATA: Ortam degiskeni TELEGRAM_BOT_TOKEN tanimli degil.", file=sys.stderr)
        sys.exit(1)

    base = f"https://api.telegram.org/bot{token}"

    # Webhook varsa polling bos kalir
    wh = requests.get(f"{base}/getWebhookInfo", timeout=15).json()
    if wh.get("ok") and wh.get("result", {}).get("url"):
        print(f"Webhook kapatiliyor: {wh['result']['url'][:60]}...")
    dw = requests.post(
        f"{base}/deleteWebhook",
        json={"drop_pending_updates": True},
        timeout=15,
    ).json()
    if not dw.get("ok"):
        print(f"Uyari deleteWebhook: {dw}", file=sys.stderr)

    me = requests.get(f"{base}/getMe", timeout=15).json()
    if not me.get("ok"):
        print(f"HATA: Token gecersiz — {me.get('description', me)}", file=sys.stderr)
        sys.exit(1)
    username = me.get("result", {}).get("username", "?")
    print(f"Calisiyor: @{username} — Telegram'da bu bota ozelden yazin.")
    print("Komutlar: /start /help /ping  |  /stop (botu durdurur)")

    help_text = (
        "Merhaba! Bu bot sadece TELEGRAM_BOT_TOKEN ile calisir.\n\n"
        "/ping — baglanti testi\n"
        "/help — bu mesaj\n"
        "/stop — sunucudaki bot dongusunu durdurur (Railway'de proses biter)"
    )

    offset = 0
    while True:
        try:
            r = requests.get(
                f"{base}/getUpdates",
                params={
                    "timeout": 25,
                    "offset": offset,
                    "allowed_updates": json.dumps(["message", "edited_message"]),
                },
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                print(f"getUpdates hata: {data.get('description', data)}")
                time.sleep(3)
                continue

            for u in data.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message") or {}
                chat_id = msg.get("chat", {}).get("id")
                raw = (msg.get("text") or "").strip()
                tl = raw.lower()

                if tl in ("start", "help", "yardım", "yardim", "basla", "başla"):
                    raw = "/start"
                elif not raw.startswith("/"):
                    continue

                parts = raw.split()
                cmd = parts[0].split("@")[0].lower()
                reply = ""

                if cmd in ("/start", "/help"):
                    reply = help_text
                elif cmd == "/ping":
                    reply = "pong — bot ayakta."
                elif cmd in ("/stop", "/quit"):
                    sm = requests.post(
                        f"{base}/sendMessage",
                        json={"chat_id": chat_id, "text": "Tamam, kapaniyorum."},
                        timeout=15,
                    ).json()
                    if not sm.get("ok"):
                        print(f"sendMessage: {sm}")
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
                        print(f"sendMessage basarisiz: {sm.get('description', sm)}")
        except KeyboardInterrupt:
            print("\nCikildi.")
            return
        except Exception as e:
            print(f"poll: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
