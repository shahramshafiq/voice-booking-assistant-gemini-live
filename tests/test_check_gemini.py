"""Tests for scripts/check_gemini.py against the fake Gemini server (no internet, no key)."""
import asyncio
from dataclasses import replace

import scripts.check_gemini as check
from app.config import settings as base_settings
from tests.fake_gemini_live import FakeGeminiLive


async def play_gemini(fake):
    """Answer the first note with a sentence, the second with a tool call and then a sentence."""
    while len(fake.notes) < 1:
        await asyncio.sleep(0.01)
    await fake.say("Hello, the voice works.", audio_ms=800)
    await fake.finish()
    while len(fake.notes) < 2:
        await asyncio.sleep(0.01)
    answer = await fake.call_tool("find_available_slots", {"service_id": "consultation"})
    assert answer["response"]["ok"]
    await fake.say("We have 3 PM or 4 PM tomorrow.", audio_ms=1200)
    await fake.finish()


def test_the_check_reports_the_setup_the_voice_and_a_tool_call(monkeypatch, capsys):
    async def scenario():
        fake = await FakeGeminiLive().start()
        monkeypatch.setattr(check, "settings", replace(base_settings, gemini_live_ws_url=fake.url))
        gemini = asyncio.create_task(play_gemini(fake))
        code = await check.main(speak=True, tool=True)
        await gemini
        await fake.stop()
        return code

    assert asyncio.run(scenario()) == 0
    out = capsys.readouterr().out
    assert "OK: Gemini accepted the setup (model gemini-3.8-live, voice Kore, 8 tools)" in out
    assert "0.8 s of sound" in out and "tool call after" in out and "find_available_slots" in out


def test_the_check_explains_a_wrong_key(monkeypatch, capsys):
    async def scenario():
        fake = await FakeGeminiLive().start()
        fake.reject = (1007, "API key not valid. Please pass a valid API key.")
        monkeypatch.setattr(check, "settings", replace(base_settings, gemini_live_ws_url=fake.url))
        code = await check.main(speak=False, tool=False)
        await fake.stop()
        return code

    assert asyncio.run(scenario()) == 1
    assert "FAILED" in capsys.readouterr().out
