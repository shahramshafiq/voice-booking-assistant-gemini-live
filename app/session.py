"""One browser connection = one VoiceSession.

The session connects the browser to Gemini Live. Gemini hears the caller, decides when they have finished,
thinks and speaks. The session passes the audio both ways and keeps everything that must stay in code: the
booking tools and their confirmation gate, the guardrails, the short-term memory and the latency log.

Messages from the browser:
  binary                      microphone audio (16-bit PCM, mono, 16 kHz)
  {"type": "start"}           begin the conversation
  {"type": "stop"}            end the conversation
  {"type": "playback_started"}   the speakers just began playing the answer
  {"type": "playback_progress", "played_ms": N}   how much of the answer has been played
  {"type": "playback_drained"}   the speakers finished playing everything

Messages to the browser:
  binary                      the answer audio (16-bit PCM, mono, 24 kHz)
  JSON events: config, ready, state, turn, user_text, reply, assistant_text, tool, audio_end, clear,
               interrupted, metrics, warning, calendar_ok, error, idle_timeout
"""
import asyncio
import base64
import json
import logging
import math
import re
import time
from array import array

from .booking.calendar_client import CalendarError
from .booking.speech import tidy_numbers
from .live import GeminiLive
from .memory import SessionMemory
from .prompt import build_instructions
from .timing import TurnTimer
from .tools import ToolContext, ToolRunner, build_definitions, for_model

log = logging.getLogger("session")

# What Gemini is told when the output guardrail stopped an answer.
NOTE = "[System note] {why} Do not go on with what you were saying. Say exactly this sentence and nothing else: {sentence}"
# An answer that starts this long after the caller's last loud sound is not an answer to their speech (for the latency log).
SPEECH_IS_RECENT_SECONDS = 10


def _content_words(text):
    """Words of three letters or more. Digits are left out: transcripts write spoken digits in different ways."""
    return set(re.findall(r"[a-z']{3,}", text.lower().replace("\u2019", "'")))


WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
RELATIVE_DAYS = {"today", "tomorrow"}
MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
TIME = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s?m\b")  # 9 AM, 9:30 pm, 9.30 a.m.
DATE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTHS})\b")  # 20 October, the 20th of October


def _facts(text):
    """The times, dates and day words in a sentence: ({(9, 0, 'a'), (20, 'october'), 'tomorrow'}, {'tuesday'})."""
    low = text.lower()
    facts = {(int(hour) % 12, int(minute or 0), half) for hour, minute, half in TIME.findall(low)}
    facts |= {(int(day), month) for day, month in DATE.findall(low)}
    words = set(re.findall(r"[a-z]+", low))
    return facts | (words & RELATIVE_DAYS), words & WEEKDAYS


def _read_back_differs(question, said):
    """Did Gemini name a time, a date or a day that the code's question does not have (10 AM instead of 9 AM)?
    Numbers Gemini's transcript writes as words are not judged. A weekday next to "tomorrow" is allowed."""
    asked, asked_weekdays = _facts(question)
    told, told_weekdays = _facts(said)
    return bool(told - asked) or bool(asked_weekdays and told_weekdays - asked_weekdays)


def _repeated_sentence(text):
    """'Alright, one moment.Alright, one moment.' -> 'alright, one moment.': a sentence said twice in a row."""
    sentences = [s.strip().lower() for s in re.findall(r"[^.?!]+[.?!]?", text) if s.strip()]
    return next((a for a, b in zip(sentences, sentences[1:]) if a == b), None)


DETAILS_END = re.compile(r", for |\. (?:Shall I|Is that right)")


def _details_part(question):
    """The part of the read-back a caller must hear before their yes counts: the service, the day and the time.
    The name, the phone number and the closing question ("Shall I book it?") may be talked over."""
    match = DETAILS_END.search(question)
    return question[:match.start()] if match else question


def _speaking_units(text):
    """A rough spoken length of a text in word-sized units: a word is 1, a number of one or two digits 1.2, every digit
    of a longer number 1.2 (it is read one digit at a time), and a comma or a full stop is a short pause (0.6)."""
    units = 0.0
    for token in re.findall(r"[A-Za-z']+|\d+|[,.:;?!]", text):
        if token[0].isdigit():
            units += 1.2 * (len(token) if len(token) > 2 else 1)
        elif token in ",.:;?!":
            units += 0.6
        else:
            units += 1
    return units


class Reply:
    """One answer from Gemini: from its first sound, text or tool call until turnComplete or an interruption."""

    def __init__(self, number, timer, after_speech):
        self.number = number
        self.timer = timer
        self.after_speech = after_speech  # it answers something the caller just said (so its latency means something)
        self.audio_bytes = 0
        self.offset_ms = 0.0  # sound still waiting in the browser's player when this answer began
        self.text = ""  # what Gemini said, from its own transcript
        self.finished = False  # turnComplete: all of its audio has arrived
        self.blocked = False  # the output guardrail stopped it: the rest of its audio is dropped
        self.replacement = None  # what Gemini is asked to say instead, once this answer has ended
        self.warned_reference = False
        self.reported = False  # its latency was reported
        self.usage = None

    def audio_ms(self, rate):
        return self.audio_bytes / (rate * 2) * 1000  # 16-bit mono: 2 bytes per sample


class VoiceSession:
    def __init__(self, websocket, settings, service, input_rail, output_rail):
        self.ws = websocket
        self.settings = settings
        self.service = service
        self.input_rail = input_rail
        self.output_rail = output_rail
        self.definitions = build_definitions()
        self.live = None  # the Gemini connection; a new one for every conversation

        self.memory = SessionMemory(settings)  # replaced by a fresh one every time a conversation starts
        self.runner = ToolRunner(settings, service, self.memory)
        self.reply = None  # the answer being generated
        self._playing = []  # answers that have all arrived and are still being played by the browser
        self.replies = 0
        self.state = "idle"  # idle, connecting, listening, thinking, speaking
        self.awaiting_drain = False  # all audio of the answer arrived, the speakers are still playing it
        self.played_ms = 0  # what the browser has played since its player was last empty
        self._queued_ms = 0.0  # what we have sent to the browser since its player was last empty
        self.last_activity = time.monotonic()
        self.tokens_used = 0
        self.closed = False

        self._caller_open = False  # a caller turn is open: their words go into it
        self._caller_text = ""
        self._caller_shown = ""  # the same with card numbers removed (for the page and the log)
        self._caller_blocked = False  # the input guardrail blocked this turn: no tool runs for it
        self._turn_tool_calls = 0  # tool calls Gemini made for the caller's latest words
        self._spoke_this_turn = False  # Gemini has made a sound since the caller's latest words
        self._bubble_waiting = False  # the page has an empty pair of bubbles for words that have not arrived yet
        self._last_loud = None  # when the microphone last carried speech (perf_counter)
        self._loud_while_playing = None  # the same while the speakers were playing (it may be their own sound)
        self._drained_at = None  # when the speakers last finished playing everything (perf_counter)
        self._levels = ([], [])  # microphone loudness of the loud and the quiet pieces in this turn
        self._tool_tasks = {}  # Gemini's call id -> running tool
        self._send_lock = asyncio.Lock()  # one message at a time to the browser
        self._lock = asyncio.Lock()  # one state change at a time
        self._connect_lock = asyncio.Lock()
        self._drain_task = None
        self._idle_task = None
        self._calendar_task = None
        self._background = set()
        self._calendar_problem = None  # the calendar's last problem, kept across conversations
        self._calendar_warning = None  # the text shown in the browser for it
        self._warmed_at = None  # when the calendar window was last refreshed

    # ================================================================ sending to the browser
    async def send_event(self, event):
        if self.closed:
            return
        try:
            async with self._send_lock:
                await self.ws.send_json(event)
        except Exception as exc:
            log.debug("could not send %s to the browser: %s", event.get("type"), type(exc).__name__)

    async def send_audio(self, data):
        if self.closed:
            return
        try:
            async with self._send_lock:
                await self.ws.send_bytes(data)
        except Exception as exc:
            log.debug("could not send audio to the browser: %s", type(exc).__name__)

    async def _set_state(self, value):
        if self.state != value:
            self.state = value
            await self.send_event({"type": "state", "value": value})

    # ================================================================ main loop (browser -> server)
    async def run(self):
        await self.send_event({
            "type": "config",
            "mic_sample_rate": self.settings.gemini_input_sample_rate,
            "mic_chunk_ms": self.settings.mic_chunk_ms,
            "output_sample_rate": self.settings.gemini_output_sample_rate,
            "barge_in_enabled": self.settings.barge_in_enabled,
        })
        # While the page just sits open, connect to Gemini and load the calendar, so that Start is instant.
        self._spawn(self._prewarm())
        self._calendar_task = asyncio.create_task(self._calendar_watch())
        try:
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    self._on_mic_audio(message["bytes"])
                elif message.get("text") is not None:
                    try:
                        control = json.loads(message["text"])
                    except json.JSONDecodeError:
                        log.warning("the browser sent a message that is not JSON")
                        continue
                    await self._on_control(control)
        except Exception:
            log.exception("browser connection failed")
        finally:
            await self.shutdown()

    def _on_mic_audio(self, data):
        live = self.live
        if live is None or self.state in ("idle", "connecting") or not live.is_open():
            return
        live.send_audio(data)
        self._measure(data)

    def _measure(self, data):
        """A loudness check on each microphone piece. Gemini does not say when it decided the caller had finished,
        so the end of the caller's speech is estimated here, for the latency log."""
        if len(data) < 2:
            return
        samples = array("h", data[:len(data) // 2 * 2])
        level = math.sqrt(sum(x * x for x in samples) / len(samples))
        loud = level >= self.settings.local_speech_rms
        if loud and self._queued_ms > 0:
            # The speakers are playing, and without headphones the microphone hears them. This only counts as the
            # caller's speech if their words arrive (a barge-in), see _on_caller_words.
            self._loud_while_playing = time.perf_counter()
        elif loud:
            self._last_loud = time.perf_counter()
        levels = self._levels[0 if loud else 1]
        if len(levels) < 2000:
            levels.append(level)

    async def _on_control(self, message):
        kind = message.get("type")
        if kind == "start":
            await self._start_conversation()
        elif kind == "stop":
            await self._stop_conversation()
        elif kind == "playback_started":
            await self._on_playback_started()
        elif kind == "playback_progress":
            await self._on_playback_progress(int(message.get("played_ms", 0)))
        elif kind == "playback_drained":
            async with self._lock:
                if self._playing and self.awaiting_drain:
                    await self._finish_playback_nolock()
        else:
            log.debug("unknown message from the browser: %s", kind)

    # ================================================================ start / stop
    async def _start_conversation(self):
        if self.state != "idle":
            return
        await self._set_state("connecting")

        # Gemini and the calendar were normally opened while the page was loading; anything that is not open is opened now
        results = await self._connect_all()
        if isinstance(results[0], Exception):
            log.error("could not start: %s", results[0])
            await self.send_event({"type": "error", "message": f"Could not connect to Gemini: {results[0]}"})
            await self._stop_conversation(rewarm=False)
            return

        # a calendar problem does not stop the call: the assistant tells the caller honestly instead
        if self._calendar_warning:
            await self.send_event({"type": "warning", "message": self._calendar_warning})

        # every conversation starts with an empty short-term memory (and its own Gemini session)
        self.memory = SessionMemory(self.settings)
        self.runner = ToolRunner(self.settings, self.service, self.memory)
        self.reply = None
        self._playing = []
        self.replies = 0
        self.tokens_used = 0
        self._caller_open = False
        self._turn_tool_calls = 0
        self._spoke_this_turn = False
        self._bubble_waiting = False
        self._last_loud = self._loud_while_playing = self._drained_at = None
        self._levels = ([], [])
        self.last_activity = time.monotonic()
        await self._set_state("listening")
        await self.send_event({"type": "ready"})
        self._idle_task = asyncio.create_task(self._idle_watchdog())
        log.info("session ready")

    async def _prewarm(self):
        """Runs when the page opens and again after every Stop, so that pressing Start has nothing left to wait for."""
        started = time.perf_counter()
        results = await self._connect_all()
        if isinstance(results[0], Exception):
            log.warning("could not prepare Gemini (it will be tried again at Start): %s", results[0])
            await self.send_event({"type": "warning", "message": f"Gemini is not reachable: {results[0]}"})
            return
        log.info("ready for Start (connections opened in %.0f ms)", (time.perf_counter() - started) * 1000)

    async def _connect_all(self):
        """Open whatever is not open yet; anything already open is kept. Returns [gemini, calendar] results."""
        async with self._connect_lock:
            live = self.live
            live_ready = (
                live is not None
                and live.is_open()
                and time.monotonic() - live.opened_at < self.settings.live_prewarm_max_age_seconds  # its date is current
            )
            window_is_old = (
                self._warmed_at is None
                or self._calendar_problem is not None
                or time.monotonic() - self._warmed_at > self.settings.calendar_cache_seconds
            )
            return await asyncio.gather(
                self._nothing() if live_ready else self._open_live(),
                self._warm_calendar() if window_is_old else self._nothing(),
                return_exceptions=True,
            )

    @staticmethod
    async def _nothing():
        return None

    async def _open_live(self):
        """A new Gemini session with fresh instructions (they carry the current date and time)."""
        if self.live is not None:
            await self.live.close()
        if self.closed:
            return  # the page has gone meanwhile
        live = GeminiLive(self.settings, self._on_live_message, None)
        live.on_closed = lambda reason: self._on_live_closed(live, reason)
        self.live = live
        await live.connect(build_instructions(self.settings, self.service.now()), self.definitions)

    async def _stop_conversation(self, error_message=None, rewarm=True):
        async with self._lock:
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            for task in list(self._tool_tasks.values()):
                task.cancel()  # a change that has started still finishes (the tool runner protects it)
            self._cancel_drain_watchdog()
            self.awaiting_drain = False
            self.reply = None
            self._playing = []
            self._caller_open = False
            live, self.live = self.live, None  # the next conversation gets a new Gemini session: it never sees this one
            await self.send_event({"type": "clear"})
            self._speakers_emptied()
            if error_message:
                await self.send_event({"type": "error", "message": error_message})
            if self.tokens_used:
                log.info("this call used %d tokens (as reported by Gemini)", self.tokens_used)
                self.tokens_used = 0
            await self._set_state("idle")
        if live is not None:
            # Closed only after the lock is free: Gemini's reader may be waiting for the lock with its last messages,
            # and it has to keep reading for the close to finish (otherwise it waits for a 10 s timeout).
            await live.close()
        if rewarm and not self.closed:
            self._spawn(self._prewarm())  # so the next Start is instant too

    async def shutdown(self):
        if self.closed:
            return
        if self._calendar_task is not None:
            self._calendar_task.cancel()
            self._calendar_task = None
        await self._stop_conversation(rewarm=False)
        self.closed = True
        for task in list(self._background):  # for example a prewarm that is still connecting to Gemini
            task.cancel()
        if self.live is not None:
            await self.live.close()
            self.live = None
        log.info("session closed")

    async def _on_live_closed(self, live, reason):
        if live is not self.live or self.closed:
            return  # an old connection
        if self.state == "idle":
            log.info("the waiting Gemini connection closed (%s); a new one opens at Start", reason)
            return
        # run in a separate task: stopping closes the reader that is calling us
        self._spawn(self._stop_conversation(error_message=f"Gemini stopped the call: {reason}"))

    async def _idle_watchdog(self):
        """Stop the call if nobody has spoken for a while."""
        while True:
            await asyncio.sleep(5)
            idle_for = time.monotonic() - self.last_activity
            if self.state == "listening" and idle_for > self.settings.idle_timeout_seconds:
                log.info("no activity for %.0f s, stopping", idle_for)
                await self.send_event({"type": "idle_timeout"})
                self._spawn(self._stop_conversation())
                return

    async def _calendar_watch(self):
        """Refreshes the in-memory calendar window and notices when the token expires during the call.
        It refreshes before the memory would go stale, so a question never has to wait for Google."""
        pause = min(self.settings.calendar_warm_interval_seconds, self.settings.calendar_cache_seconds * 0.7)
        while True:
            await asyncio.sleep(pause)
            await self._warm_calendar()

    async def _warm_calendar(self):
        """Refresh the in-memory calendar window. Never raises: a problem is remembered and shown instead."""
        try:
            await self.service.warm()
        except CalendarError as exc:
            await self._set_calendar_problem(exc.message, getattr(exc, "hint", ""))
        except Exception as exc:
            await self._set_calendar_problem(f"unexpected problem ({type(exc).__name__})")
        else:
            self._warmed_at = time.monotonic()
            if self._calendar_problem is not None:
                log.info("the calendar is reachable again")
                self._calendar_problem = self._calendar_warning = None
                await self.send_event({"type": "calendar_ok"})

    async def _set_calendar_problem(self, message, hint=""):
        """`message` is safe for the assistant to know; `hint` is advice for you, shown in the browser only."""
        if self._calendar_problem != message:
            log.warning("calendar problem: %s", message)
            self._calendar_problem = message
            self._calendar_warning = f"Calendar problem: {message} {hint}".strip()
            await self.send_event({"type": "warning", "message": self._calendar_warning})

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ================================================================ Gemini -> session
    async def _on_live_message(self, message):
        started = time.perf_counter()
        async with self._lock:
            if self.state in ("idle", "connecting"):
                return  # a late message for a call that has ended
            content = message.get("serverContent") or {}
            heard = (content.get("inputTranscription") or {}).get("text")
            if heard:
                await self._on_caller_words(heard)
            if content.get("interrupted"):
                await self._on_interrupted()
            for part in (content.get("modelTurn") or {}).get("parts") or []:
                data = (part.get("inlineData") or {}).get("data")
                if data:
                    await self._on_answer_audio(base64.b64decode(data))
            said = (content.get("outputTranscription") or {}).get("text")
            if said:
                await self._on_answer_text(said)
            if "toolCall" in message:
                await self._on_tool_call(message["toolCall"])
            if "toolCallCancellation" in message:
                self._on_tool_cancellation(message["toolCallCancellation"])
            if "usageMetadata" in message:
                self._on_usage(message["usageMetadata"])
            if content.get("generationComplete") and self.reply is not None:
                self.reply.timer.mark("generation_complete")
            if content.get("turnComplete"):
                await self._on_turn_complete()
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > self.settings.slow_event_warning_ms:
            log.warning("slow Gemini message %s: %.0f ms to handle (waiting for the lock or for the browser)",
                        sorted(message), elapsed_ms)

    # ---------------------------------------------------------------- the caller
    async def _open_caller_turn(self):
        """A caller turn is opened only by the caller's own words (an answer of Gemini never opens one)."""
        self._caller_open = True
        self._caller_text = self._caller_shown = ""
        self._caller_blocked = False
        self._turn_tool_calls = 0
        self._spoke_this_turn = False
        self.memory.turn_no += 1
        if self._bubble_waiting:
            self._bubble_waiting = False  # the page already has the bubbles for these words, above Gemini's answer
        else:
            await self.send_event({"type": "turn", "number": self.memory.turn_no})

    def _close_caller_turn(self):
        self._bubble_waiting = False
        if not self._caller_open:
            return
        self._caller_open = False
        loud, quiet = self._levels
        log.info("turn %d: the caller said %r (microphone: speech level %s, background %s, speech counts from %.0f)",
                 self.memory.turn_no, tidy_numbers(self._caller_shown.strip()),
                 f"{max(loud):.0f}" if loud else "none", f"{sorted(quiet)[len(quiet) // 2]:.0f}" if quiet else "none",
                 self.settings.local_speech_rms)
        self._levels = ([], [])

    async def _on_caller_words(self, text):
        """A piece of Gemini's transcript of the caller."""
        self.last_activity = time.monotonic()
        if self._loud_while_playing is not None:  # the caller really was talking over the speakers
            self._last_loud = max(self._last_loud or 0.0, self._loud_while_playing)
            self._loud_while_playing = None
        if self._playing and self.reply is None and self.settings.barge_in_enabled and re.search(r"[^\W_]", text):
            # Gemini has finished this answer, so it does not interrupt it: the rest is only in our speakers.
            # This comes before the new turn opens, so the page marks the old answer as interrupted.
            log.info("the caller spoke while the end of the answer was still playing: speakers stopped "
                     "(played %.0f of %.0f ms)", self.played_ms, self._queued_ms)
            await self._stop_speakers_nolock()
            await self._set_state("listening")
        if not self._caller_open:
            await self._open_caller_turn()
        self.memory.hear(text)
        self._caller_text += text
        verdict = self.input_rail.check(self._caller_text)
        if verdict.action == "block" and not self._caller_blocked:
            self._caller_blocked = True
            log.warning("turn %d: the input guardrail blocked this turn (%s), so no tool will run for it",
                        self.memory.turn_no, verdict.reason)
        self._caller_shown = verdict.text if verdict.action == "redact" else self._caller_text
        await self.send_event({"type": "user_text", "text": tidy_numbers(self._caller_shown.strip())})

    # ---------------------------------------------------------------- Gemini's answer
    # An answer is "being generated" (self.reply) from its first sound, text or tool call until turnComplete. Then it
    # waits in self._playing until the speakers have played everything (drained) or were stopped. Only then is it
    # known how much of it the caller heard, and only then is a read-back question in it judged.
    async def _answer(self):
        """The answer that is being generated now, started when its first sound, text or tool call arrives."""
        if self.reply is not None:
            return self.reply
        spoke = self._last_loud
        after_speech = spoke is not None and time.perf_counter() - spoke < SPEECH_IS_RECENT_SECONDS
        timer = TurnTimer()
        timer.mark("speech_end", at=spoke if after_speech else None)
        timer.mark("answer_started")
        self.replies += 1
        reply = Reply(self.replies, timer, after_speech)
        reply.offset_ms = self._queued_ms  # sound still waiting in the browser ahead of this answer
        self.reply = reply
        if not self._caller_open and not self._bubble_waiting:
            # The caller's words have not arrived (yet): the page gets its pair of bubbles now, so that words arriving
            # late still appear above this answer.
            self._bubble_waiting = True
            await self.send_event({"type": "turn"})
        await self.send_event({"type": "reply"})
        return reply

    async def _on_answer_audio(self, pcm):
        reply = await self._answer()
        if reply.blocked:
            return  # the output guardrail stopped this answer: none of the rest may reach the speakers
        if reply.audio_bytes == 0:
            reply.timer.mark("first_audio")
            await self._set_state("speaking")
        reply.audio_bytes += len(pcm)
        self._spoke_this_turn = True
        self._queued_ms += len(pcm) / (self.settings.gemini_output_sample_rate * 2) * 1000
        self.last_activity = time.monotonic()
        await self.send_audio(pcm)

    async def _on_answer_text(self, text):
        """A piece of Gemini's transcript of its own answer. Gemini makes this text from its sound, so it comes a little
        after the sound, sometimes even after turnComplete: it then belongs to the answer that is still playing.
        The output guardrail checks it. Gemini makes sound faster than it is played, so the text normally arrives
        before the caller hears the same words."""
        if self.reply is not None:
            reply = self.reply
        elif self._playing:
            reply = self._playing[-1]
        else:
            reply = await self._answer()
        if reply.blocked:
            return
        reply.text += text
        verdict = self.output_rail.check(reply.text[-200:], "", self.memory.achieved, self.memory.known_refs)
        if not verdict.ok and verdict.reason == "unknown_reference":
            # a transcript of spoken digits is not exact enough to stop the voice for; it is logged instead
            if not reply.warned_reference:
                reply.warned_reference = True
                log.warning("reply %d: a booking reference was said that no tool returned (from the transcript, "
                            "which can mishear digits): %r", reply.number, reply.text[-80:])
        elif not verdict.ok:
            await self._block_reply_nolock(reply, verdict)
            return
        await self.send_event({"type": "assistant_text", "text": tidy_numbers(reply.text.strip())})

    async def _block_reply_nolock(self, reply, verdict):
        reply.blocked = True
        reply.replacement = verdict.text
        log.warning("reply %d stopped by the output guardrail (%s); %.0f of %.0f ms of its sound had been played: %r",
                    reply.number, verdict.reason, self.played_ms - reply.offset_ms,
                    reply.audio_ms(self.settings.gemini_output_sample_rate), reply.text[-120:])
        await self._stop_speakers_nolock()  # silence the speakers at once
        if reply is not self.reply:  # it had all arrived already: ask for the replacement now
            self._send_replacement(reply)
            await self._set_state("listening")

    def _send_replacement(self, reply):
        if reply.replacement and self.live is not None:
            log.info("asking Gemini to say instead: %r", reply.replacement)
            self.live.send_note(NOTE.format(why="Your last answer was stopped by the safety filter.", sentence=reply.replacement))

    async def _on_turn_complete(self):
        reply = self.reply
        self._close_caller_turn()
        if reply is None:
            return
        reply.finished = True
        self.reply = None
        log.info("reply %d said: %s", reply.number, tidy_numbers(reply.text.strip()) or "(nothing)")
        repeated = _repeated_sentence(reply.text)
        if repeated:
            log.warning("reply %d: Gemini said the same sentence twice in a row: %r", reply.number, repeated)
        if reply.usage:
            log.info("reply %d tokens: %s", reply.number, reply.usage)
        if reply.blocked:
            self._check_read_back_nolock(reply, played_all=False)
            self._send_replacement(reply)
            await self._set_state("listening")
            return
        if reply.audio_bytes == 0:  # nothing was said, for example Gemini ended its turn right after a tool call
            if not self._playing:
                await self._set_state("listening")
            return
        # all of its sound has arrived; tell the browser no more is coming and wait for it to finish playing
        self._playing.append(reply)
        await self.send_event({"type": "audio_end"})
        self.awaiting_drain = True
        self._cancel_drain_watchdog()
        wait = max(self._queued_ms - self.played_ms, 0) / 1000 + self.settings.drain_grace_seconds
        self._drain_task = asyncio.create_task(self._drain_watchdog(wait))

    async def _on_interrupted(self):
        """Gemini heard the caller start talking while it was still generating, and stopped."""
        reply, self.reply = self.reply, None
        self._close_caller_turn()
        if reply is None:
            if self._playing:  # all of the answer had arrived; only its end was still playing
                log.info("barge-in: speakers stopped (played %.0f of %.0f ms)", self.played_ms, self._queued_ms)
                await self._stop_speakers_nolock()
                await self._set_state("listening")
            return
        reply.finished = True
        log.info("barge-in on reply %d (played %.0f of %.0f ms)", reply.number, self.played_ms - reply.offset_ms,
                 reply.audio_ms(self.settings.gemini_output_sample_rate))
        self._check_read_back_nolock(reply, played_all=False)  # judged before the count of what was played is reset
        await self._stop_speakers_nolock()
        self._send_replacement(reply)
        await self._set_state("listening")

    async def _stop_speakers_nolock(self):
        """Silence the speakers now. Every answer that was still playing is judged by how much of it was played."""
        for reply in self._playing:
            self._check_read_back_nolock(reply, played_all=False)
        self._playing = []
        self.awaiting_drain = False
        self._cancel_drain_watchdog()
        if self.reply is not None:
            self.reply.offset_ms = -self.reply.audio_ms(self.settings.gemini_output_sample_rate)  # its sound so far is gone
        await self.send_event({"type": "clear"})
        self._speakers_emptied()
        await self.send_event({"type": "interrupted"})

    async def _finish_playback_nolock(self):
        """The speakers have played everything they were given."""
        for reply in self._playing:
            self._check_read_back_nolock(reply, played_all=True)
        self._playing = []
        self.awaiting_drain = False
        self._cancel_drain_watchdog()
        self._drained_at = time.perf_counter()
        if self.reply is not None:
            # an answer still being generated plays on, and the browser starts counting again from zero
            self.reply.offset_ms = -self.reply.audio_ms(self.settings.gemini_output_sample_rate)
        self._speakers_emptied()
        if self.reply is None:
            await self._set_state("listening")

    def _speakers_emptied(self):
        """The browser's player was cleared or has played everything: its count starts again from zero."""
        self.played_ms = 0
        self._queued_ms = 0.0

    def _check_read_back_nolock(self, reply, played_all):
        """If this answer read out the confirmation question, decide whether the caller has heard enough of it.
        The answer that reads it is found by its words: a filler and the tool call usually come in an answer before it."""
        pending = self.memory.pending
        if pending is None or pending.heard_at is not None:
            return
        wanted = _content_words(pending.question)
        share = len(wanted & _content_words(reply.text)) / max(len(wanted), 1)
        if share < self.settings.read_back_present_share:
            return  # not the question (a filler, or another sentence): the question is still to come
        if reply.blocked:
            heard, how = False, "the answer was stopped by the output guardrail"
        elif played_all:
            heard = share >= self.settings.read_back_min_share
            how = "it was played to the end" if heard else "it was not read out word for word"
        else:
            heard, how = self._details_heard(reply, pending)
        if heard and _read_back_differs(pending.question, reply.text):
            heard, how = False, "it was read out with a different time, date or day"
        if heard:
            self.memory.question_heard()
            log.info("the caller has heard the confirmation question: %s (%.0f%% of its words were said)", how, share * 100)
        else:
            log.info("the confirmation question will be asked again: %s (%.0f%% of its words were said: %r)",
                     how, share * 100, reply.text.strip()[-120:])
            self.memory.pending = None

    def _details_heard(self, reply, pending):
        """Was the question played at least up to the end of its service, day and time? Then a yes spoken over the rest
        (the name, the phone number, "Shall I book it?") counts. Where the details end in the sound is estimated from
        the spoken length of Gemini's own text."""
        details = _details_part(pending.question)
        wanted = _content_words(details)
        if len(wanted & _content_words(reply.text)) / max(len(wanted), 1) < self.settings.read_back_min_share:
            return False, "it was cut short before its service, day and time were said"
        said = _speaking_units(reply.text)
        before = max(said - _speaking_units(pending.question), 0)  # anything Gemini said before the question itself
        needed_ms = (before + _speaking_units(details)) / max(said, 1) * reply.audio_ms(self.settings.gemini_output_sample_rate)
        played_ms = self.played_ms - reply.offset_ms
        if played_ms >= needed_ms:
            return True, f"talked over after {played_ms:.0f} ms, the service, day and time end at about {needed_ms:.0f} ms"
        return False, f"cut short after {played_ms:.0f} ms, before the service, day and time ended (about {needed_ms:.0f} ms)"

    async def _drain_watchdog(self, wait):
        """Safety net in case the browser never reports that it finished playing."""
        await asyncio.sleep(wait)
        async with self._lock:
            if self._playing and self.awaiting_drain:
                log.warning("the browser never reported the end of playback, finishing anyway")
                await self._finish_playback_nolock()

    def _cancel_drain_watchdog(self):
        task, self._drain_task = self._drain_task, None
        if task is not None and task is not asyncio.current_task():  # the watchdog itself must not cancel itself
            task.cancel()

    # ---------------------------------------------------------------- tools
    async def _on_tool_call(self, tool_call):
        reply = await self._answer()
        calls = tool_call.get("functionCalls") or []
        if not self._spoke_this_turn:
            log.warning("Gemini called %s without saying a filler sentence first", ", ".join(c.get("name", "?") for c in calls))
        reply.timer.mark("tools_started")
        await self._set_state("thinking")
        ctx = ToolContext(emit=self.send_event, turn_no=self.memory.turn_no, blocked=self._caller_blocked)
        for call in calls:
            self._turn_tool_calls += 1
            too_many = self._turn_tool_calls > self.settings.max_tool_rounds
            # every call answers Gemini as soon as it is finished, so one slow tool never holds up another
            self._tool_tasks[call.get("id")] = asyncio.create_task(self._run_tool(call, ctx, reply, too_many))

    async def _run_tool(self, call, ctx, reply, too_many):
        call_id, name = call.get("id"), call.get("name", "")
        try:
            if too_many:
                log.warning("Gemini kept calling tools for the caller's last words; telling it to answer now")
                result = {"ok": False, "error": "TOO_MANY_TOOL_CALLS",
                          "message": "Stop using tools now. Speak to the caller and wait for their answer."}
            else:
                result = await self.runner.run(name, call.get("args") or {}, ctx)
        except asyncio.CancelledError:
            log.info("tool %s was cancelled (the caller interrupted, or the call ended)", name)
            return
        finally:
            self._tool_tasks.pop(call_id, None)
        reply.timer.mark("tools_done")
        if self.live is not None:
            self.live.send_tool_response([{"id": call_id, "name": name, "response": for_model(result)}])

    def _on_tool_cancellation(self, cancellation):
        for call_id in cancellation.get("ids") or []:
            task = self._tool_tasks.get(call_id)
            if task is not None:
                task.cancel()

    def _on_usage(self, usage):
        total = usage.get("totalTokenCount")
        if isinstance(total, int):
            self.tokens_used += total
        if self.reply is not None:
            self.reply.usage = {k: v for k, v in usage.items() if k.endswith("TokenCount")}

    # ================================================================ playback reports from the browser
    def _unreported(self):
        """Answers with sound whose start of playback has not been reported yet, in the order they play."""
        return [r for r in [*self._playing, self.reply] if r is not None and r.audio_bytes and not r.reported]

    async def _on_playback_started(self):
        """The speakers were empty and start playing: that is the first answer not reported yet."""
        waiting = self._unreported()
        if not waiting:
            return
        started = time.perf_counter()
        silence = None
        spoke = waiting[0].timer.marks.get("speech_end")
        if self._drained_at is not None and (spoke is None or spoke < self._drained_at):
            # the caller said nothing in between: this answer follows another one (a result after its filler)
            silence = (started - self._drained_at) * 1000
        await self._report_latency(waiting[0], started, silence)

    async def _on_playback_progress(self, played_ms):
        """An answer queued behind another one (a result that arrived while its filler was still playing) gets no
        "started" from the browser: it starts when the sound ahead of it has been played, with no silence before it."""
        self.played_ms = played_ms
        now = time.perf_counter()
        for reply in self._unreported():
            if 0 < reply.offset_ms <= played_ms:
                await self._report_latency(reply, now - (played_ms - reply.offset_ms) / 1000, 0.0)

    async def _report_latency(self, reply, started, silence_ms):
        reply.reported = True
        self._drained_at = None
        reply.timer.mark("audio_played", at=started)
        metrics = {"type": "metrics", "after_speech": reply.after_speech}
        metrics.update(reply.timer.summary())
        if reply.after_speech:
            metrics["perceived_ms"] = metrics.get("audio_played_ms")
        if silence_ms is not None:
            metrics["silence_before_ms"] = round(silence_ms)  # the gap the caller heard after the answer before it
        log.info("reply %d latency: %s", reply.number, {k: v for k, v in metrics.items() if k != "type"})
        await self.send_event(metrics)
