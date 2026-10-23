"""The connection to the Gemini Live API: one WebSocket that hears the caller and speaks the answer.

Gemini Live replaces the three services of a classic voice pipeline (speech to text, language model, text to
speech). The protocol is JSON over one WebSocket:
  we send     setup (once, first), realtimeInput (microphone audio), toolResponse, clientContent (a text note)
  Gemini sends setupComplete, serverContent (audio, transcripts, interrupted, turnComplete), toolCall,
              toolCallCancellation, usageMetadata, sessionResumptionUpdate, goAway
Audio in is 16-bit PCM at 16 kHz and audio out is 16-bit PCM at 24 kHz, both base64 inside the JSON.

A connection lasts about 10 minutes. Before it ends Gemini sends goAway; we then open a new connection with
the latest session resumption handle, so the conversation goes on with its memory.
"""
import asyncio
import base64
import copy
import json
import logging
import time
from contextlib import suppress

import websockets

log = logging.getLogger("live")


class LiveError(Exception):
    """Gemini refused the connection or the setup (for example a wrong key or an unknown model name)."""


def close_reason(exc):
    """The code and reason Gemini gave when it closed the connection, e.g. '1008: API key not valid...'."""
    frame = getattr(exc, "rcvd", None)
    if frame is None:
        return f"{type(exc).__name__}: {exc}"
    return f"{frame.code}: {frame.reason or 'no reason given'}"


def parse(raw):
    """Gemini sends its JSON in binary frames; the tests may send text frames. Both are accepted."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


class GeminiLive:
    def __init__(self, settings, on_message, on_closed):
        """on_message(dict) is called for every message after setup. on_closed(reason) if the connection ends
        by itself (not when we close it, and not when it is replaced after a goAway)."""
        self.settings = settings
        self.on_message = on_message
        self.on_closed = on_closed
        self.websocket = None
        self.reader_task = None
        self.setup = None  # the setup message, kept so a new connection can repeat it
        self.handle = None  # the latest session resumption handle
        self.audio_bytes_sent = 0
        self.connections = 0
        self._outbox = asyncio.Queue()  # everything we send goes through one sender, in order, so nothing waits
        self._sender_task = None
        self._closing = False
        self._reconnecting = False
        self._background = set()

    # ================================================================ state
    def is_open(self):
        return (
            self.websocket is not None
            and self.websocket.close_code is None
            and self.reader_task is not None
            and not self.reader_task.done()
        )

    # ================================================================ connecting
    def build_setup(self, instructions, tools):
        """The first message of every connection: model, voice, prompt, tools, turn detection and transcripts."""
        s = self.settings
        detection = {"silenceDurationMs": s.live_silence_duration_ms}
        if s.live_prefix_padding_ms is not None:
            detection["prefixPaddingMs"] = s.live_prefix_padding_ms
        if s.live_start_sensitivity:
            detection["startOfSpeechSensitivity"] = s.live_start_sensitivity
        if s.live_end_sensitivity:
            detection["endOfSpeechSensitivity"] = s.live_end_sensitivity
        return {"setup": {
            "model": f"models/{s.gemini_live_model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": s.gemini_voice}}},
            },
            "systemInstruction": {"parts": [{"text": instructions}]},
            "tools": [{"functionDeclarations": tools}],
            "realtimeInputConfig": {
                "automaticActivityDetection": detection,
                # barge-in: the caller's voice stops the answer (NO_INTERRUPTION lets the answer finish)
                "activityHandling": "START_OF_ACTIVITY_INTERRUPTS" if s.barge_in_enabled else "NO_INTERRUPTION",
            },
            "inputAudioTranscription": {},  # what the caller said, as text: the guardrails and the confirmation need it
            "outputAudioTranscription": {},  # what Gemini said, as text: the output guardrail and the page need it
            "sessionResumption": {},  # Gemini sends handles, so a closing connection can be continued
            "contextWindowCompression": {"slidingWindow": {}},  # a long call drops its oldest part instead of ending
        }}

    async def connect(self, instructions, tools):
        self.setup = self.build_setup(instructions, tools)
        started = time.perf_counter()
        await self._open()
        log.info("Gemini Live ready (%s, voice %s) in %.0f ms", self.settings.gemini_live_model, self.settings.gemini_voice,
                 (time.perf_counter() - started) * 1000)

    async def _open(self):
        """Open a connection, send the setup and wait for setupComplete. Returns the connection it replaced."""
        s = self.settings
        setup = copy.deepcopy(self.setup)
        if self.handle:
            setup["setup"]["sessionResumption"] = {"handle": self.handle}
        url = f"{s.gemini_live_ws_url}?key={s.gemini_api_key}"  # the key is in the address: never log the url
        try:
            websocket = await websockets.connect(url, open_timeout=s.live_connect_timeout_seconds,
                                                 ping_interval=20, ping_timeout=20, max_size=None)
        except websockets.InvalidStatus as exc:
            raise LiveError(f"Gemini refused the connection (HTTP {exc.response.status_code})") from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise LiveError(f"could not reach Gemini: {type(exc).__name__}: {exc}") from exc
        try:
            await websocket.send(json.dumps(setup))
            first = parse(await asyncio.wait_for(websocket.recv(), s.live_connect_timeout_seconds))
            if "setupComplete" not in first:
                raise LiveError(f"Gemini answered the setup with {sorted(first)} instead of setupComplete")
            if self._closing:
                raise LiveError("the connection was closed while it was opening")
        except BaseException as exc:
            # never leave a half-open connection behind (it would keep a Gemini session open)
            with suppress(Exception):
                await websocket.close()
            if isinstance(exc, websockets.ConnectionClosed):
                raise LiveError(f"Gemini closed the connection during setup ({close_reason(exc)})") from exc
            if isinstance(exc, asyncio.TimeoutError):
                raise LiveError("Gemini did not answer the setup in time") from exc
            raise

        replaced, self.websocket = self.websocket, websocket
        self.connections += 1
        self.reader_task = asyncio.create_task(self._read_loop(websocket))
        if self._sender_task is None:
            self._sender_task = asyncio.create_task(self._send_loop())
        return replaced

    # ================================================================ sending
    def _queue(self, message):
        if self.websocket is None or self._closing:
            return
        self._outbox.put_nowait(json.dumps(message))

    def send_audio(self, pcm):
        """Never waits: the microphone audio is queued and sent in the background."""
        self.audio_bytes_sent += len(pcm)
        self._queue({"realtimeInput": {"audio": {
            "data": base64.b64encode(pcm).decode("ascii"),
            "mimeType": f"audio/pcm;rate={self.settings.gemini_input_sample_rate}",
        }}})

    def send_tool_response(self, responses):
        """responses: [{"id", "name", "response"}], matched by Gemini to its calls by id."""
        self._queue({"toolResponse": {"functionResponses": responses}})

    def send_note(self, text):
        """A text turn from us (not from the caller). It interrupts what Gemini is saying and makes it answer."""
        self._queue({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}})

    async def _send_loop(self):
        while True:
            text = await self._outbox.get()
            websocket = self.websocket
            try:
                await websocket.send(text)
            except Exception as exc:
                log.warning("could not send to Gemini: %s", type(exc).__name__)  # the reader reports a closed connection

    # ================================================================ receiving
    async def _read_loop(self, websocket):
        reason = "Gemini closed the connection"
        try:
            async for raw in websocket:
                try:
                    message = parse(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    log.warning("Gemini sent something that is not JSON")
                    continue
                if not self._handle_connection_message(message):
                    continue
                try:
                    await self.on_message(message)
                except Exception:
                    log.exception("error while handling a Gemini message")
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed as exc:
            reason = f"Gemini closed the connection ({close_reason(exc)})"
        except Exception as exc:
            reason = f"Gemini connection failed: {type(exc).__name__}: {exc}"
        else:
            reason = f"Gemini closed the connection ({websocket.close_code}: {websocket.close_reason or 'no reason given'})"

        if websocket is self.websocket and not self._closing and not self._reconnecting:
            log.warning(reason)
            await self.on_closed(reason)

    def _handle_connection_message(self, message):
        """Messages about the connection itself are handled here. Returns True if the session must see it too."""
        update = message.get("sessionResumptionUpdate")
        if update is not None:
            if update.get("resumable") and update.get("newHandle"):
                self.handle = update["newHandle"]
            return len(message) > 1
        if "goAway" in message:
            log.info("Gemini will close this connection in %s; opening a new one with the saved session",
                     message["goAway"].get("timeLeft", "a moment"))
            self._spawn(self._reconnect())
            return False
        return True

    async def _reconnect(self):
        if self._reconnecting or self._closing:
            return
        if not self.handle:
            log.warning("Gemini is closing the connection and gave no session handle; the call will end")
            return
        self._reconnecting = True
        problem = None
        try:
            old = await self._open()
            log.info("continued the conversation on a new Gemini connection (number %d)", self.connections)
            if old is not None:
                with suppress(Exception):
                    await old.close()
        except Exception as exc:
            problem = exc
            log.error("could not continue on a new Gemini connection: %s", exc)
        finally:
            self._reconnecting = False
        if problem is not None and not self.is_open() and not self._closing:
            # the old connection ended while we were trying, so its reader did not report it: report it here
            await self.on_closed(f"Gemini closed the connection and a new one could not be opened ({problem})")

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ================================================================ closing
    async def close(self):
        self._closing = True
        for task in list(self._background):  # for example a reconnect that is still opening its connection
            task.cancel()
        if self._sender_task is not None:
            self._sender_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._sender_task
            self._sender_task = None
        websocket = self.websocket
        if websocket is not None:
            with suppress(Exception):
                await websocket.close()
        if self.reader_task is not None and not self.reader_task.done():
            self.reader_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.reader_task
        self.websocket = None
        self.reader_task = None
