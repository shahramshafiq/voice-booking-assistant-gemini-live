"""A check of the Gemini settings against the real service. Run it after putting GEMINI_API_KEY in .env.

Run it from the project folder:
    python -m scripts.check_gemini           opens the real setup (prompt, the eight tools, the voice) and closes it.
                                             Nothing is generated, so it costs nothing.
    python -m scripts.check_gemini --speak   also asks for one short spoken sentence and times it (a tiny cost)
    python -m scripts.check_gemini --tool    also asks a question that needs a tool, answers the tool with made-up
                                             free times and times the spoken answer (a tiny cost; the calendar is not used)
"""
import asyncio
import base64
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.live import GeminiLive, LiveError
from app.prompt import build_instructions
from app.tools import build_definitions

WAIT_SECONDS = 20


class Listener:
    def __init__(self):
        self.messages = asyncio.Queue()

    async def on_message(self, message):
        await self.messages.put(message)

    async def on_closed(self, reason):
        await self.messages.put({"closed": reason})

    async def answer(self, live, started):
        """Collect one spoken answer until turnComplete. Answers a tool call with made-up free times."""
        first_audio = None
        audio = 0
        said = ""
        deadline = time.perf_counter() + WAIT_SECONDS
        while time.perf_counter() < deadline:
            try:
                message = await asyncio.wait_for(self.messages.get(), deadline - time.perf_counter())
            except asyncio.TimeoutError:
                break
            if "closed" in message:
                print(f"FAILED: {message['closed']}")
                return False
            if "toolCall" in message:
                for call in message["toolCall"].get("functionCalls", []):
                    print(f"  tool call after {(time.perf_counter() - started) * 1000:.0f} ms: {call.get('name')} {call.get('args')}")
                    live.send_tool_response([{"id": call.get("id"), "name": call.get("name"), "response": {
                        "ok": True, "service": {"name": "Consultation"},
                        "slots": [{"label": "tomorrow at 3 PM"}, {"label": "tomorrow at 4 PM"}]}}])
            content = message.get("serverContent") or {}
            for part in (content.get("modelTurn") or {}).get("parts") or []:
                data = (part.get("inlineData") or {}).get("data")
                if data:
                    first_audio = first_audio or time.perf_counter()
                    audio += len(base64.b64decode(data))
            said += (content.get("outputTranscription") or {}).get("text", "")
            if content.get("turnComplete"):
                break
        if not audio:
            print("FAILED: no sound came back.")
            return False
        seconds = audio / (settings.gemini_output_sample_rate * 2)
        print(f"  first sound after {(first_audio - started) * 1000:.0f} ms, {seconds:.1f} s of sound")
        print(f"  Gemini said: {said.strip()!r}")
        return True


async def main(speak, tool):
    listener = Listener()
    live = GeminiLive(settings, listener.on_message, listener.on_closed)
    now = datetime.now(ZoneInfo(settings.business_timezone))
    started = time.perf_counter()
    try:
        await live.connect(build_instructions(settings, now), build_definitions())
    except LiveError as error:
        print(f"FAILED: {error}")
        print("Check GEMINI_API_KEY in .env, and GEMINI_LIVE_MODEL and GEMINI_VOICE in .env.example.")
        return 1
    print(f"OK: Gemini accepted the setup (model {settings.gemini_live_model}, voice {settings.gemini_voice}, "
          f"{len(build_definitions())} tools) in {(time.perf_counter() - started) * 1000:.0f} ms.")
    ok = True
    try:
        if speak:
            print("Asking for one short sentence...")
            started = time.perf_counter()
            live.send_note("[System note] Say only this sentence: Hello, the voice works.")
            ok = await listener.answer(live, started) and ok
        if tool:
            print("Asking a question that needs a tool...")
            started = time.perf_counter()
            live.send_note("What times are free tomorrow afternoon for a consultation?")
            ok = await listener.answer(live, started) and ok
    finally:
        await live.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--speak" in sys.argv, "--tool" in sys.argv)))
