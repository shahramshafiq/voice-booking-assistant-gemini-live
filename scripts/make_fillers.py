"""Record, in the Gemini voice, the filler sentences that are said while a confirmed change goes to Google.

The session plays one of them when Gemini books, moves or cancels after the caller's yes without saying a filler
first (see app/fillers.py). Run it once from the project folder, and again after changing GEMINI_VOICE or the
TOOL_FILLER_BOOK, TOOL_FILLER_RESCHEDULE or TOOL_FILLER_CANCEL sentences:
    python -m scripts.make_fillers
It asks Gemini to say each sentence (a small cost: a few seconds of speech each) and saves the sound in
FILLER_CLIPS_DIR/<voice>/ with an index.json.
"""
import asyncio
import base64
import json
import re
import sys
import time
from array import array

from app.config import settings
from app.fillers import clips_folder
from app.live import GeminiLive, LiveError

KINDS = ("book", "reschedule", "cancel")
INSTRUCTIONS = ("You are the voice of a friendly receptionist on a phone call. When you get a sentence, say exactly "
                "that sentence in a warm, natural way, and nothing else.")
WAIT_SECONDS = 20
TRIES = 3
QUIET = 300  # 16-bit samples quieter than this before the first word and after the last one are cut off


def words(text):
    return re.findall(r"[a-z]+", text.lower().replace(chr(0x2019), "'").replace("'", ""))


def said_it(sentence, said):
    """Did Gemini say the sentence (its transcript may write "it'll" for "it will")?"""
    wanted, heard = words(sentence), words(said)
    return bool(heard) and len(set(wanted) & set(heard)) >= 0.8 * len(set(wanted)) and len(heard) <= len(wanted) + 2


def trim(pcm, rate):
    """Cut the silence before the first word and after the last one, keeping 50 ms on each side."""
    samples = array("h", pcm[:len(pcm) // 2 * 2])
    loud = [i for i, sample in enumerate(samples) if abs(sample) > QUIET]
    if not loud:
        return b""
    pad = rate // 20
    return samples[max(loud[0] - pad, 0):loud[-1] + pad].tobytes()


async def record(sentence):
    """Gemini says the sentence. Returns its sound and its own transcript of it."""
    messages = asyncio.Queue()

    async def on_message(message):
        await messages.put(message)

    async def on_closed(reason):
        await messages.put({"closed": reason})

    live = GeminiLive(settings, on_message, on_closed)
    await live.connect(INSTRUCTIONS, [])
    try:
        live.send_note(sentence)
        pcm, said = bytearray(), ""
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                message = await asyncio.wait_for(messages.get(), deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            if "closed" in message:
                raise LiveError(message["closed"])
            content = message.get("serverContent") or {}
            for part in (content.get("modelTurn") or {}).get("parts") or []:
                data = (part.get("inlineData") or {}).get("data")
                if data:
                    pcm += base64.b64decode(data)
            said += (content.get("outputTranscription") or {}).get("text", "")
            if content.get("turnComplete"):
                break
        return bytes(pcm), said.strip()
    finally:
        await live.close()


async def main():
    folder = clips_folder(settings)
    folder.mkdir(parents=True, exist_ok=True)
    rate = settings.gemini_output_sample_rate
    index = []
    for kind in KINDS:
        for number, sentence in enumerate(settings.tool_fillers[kind], 1):
            for _ in range(TRIES):
                pcm, said = await record(sentence)
                if pcm and said_it(sentence, said):
                    break
                print(f"  Gemini said {said!r} instead of {sentence!r}, trying again")
            else:
                print(f"SKIPPED: {sentence!r}")
                continue
            pcm = trim(pcm, rate)
            name = f"{kind}_{number}.pcm"
            (folder / name).write_bytes(pcm)
            index.append({"kind": kind, "text": sentence, "file": name})
            print(f"OK: {kind} {sentence!r} ({len(pcm) / (rate * 2):.1f} s)")
    for old in folder.glob("*.pcm"):  # recordings of sentences that are no longer in the settings
        if old.name not in {item["file"] for item in index}:
            old.unlink()
    (folder / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(index)} recordings for the voice {settings.gemini_voice} in {folder}")
    return bool(index)


if __name__ == "__main__":
    try:
        ok = asyncio.run(main())
    except LiveError as exc:
        print(f"FAILED: {exc}")
        ok = False
    sys.exit(0 if ok else 1)
