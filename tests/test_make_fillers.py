"""Tests for the helpers of the filler recording script (the recording itself needs the real Gemini)."""
from array import array

from scripts.make_fillers import said_it, trim


def test_a_recording_is_kept_only_when_gemini_said_the_sentence():
    assert said_it("Great, I'm booking that for you now, it will just take a moment.",
                   "Great, I'm booking that for you now, it'll just take a moment.")
    assert not said_it("Great, I'm booking that for you now.", "Sure! What would you like to book?")
    assert not said_it("Great, I'm booking that for you now.", "")


def test_the_silence_around_the_words_is_cut_off():
    rate = 24000
    silence, speech = [0] * rate, [5000] * rate  # 1 s of each
    pcm = array("h", silence + speech + silence).tobytes()
    kept = len(trim(pcm, rate)) // 2
    assert rate <= kept <= rate + rate // 10  # the speech and 50 ms on each side
    assert trim(array("h", silence).tobytes(), rate) == b""
