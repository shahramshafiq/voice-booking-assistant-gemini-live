<div align="center">

# Voice Booking Assistant (Gemini Live)

A voice receptionist that books, looks up, moves and cancels appointments in Google Calendar, built on the Gemini Live API for low latency.

![Python](https://img.shields.io/badge/Python-3.14-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Gemini Live](https://img.shields.io/badge/Gemini%20Live-gemini--3.8--live-8E75B2?style=flat-square&logo=googlegemini&logoColor=white)
![Google Calendar](https://img.shields.io/badge/Google%20Calendar-API%20v3-4285F4?style=flat-square&logo=googlecalendar&logoColor=white)
![Tests](https://img.shields.io/badge/tests-141%20passing-brightgreen?style=flat-square)

</div>

## Overview

You open a web page, press Start and talk. The assistant hears you, decides what you need, checks one Google Calendar through a set of tools, offers free times, and only changes the calendar after you say yes to a read-back of the exact details.

This is the Gemini Live version of [voice-booking-assistant](https://github.com/shahramshafiq/voice-booking-assistant). That project chained three streaming services (speech to text, a language model, text to speech). Here one Gemini Live WebSocket hears the audio, decides when you have finished, calls the tools and speaks the answer, so there are no hand-offs between services. The booking rules, the confirmation step and the guardrails stay in the backend code.

## How It Works

```text
browser microphone (16 kHz PCM)                         Google Calendar API
        |                                                        ^
        v                                                        |
  FastAPI WebSocket  --audio-->  Gemini Live  --tool call-->  tool runner
  (VoiceSession)    <--audio--  (one socket)  <--result----  (rules, confirmation gate)
        |
        v
browser speakers (24 kHz PCM), transcripts, tool list, latency table
```

1. The page connects to the server when it loads, and the server opens a Gemini Live session with the instructions and the eight tools, so pressing Start has nothing left to wait for.
2. Microphone audio goes to Gemini in 40 ms pieces. Gemini detects the end of your turn itself.
3. When Gemini asks for a tool, the backend checks the input, runs the booking rules against the calendar and answers with the result.
4. Gemini's sound is passed to the browser as it arrives. Its transcript of its own words is checked by the output guardrail on the way.

### What a tool call sounds like

Measured against the real Gemini Live API (`gemini-3.8-live`):

1. You stop talking. Gemini waits for 700 ms of silence (`LIVE_SILENCE_DURATION_MS`), then starts a short filler sentence ("Let me check the calendar for you.") and sends the tool call almost at the same time.
2. The tool runs while the filler is being heard.
3. Gemini holds its next sentence until the filler has been heard, then starts it a moment later (0.1 to 0.9 s in our runs), if the tool has answered by then.

So a filler should last about as long as its tool plus Gemini's own 0.7 s: a longer one only makes the answer come later, a shorter one leaves a silence. Searches, lookups and the first call of a change read the copy of the calendar kept in memory (well under 0.1 s), so their fillers take about 1 s ("Let me check."). After your yes, the change goes to Google (1.1 to 1.6 s in a live test), so those fillers take about 2 s ("Great, I'm booking that for you now.").

Every tool result is sent with `scheduling: WHEN_IDLE` and, when it worked, a short "give the caller the answer now" nudge. Without them, `gemini-3.8-live` sometimes held a finished result back until the caller spoke again (once for 13 s in a live test). Gemini does not always say the filler: when it makes a confirmed change without one, the server plays a filler recorded in the same voice (`python -m scripts.make_fillers`), so the line is never silent while the calendar is written.

## Key Features

- **Eight booking tools:** services, business config, calendar availability, slot search, create, look up, reschedule and cancel. All rules (opening hours, breaks, buffers, notice, durations, cancellation policy) live in backend code and `.env`, never in the model.
- **Confirm before any change:** the first call to a change tool only parks the action and returns a read-back question written by the code. Gemini reads it out. The change happens only when the caller says yes in their answer after hearing that question. A yes spoken over the name, the phone number or "Shall I book it?" counts once the service, day and time were heard. A yes from any other turn, a question cut short before its day and time, or a read-back that Gemini did not say (or said with a different time, date or day) means the question is asked again. A tiny turn that is only background sound ("A", "es") does not use up the caller's answer.
- **One connection for speech, thinking and voice:** no speech to text, model and voice hand-offs. Gemini is connected and set up before Start, the next weeks of the calendar are kept in memory, every tool answers Gemini as soon as it is finished, and a confirmed reschedule reads the booking and the new day from Google at the same time.
- **Filler sentences:** set per kind of tool call in `.env.example`, sized to how long each tool takes, with a recorded fallback for confirmed changes. The log warns when Gemini calls a tool without a filler or says a sentence twice in a row.
- **Varied answers:** each result sentence (booked, moved, cancelled) has several wordings, picked at random and always filled from the real calendar result.
- **Barge-in:** Gemini stops when you talk over it. Gemini makes sound faster than it is played, so when you talk over the end of an answer that is still playing, the server stops the speakers itself.
- **Safe against retries and races:** a repeated request returns the existing booking, a lost reply is checked before reporting failure, a reschedule updates the same calendar event, and two callers cannot take one slot.
- **Identity checks:** lookup, reschedule and cancel need the booking reference and the phone number, and wrong tries are limited per call and per phone number.
- **Input and output guardrails:** the input guardrail reads Gemini's transcript of the caller. In a turn that tries to override the rules, no tool runs at all, and card numbers are kept off the page and out of the logs. The output guardrail reads Gemini's transcript of its own answer and stops secrets, instruction leaks and "booked it" claims that no tool confirmed. A stopped answer is silenced and Gemini is told to say a fixed sentence instead.
- **Fresh memory per call:** every conversation gets its own Gemini session and an empty short-term memory. Long calls continue on a new connection with Gemini's session resumption when the old one is about to close.
- **Latency table:** each answer is timed from your last word to the first sound you hear, with the stages in between. For an answer that follows another one (a result after its filler) the silence between the two is shown too.

## Tech Stack

| Area | Technology |
|---|---|
| Language | Python |
| Backend | FastAPI, Uvicorn, WebSockets |
| Speech in, thinking, speech out | Gemini Live API (`gemini-3.8-live`) over a raw WebSocket |
| Calendar | Google Calendar API v3 over httpx |
| Validation | Pydantic |
| Browser audio | AudioWorklet (microphone and player) |
| Testing | pytest, a local fake Gemini Live server and a fake Google Calendar |

## Project Structure

```text
voice-booking-assistant-gemini-live/
  app/
    main.py              FastAPI app, page, health check and the voice WebSocket
    config.py            Reads and validates every setting from .env
    live.py              Gemini Live client: setup, audio, tool answers, reconnect
    session.py           One browser call: Gemini messages, tools, barge-in, guardrails, latency
    prompt.py            Builds the instructions for each conversation
    tools.py             Tool list, input checks and the confirmation gate
    memory.py            Short-term memory for one call
    fillers.py           Loads the recorded filler sentences
    timing.py            Per-answer latency stopwatch
    booking/             Scheduling rules and the eight tools
    guardrails/          Input and output guardrails
  data/services.json     The services offered
  data/fillers/          Filler sentences recorded in the Gemini voice (one folder per voice)
  prompts/               System prompt template
  scripts/               check_calendar.py, check_gemini.py and make_fillers.py
  static/                Web page, audio worklets, styles
  tests/                 141 tests with a fake Gemini server and a fake calendar
```

## Getting Started

### Prerequisites

- Python 3.14
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/)
- A Google Calendar (a separate test calendar is recommended) and its Calendar ID
- A modern desktop browser and a microphone (headphones recommended: without them the microphone hears the assistant)

### Installation

```bash
git clone https://github.com/shahramshafiq/voice-booking-assistant-gemini-live.git
cd voice-booking-assistant-gemini-live
py -3.14 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file with your three private values:

```
GEMINI_API_KEY=
GOOGLE_CALENDAR_API_TOKEN=
GOOGLE_CALENDAR_ID=
```

That is all `.env` needs. Every other setting is read from `.env.example`, where each one is explained in a comment. A value you put in `.env` wins over the one in `.env.example`, so copy a line into `.env` only when you want to change it.

### Environment Variables

The settings you are most likely to change (all names and defaults are in `.env.example`):

| Setting | Default | What it does |
|---|---|---|
| `GEMINI_API_KEY` | (yours) | Gemini API key |
| `GEMINI_LIVE_MODEL` | `gemini-3.8-live` | The Live model |
| `GEMINI_VOICE` | `Kore` | A prebuilt Gemini voice (record the fillers again after changing it) |
| `LIVE_SILENCE_DURATION_MS` | `700` | How long you must be quiet before your turn ends (lower answers sooner, higher cuts you off less) |
| `LIVE_PREFIX_PADDING_MS` | empty | How long speech must last before Gemini counts it as the start of a turn (higher ignores short sounds) |
| `LIVE_START_SENSITIVITY` / `LIVE_END_SENSITIVITY` | empty | `START_SENSITIVITY_LOW` gives fewer false turns from noise; `END_SENSITIVITY_LOW` gives fewer cut-offs in a pause; empty uses Gemini's default |
| `LIVE_TRANSCRIPT_WAIT_MS` | `800` | How long a change waits for Gemini's text of your yes |
| `TOOL_FILLER_SEARCH`, `_LOOKUP`, `_PREPARE`, `_BOOK`, `_RESCHEDULE`, `_CANCEL` | see `.env.example` | Filler sentences per kind of tool call, separated with `\|` |
| `FILLER_CLIPS_DIR` | `data/fillers` | Where the recorded fillers are kept (one folder per voice) |
| `TOOL_RESPONSE_SCHEDULING` | `WHEN_IDLE` | When Gemini should speak about a tool result: `WHEN_IDLE`, `INTERRUPT`, `SILENT`, or empty to send nothing |
| `READ_BACK_PRESENT_SHARE` / `READ_BACK_MIN_SHARE` | `0.3` / `0.6` | Share of the question's words that marks an answer as the read-back, and that must be said for it to count |
| `ANSWER_MIN_LETTERS` | `4` | A turn after the question with fewer letters and no yes or no word is treated as background sound |
| `REPLY_BOOKED`, `REPLY_RESCHEDULED`, `REPLY_CANCELLED` | see `.env.example` | Result sentences, several wordings separated with `\|`, with `{service}`, `{when}` and `{reference}` |
| `GOOGLE_CALENDAR_ID` | (yours) | The one calendar the assistant books into |
| `GOOGLE_CALENDAR_API_TOKEN` | (yours) | Manually pasted access token, about one hour long |
| `BUSINESS_NAME` / `BUSINESS_STAFF_NAME` | `TrustCare` / `Shahram Shafiq` | The business and the one staff member all appointments are with |
| `BUSINESS_TIMEZONE` | `Asia/Karachi` | Time zone for every date and time |
| `BUSINESS_HOURS` / `BUSINESS_BREAKS` | `mon-fri 09:00-17:00;sat 10:00-14:00` / `13:00-14:00` | Opening hours and the daily break |
| `CANCELLATION_NOTICE_HOURS` | `24` | Later than this, the caller must contact the staff member |
| `MAX_VERIFY_FAILURES` / `VERIFY_LOCK_SECONDS` | `3` / `600` | Wrong reference or phone tries before a phone number is locked |
| `CONFIRM_TTL_SECONDS` | `180` | How long a confirmation question stays valid |
| `BARGE_IN_ENABLED` | `true` | Whether you can interrupt the assistant (set `false` without headphones) |
| `IDLE_TIMEOUT_SECONDS` | `180` | Stops the call after this long without speech |

### Getting a Google Calendar token

The app uses a manually pasted access token (no login flow). It lasts about one hour.

1. Open the [Google OAuth 2.0 Playground](https://developers.google.com/oauthplayground/).
2. In "Input your own scopes" enter `https://www.googleapis.com/auth/calendar.events` and click Authorize APIs.
3. Sign in with the account that owns the calendar and allow access.
4. Click "Exchange authorization code for tokens" and copy the access token.
5. Paste it after `GOOGLE_CALENDAR_API_TOKEN=` in `.env` and restart the server.

### Checking the setup

```bash
python -m scripts.check_calendar
python -m scripts.check_calendar --write
python -m scripts.check_gemini
python -m scripts.check_gemini --speak --tool
```

`check_calendar` checks the token and the calendar (free). `check_gemini` opens the real Gemini setup with the instructions, the eight tools and the voice, which generates nothing. With `--speak` it times one short spoken sentence, and with `--tool` it asks a question that needs a tool and answers the tool with made-up times (the calendar is not used).

The recorded fillers for the `Kore` voice are included. After changing `GEMINI_VOICE` or the book, reschedule or cancel fillers, record them again (a small cost, a few seconds of speech each):

```bash
python -m scripts.make_fillers
```

### Run

```bash
python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000, press Start conversation and allow the microphone.

## Usage

Try a full conversation:

1. "I'd like to book a consultation tomorrow afternoon."
2. Pick one of the offered times, then give your name and phone number.
3. The assistant reads back the details and asks to confirm. Say "yes".
4. It books the slot and says a booking reference. Use the reference and the same phone number to look up, move or cancel the booking.

The page shows both transcripts, the tools that were used and a latency table for every answer.

### Example conversations

Both were recorded on 27 October 2026 with the real Gemini Live API (`gemini-3.8-live`, voice `Kore`) and an in-memory test calendar, with the longer filler sentences used that day (they have since been shortened). Tool calls are shown in brackets. Numbers that were read one digit at a time are shown as Gemini's transcript wrote them.

**Book, move and cancel** (the caller's lines were typed into the session):

> **Caller:** Hi, I'd like to book a consultation on Thursday afternoon.<br>
> **Assistant:** Let me check the calendar for you. [find_available_slots] We have openings on Thursday at 2 PM, 2:30 PM, and 3 PM. Which time works best for you?<br>
> **Caller:** The first time please. My name is Ali Raza and my number is 0300 1234567.<br>
> **Assistant:** Alright, one moment. [create_booking: confirmation required] Just to confirm: a Consultation Thursday 29 October at 2 PM, for Ali Raza, phone number 0 3 0 0, 1 2 3, 4 5 6 7. Shall I book it?<br>
> **Caller:** Yes, go ahead.<br>
> **Assistant:** Great, I'm booking that for you now, it will just take a moment. [create_booking: ok] That's confirmed: your Consultation for Thursday 29 October at 2 PM. Please keep your booking reference, 0 1 3, 4 0 2. Is there anything else you need?<br>
> **Caller:** Thanks. Actually, can you move that booking to Friday at 3 PM? The reference is 0 1 3 4 0 2 and my number is 0300 1234567.<br>
> **Assistant:** Let me check the calendar for you. [find_available_slots] Alright, one moment. [reschedule_booking: confirmation required] Just to confirm: you want to move your Consultation Thursday 29 October at 2 PM to Friday 30 October at 3 PM. Shall I go ahead?<br>
> **Caller:** Yes, please.<br>
> **Assistant:** Great, I'm moving that for you now, it will just take a moment. [reschedule_booking: ok] Your Consultation has been moved to Friday 30 October at 3 PM. Your booking reference is still 0 1 3, 4 0 2. Is there anything else I can help with?<br>
> **Caller:** Sorry, I've changed my mind. Please cancel it.<br>
> **Assistant:** Alright, let me cancel that appointment for you right now. [cancel_booking: confirmation required] Just to confirm: you want to cancel your Consultation Friday 30 October at 3 PM. Is that right?<br>
> **Caller:** Yes.<br>
> **Assistant:** Okay, I'm cancelling that for you now, just a moment. [cancel_booking: ok] Your Consultation for Friday 30 October at 3 PM has been cancelled. Is there anything else I can help with?

**A spoken booking** (the caller's lines were spoken and sent as microphone audio in real time):

> **Caller:** Hi, I'd like to book a consultation on Thursday at eleven in the morning.<br>
> **Assistant:** Let me check the calendar for you. [find_available_slots] That time is available. May I please have your name and phone number?<br>
> **Caller:** My name is Ali Raza, and my number is zero three zero zero, one two three four five six seven.<br>
> **Assistant:** Alright, one moment. [create_booking: confirmation required] Just to confirm: a Consultation Thursday 29 October at 11 AM, for Ali Raza, phone number 0300 1234567. Shall I book it?<br>
> **Caller:** Yes, go ahead.<br>
> **Assistant:** Great, I'm booking that for you now, it will just take a moment. [create_booking: ok] Your Consultation is booked for Thursday 29 October at 11 AM, and your reference number is 348959. Anything else I can help you with?

## Latency

How it is measured: the end of your speech is estimated from the microphone loudness (Gemini does not report when it decided you had finished), and only while the speakers are quiet, so their own sound coming back into the microphone does not count. Each answer is timed to the moment the browser starts playing it.

Measured in the spoken booking above (real Gemini audio, in-memory calendar, so the calendar writes took a few milliseconds instead of the 1 to 1.5 s of Google):

| Moment | From the end of the caller's speech |
|---|---|
| Filler starts after "I'd like to book ..." | 1.9 s |
| Answer with the free time starts | 4.3 s (straight after the filler, 0 s of silence) |
| Filler starts after the name and phone number | 3.3 s |
| Read-back question starts | 5.4 s (0.23 s of silence after the filler) |
| Filler starts after "Yes, go ahead." | 1.4 s |
| Booking result starts | 5.6 s (0.06 s of silence after the filler) |

A later spoken run on 28 October, with the shorter fillers and `WHEN_IDLE` (in-memory calendar):

| Moment | From the end of the caller's speech |
|---|---|
| Lookup: filler "Let me find that." starts | 1.8 s |
| Lookup: the booking is read out | 3.4 s (0.1 s of silence after the filler) |
| Booking after "Yes, go ahead.": filler starts | 1.3 s |
| Booking result starts | 4.3 s (straight after the filler) |

In the live test before these changes, the same lookup answer came about 13 s after the filler, and a slot search answer after 3.6 s of silence.

Other measurements:

- Opening a Gemini session (done when the page loads, not at Start): 1.1 to 1.7 s.
- `check_gemini --speak`: first sound 1.1 s after the question was sent. `check_gemini --tool`: tool call after 1.0 s, first sound 0.8 s after the tool answer.
- In typed runs, the silence between a filler and the next sentence was 0.3 to 0.9 s when the tool answered in time. Once, after Gemini picked a filler meant for after the yes ("let me cancel that appointment for you right now") before the question, it paused about 6 s before reading the question; the prompt now names the first call and the call after the yes separately.
- Early live tests had up to 9.4 s of silence after "I'm booking that for you now", before the result sentence was given to Gemini inside the tool result with a nudge to read it at once.

## Testing

```bash
python -m pytest
```

The 141 tests need no keys and cost nothing. A local fake Gemini Live server speaks the same JSON protocol as the real one (binary frames, setup, transcripts, tool calls, interruptions, goAway and session resumption), and a fake Google Calendar answers the calendar requests. The tests cover date parsing, slot maths, the booking rules, the confirmation gate, the guardrails, the Gemini client, the latency measurements, the recorded fillers and whole conversations.

## Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| "Calendar problem: The Google Calendar token is expired or invalid." | The pasted token lasts about one hour | Get a new token, paste it in `.env`, restart |
| "Could not connect to Gemini" | Wrong key, no network or no quota | Run `python -m scripts.check_gemini`: it prints Gemini's own reason |
| The assistant keeps stopping in the middle | The microphone hears the speakers | Use headphones, or set `BARGE_IN_ENABLED=false` |
| You are cut off in a pause | The end of turn comes after 700 ms of silence | Say each request in one breath, or try `LIVE_END_SENSITIVITY=END_SENSITIVITY_LOW` |
| Tiny turns like "A" or "es" appear | Background sound was taken as speech | Try `LIVE_START_SENSITIVITY=START_SENSITIVITY_LOW` or `LIVE_PREFIX_PADDING_MS=150` |
| Log: "Gemini called ... without saying a filler sentence first" | Gemini skipped the filler | For a confirmed change the recorded filler is played; for other tools the answer simply comes without one |

## Limitations and Roadmap

- Gemini hears the audio itself, so the input guardrail cannot hide a request from the model. It stops every tool in that turn instead, and the output guardrail checks what is said.
- The fillers and the way the result follows them are asked for in the prompt, which Gemini usually but not always follows. The recorded filler covers only confirmed changes, because a filler before a quick tool would only make its answer come later.
- Gemini reads the confirmation question out itself, so its transcript is compared with the code's question. A paraphrase that drops too many of its words is asked again.
- The output guardrail works on Gemini's transcript, which usually arrives before the same words are played, but not always.
- The end of your speech in the latency table is estimated from the microphone loudness, because Gemini does not report when it decided you had finished.
- The Google token is pasted by hand and expires after about an hour. One staff member and one calendar. Locks and the race guard work inside one server process.
- How quickly Gemini starts speaking after you stop varies from call to call (about 1.1 to 3.5 s in our runs; once 4.9 s). Its own reasoning cannot be turned down on `gemini-3.8-live` (`thinkingLevel` is not supported there).
- `gemini-3.1-flash-live-preview` was tried as a faster model: it accepted the setup and the tools, but it offered times before searching, wrote its own confirmation question and said "your appointment has been cancelled" before the tool had answered, so it is not used.
- The voice is one of Gemini's prebuilt voices. The app runs in the browser only, with no phone line.

## Author

**Shahram Shafiq** - [GitHub](https://github.com/shahramshafiq) | [LinkedIn](https://www.linkedin.com/in/shahramshafiq/)
