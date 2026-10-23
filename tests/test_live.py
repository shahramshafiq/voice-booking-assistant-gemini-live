"""Tests for the Gemini Live client against the fake Gemini server (no internet, no key)."""
import asyncio
import base64
from dataclasses import replace

from app.config import settings as base_settings
from app.live import GeminiLive, LiveError
from tests.fake_gemini_live import FakeGeminiLive


async def until(condition, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("waited too long")


class Recorder:
    def __init__(self):
        self.messages = []
        self.closed = []

    async def on_message(self, message):
        self.messages.append(message)

    async def on_closed(self, reason):
        self.closed.append(reason)


async def connected(**changes):
    fake = await FakeGeminiLive().start()
    recorder = Recorder()
    live = GeminiLive(replace(base_settings, gemini_live_ws_url=fake.url, **changes), recorder.on_message, recorder.on_closed)
    await live.connect("You are a receptionist.", [{"name": "get_services", "description": "List the services."}])
    return fake, live, recorder


def test_the_setup_carries_the_model_voice_prompt_tools_and_transcripts():
    async def scenario():
        fake, live, _ = await connected()
        setup = fake.setups[0]["setup"]
        assert setup["model"] == "models/gemini-3.8-live"
        assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
        assert setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
        assert setup["systemInstruction"]["parts"][0]["text"] == "You are a receptionist."
        assert setup["tools"][0]["functionDeclarations"][0]["name"] == "get_services"
        assert setup["inputAudioTranscription"] == {} and setup["outputAudioTranscription"] == {}
        assert setup["realtimeInputConfig"]["automaticActivityDetection"] == {"silenceDurationMs": 700}
        assert setup["realtimeInputConfig"]["activityHandling"] == "START_OF_ACTIVITY_INTERRUPTS"
        assert setup["sessionResumption"] == {} and "slidingWindow" in setup["contextWindowCompression"]
        assert fake.keys == ["test-value"] and live.is_open()
        await live.close()
        assert not live.is_open()
        await fake.stop()

    asyncio.run(scenario())


def test_optional_turn_settings_are_only_sent_when_set():
    async def scenario():
        fake, live, _ = await connected(barge_in_enabled=False, live_prefix_padding_ms=120,
                                        live_end_sensitivity="END_SENSITIVITY_HIGH")
        config = fake.setups[0]["setup"]["realtimeInputConfig"]
        assert config["activityHandling"] == "NO_INTERRUPTION"  # barge-in off: the answer always finishes
        assert config["automaticActivityDetection"] == {"silenceDurationMs": 700, "prefixPaddingMs": 120,
                                                        "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH"}
        await live.close()
        await fake.stop()

    asyncio.run(scenario())


def test_microphone_audio_is_sent_as_base64_pcm_with_its_rate():
    async def scenario():
        fake, live, _ = await connected()
        live.send_audio(b"\x10\x00" * 640)  # 40 ms at 16 kHz
        live.send_audio(b"\x20\x00" * 640)
        await until(lambda: len(fake.audio) == 2560)
        assert fake.audio == b"\x10\x00" * 640 + b"\x20\x00" * 640
        assert fake.audio_mime == {"audio/pcm;rate=16000"}
        await live.close()
        await fake.stop()

    asyncio.run(scenario())


def test_gemini_messages_reach_the_session_and_tool_answers_go_back():
    async def scenario():
        fake, live, recorder = await connected()
        await fake.hear("I'd like to book")
        await fake.call_tool("get_services", {}, call_id="abc", wait=False)
        await until(lambda: len(recorder.messages) == 2)
        assert recorder.messages[0]["serverContent"]["inputTranscription"]["text"] == "I'd like to book"
        assert recorder.messages[1]["toolCall"]["functionCalls"][0]["id"] == "abc"
        live.send_tool_response([{"id": "abc", "name": "get_services", "response": {"ok": True}}])
        answer = await asyncio.wait_for(fake.tool_responses.get(), 2)
        assert answer == [{"id": "abc", "name": "get_services", "response": {"ok": True}}]
        live.send_note("Say hello.")
        await until(lambda: fake.notes == ["Say hello."])
        await live.close()
        await fake.stop()

    asyncio.run(scenario())


def test_a_wrong_key_gives_a_clear_error():
    async def scenario():
        fake = await FakeGeminiLive().start()
        fake.reject = (1007, "API key not valid. Please pass a valid API key.")  # what the real service sends
        live = GeminiLive(replace(base_settings, gemini_live_ws_url=fake.url), Recorder().on_message, Recorder().on_closed)
        try:
            await live.connect("x", [])
        except LiveError as error:
            assert "1007: API key not valid" in str(error)
        else:
            raise AssertionError("a wrong key must raise LiveError")
        await fake.stop()

    asyncio.run(scenario())


def test_go_away_moves_the_call_to_a_new_connection_with_the_saved_handle():
    async def scenario():
        fake, live, recorder = await connected()
        first = fake.connection
        await fake.go_away("handle-1")
        await until(lambda: fake.connections == 2)
        assert fake.handles == [None, "handle-1"]  # the new connection continues the same conversation
        assert fake.setups[1]["setup"]["systemInstruction"] == fake.setups[0]["setup"]["systemInstruction"]
        await until(lambda: first.close_code is not None)  # the old connection is closed
        live.send_audio(b"\x05\x00" * 640)
        await until(lambda: len(fake.audio) == 1280)  # audio now flows on the new connection
        assert recorder.closed == [] and recorder.messages == []  # the session never noticed
        await live.close()
        await fake.stop()

    asyncio.run(scenario())


def test_closing_while_the_connection_is_still_opening_leaves_nothing_open():
    async def scenario():
        fake = await FakeGeminiLive().start()
        fake.setup_delay = 0.3
        live = GeminiLive(replace(base_settings, gemini_live_ws_url=fake.url), Recorder().on_message, Recorder().on_closed)
        opening = asyncio.create_task(live.connect("x", []))
        await until(lambda: fake.setups)
        await live.close()  # for example the page was closed while it was still getting ready
        try:
            await opening
        except LiveError:
            pass
        await until(lambda: fake.closed_connections == 1)  # the half-open connection was closed, not leaked
        assert not live.is_open()
        await fake.stop()

    asyncio.run(scenario())


def test_a_failed_move_to_a_new_connection_ends_the_call_with_a_reason():
    async def scenario():
        fake, live, recorder = await connected()
        fake.setup_delay = 0.3  # the new connection is slow...
        fake.reject = (1011, "The service is busy.")  # ...and then refused
        await fake.go_away("handle-1")
        await until(lambda: len(fake.setups) == 2)
        await fake.connection.close(1000, "time is up")  # the old connection ends while the new one is being opened
        await until(lambda: recorder.closed)
        assert "1011: The service is busy." in recorder.closed[0]  # so the session can end the call instead of hanging
        await live.close()
        await fake.stop()

    asyncio.run(scenario())


def test_a_connection_that_ends_by_itself_is_reported_with_the_reason():
    async def scenario():
        fake, live, recorder = await connected()
        await fake.connection.close(1011, "Internal error encountered.")
        await until(lambda: recorder.closed)
        assert "1011: Internal error encountered." in recorder.closed[0]
        await live.close()
        await fake.stop()

    asyncio.run(scenario())


def test_the_audio_and_text_frames_of_the_real_service_are_both_understood():
    async def scenario():
        fake, live, recorder = await connected()
        await fake.connection.send('{"serverContent": {"turnComplete": true}}')  # a text frame instead of binary
        await fake.say("Hello.", audio_ms=100)
        await until(lambda: len(recorder.messages) == 4)
        parts = [m["serverContent"]["modelTurn"]["parts"][0]["inlineData"]["data"] for m in recorder.messages[1:3]]
        assert sum(len(base64.b64decode(p)) for p in parts) == 4800  # 100 ms at 24 kHz, 2 bytes per sample
        await live.close()
        await fake.stop()

    asyncio.run(scenario())
