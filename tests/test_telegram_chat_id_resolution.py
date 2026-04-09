# -*- coding: utf-8 -*-
"""Tests for Telegram chat id resolution in standalone crypto scripts."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DummyResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def load_script_module(script_name: str):
    path = ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(f"test_{script_name}_{uuid.uuid4().hex}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_signal_prefers_env_chat_id(monkeypatch):
    module = load_script_module("crypto_signal.py")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("requests.get should not be used when TELEGRAM_CHAT_ID is set")

    monkeypatch.setattr(module.requests, "get", fake_get)

    assert module._resolve_telegram_chat_id("token") == "12345"
    assert calls == []


def test_signal_auto_detects_single_chat_and_caches(monkeypatch):
    module = load_script_module("crypto_signal.py")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return DummyResponse(
            {
                "result": [
                    {"message": {"chat": {"id": 777, "first_name": "Berat"}}},
                    {"edited_message": {"chat": {"id": 777, "first_name": "Berat"}}},
                ]
            }
        )

    monkeypatch.setattr(module.requests, "get", fake_get)

    assert module._resolve_telegram_chat_id("token") == "777"
    assert module._resolve_telegram_chat_id("token") == "777"
    assert len(calls) == 1


def test_news_notifier_requires_explicit_chat_id_when_multiple_chats_exist(monkeypatch):
    module = load_script_module("crypto_news_notifier.py")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    def fake_get(*args, **kwargs):
        return DummyResponse(
            {
                "result": [
                    {"message": {"chat": {"id": 111, "first_name": "A"}}},
                    {"message": {"chat": {"id": 222, "first_name": "B"}}},
                ]
            }
        )

    monkeypatch.setattr(module.requests, "get", fake_get)

    assert module._resolve_telegram_chat_id("token") is None
