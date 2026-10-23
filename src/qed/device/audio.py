"""Ears and voice for the headless device, all free and offline by default.

- Listening: microphone via `sounddevice`, a simple energy-based voice activity
  detector (records from when you start talking until a short silence), and
  speech-to-text with Whisper running locally (`faster-whisper`, base.en).
- Speaking: the operating system's voices via `pyttsx3` (SAPI on Windows).

Both sides are small classes so a trained voice agent, Amazon Polly or Amazon
Transcribe can replace them without touching the conversation logic.
"""

from __future__ import annotations

import queue
import sys
import time
from typing import Protocol

import numpy as np

RATE = 16_000
BLOCK = 480  # 30 ms
# Words the recognizer should expect, so city names come out right.
HINT = (
    "Trip planning. Seattle, San Francisco, Los Angeles, San Diego, Portland, Denver, Chicago, Austin, New York, "
    "Boston, Miami, Nashville, Las Vegas, Phoenix. Grandma, Maya. Book it. The first one. No AC. Breakfast included."
)


_SLIPS = [
    (r"^\s*plant\b", "plan"),
    (r"^\s*playing\s+a\s+trip\b", "plan a trip"),
    (r"\bno a\.?\s?c\b", "no AC"),
    (r"\bmy ami\b", "Miami"),
    (r"\bbook at\b", "book it"),
]


def clean_transcript(text: str) -> str:
    """Fix the few mis-hearings that matter for trip planning, and drop a trailing period."""
    import re

    for pattern, fix in _SLIPS:
        text = re.sub(pattern, fix, text, flags=re.IGNORECASE)
    return text.strip().rstrip(".")


class Ears(Protocol):
    def listen(self) -> str: ...


class Voice(Protocol):
    def say(self, text: str) -> None: ...


class MicEars:
    def __init__(
        self,
        model: str = "base.en",
        max_seconds: float = 15.0,
        silence_s: float = 0.9,
        push_to_talk: bool = True,
    ) -> None:
        import sounddevice  # noqa: F401 - fail early if audio is unavailable
        from faster_whisper import WhisperModel

        self.push_to_talk = push_to_talk  # like a wake word: background talk is never taken as a request
        self.model = WhisperModel(model, device="cpu", compute_type="int8")
        self.max_seconds, self.silence_s = max_seconds, silence_s
        self.floor = self._calibrate()

    def _calibrate(self) -> float:
        import sounddevice as sd

        rec = sd.rec(int(0.6 * RATE), samplerate=RATE, channels=1, dtype="float32")
        sd.wait()
        return max(0.004, float(np.sqrt(np.mean(rec**2))))

    def record(self) -> np.ndarray:
        import sounddevice as sd

        q: queue.Queue[np.ndarray] = queue.Queue()
        stream = sd.InputStream(
            samplerate=RATE,
            channels=1,
            dtype="float32",
            blocksize=BLOCK,
            callback=lambda data, *_: q.put(data.copy()),
        )
        threshold = max(self.floor * 3.0, 0.012)
        chunks: list[np.ndarray] = []
        started, quiet, t0 = False, 0.0, time.monotonic()
        with stream:
            while time.monotonic() - t0 < self.max_seconds + 8:
                block = q.get()
                loud = float(np.sqrt(np.mean(block**2))) > threshold
                if not started:
                    if loud:
                        started, t0 = True, time.monotonic()
                        chunks.append(block)
                    elif time.monotonic() - t0 > 8:  # nobody spoke
                        return np.zeros(0, dtype="float32")
                    continue
                chunks.append(block)
                quiet = 0.0 if loud else quiet + BLOCK / RATE
                if quiet >= self.silence_s or time.monotonic() - t0 > self.max_seconds:
                    break
        return np.concatenate(chunks)[:, 0] if chunks else np.zeros(0, dtype="float32")

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size < RATE // 4:
            return ""
        segments, _ = self.model.transcribe(
            audio, language="en", beam_size=1, initial_prompt=HINT, condition_on_previous_text=False
        )
        # Drop what Whisper itself thinks is noise or guesswork (TV, background talk).
        kept = [s for s in segments if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0]
        text = " ".join(s.text.strip() for s in kept).strip()
        return clean_transcript(text)

    def listen(self) -> str:
        if self.push_to_talk:
            try:
                input("  [press Enter, then speak]")
            except EOFError:
                return "goodbye"
        print("  [listening…]", flush=True)
        return self.transcribe(self.record())


class TextEars:
    """Typed input, for testing without a microphone."""

    def listen(self) -> str:
        try:
            return input("you> ").strip()
        except EOFError:
            return "goodbye"


class SystemVoice:
    """OS voices on one dedicated thread: Windows speech (SAPI/COM) must be created and used on the same thread."""

    def __init__(self, rate: int = 185) -> None:
        import threading

        self.rate = rate
        self._jobs: queue.Queue[tuple[str, threading.Event]] = queue.Queue()
        self._ready = threading.Event()
        threading.Thread(target=self._loop, name="qed-voice", daemon=True).start()
        self._ready.wait(10)

    def _loop(self) -> None:
        try:
            import pythoncom  # type: ignore[import-not-found]

            pythoncom.CoInitialize()
        except ImportError:
            pass
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", self.rate)
        female = next(
            (
                v
                for v in engine.getProperty("voices")
                if "zira" in v.name.lower() or "female" in v.name.lower()
            ),
            None,
        )
        if female is not None:
            engine.setProperty("voice", female.id)
        self._ready.set()
        while True:
            text, done = self._jobs.get()
            try:
                engine.say(text)
                engine.runAndWait()
            finally:
                done.set()

    def say(self, text: str) -> None:
        import threading

        print(f"Plexis> {text}", flush=True)
        done = threading.Event()
        self._jobs.put((text.replace("$", "").replace(" | ", ". "), done))
        done.wait(60)


class PrintVoice:
    def say(self, text: str) -> None:
        print(f"Plexis> {text}", flush=True)
        sys.stdout.flush()
