"""Filler sentences recorded in the Gemini voice, made once by scripts/make_fillers.py.

Gemini is asked to say a filler before every tool call, but it does not always do it. The slow call is the one that
makes a change after the caller's yes: it goes to Google and takes about a second or more. When Gemini makes that
call without saying anything first, the session plays one of these recordings at once, so the caller does not wait
in silence. The recordings live in FILLER_CLIPS_DIR/<voice>/ as 24 kHz 16-bit PCM files with an index.json.
"""
import json
import logging

from .config import PROJECT_ROOT

log = logging.getLogger("fillers")

# which recordings belong to which change tool (the same kinds as TOOL_FILLER_BOOK, _RESCHEDULE and _CANCEL)
KINDS = {"create_booking": "book", "reschedule_booking": "reschedule", "cancel_booking": "cancel"}


def clips_folder(settings):
    return PROJECT_ROOT / settings.filler_clips_dir / settings.gemini_voice


def load_clips(settings):
    """{kind: [(sentence, pcm)]} for the configured voice. Empty when nothing was recorded for this voice."""
    index = clips_folder(settings) / "index.json"
    if not index.exists():
        log.warning("no recorded filler sentences for the voice %s (run python -m scripts.make_fillers)", settings.gemini_voice)
        return {}
    clips = {}
    try:
        for item in json.loads(index.read_text(encoding="utf-8")):
            pcm = (index.parent / item["file"]).read_bytes()
            clips.setdefault(item["kind"], []).append((item["text"], pcm))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("the recorded filler sentences could not be read (%s), so none will be played", exc)
        return {}
    log.info("recorded filler sentences for the voice %s: %s", settings.gemini_voice,
             {kind: len(items) for kind, items in clips.items()})
    return clips
