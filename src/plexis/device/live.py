"""Hands-free Plexis voice, built on Pipecat. Talk to it like a person.

    mic -> Silero VAD -> Whisper (local) -> turn-taking -> Plexis (MCP) -> Kokoro (local) -> speaker

What makes it feel like a conversation rather than a walkie-talkie:

- Pauses. Smart Turn v3, an end-of-turn model bundled with Pipecat and run
  locally, listens to how a sentence ends, so "I'd like to go to... Seattle"
  stays one turn. A sentence that stops on "to", "and" or "um" also holds the
  turn open. When you sound finished, Plexis answers right away.
- Interruptions. Talk over Plexis and it stops mid-sentence and listens. With
  speakers, it ignores its own voice coming back through the microphone; with
  headphones (--headphones) it stops the instant you start talking.
- Follow-ups. After Plexis speaks you can just answer. After a quiet spell it
  waits for its name ("Plexis, ...") so a TV in the room isn't taken as a request.
- Voice. Kokoro, a natural-sounding neural voice that runs on this PC (no internet,
  no per-word delay). `--voice-engine edge` uses Microsoft's online voices instead,
  falling back to the system voice when the connection is slow.

The conversation itself (routing, confirmations, alerts) is the same Device
used by the push-to-talk mode; only the ears and the mouth are replaced.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess  # nosec B404
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    SystemFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.workers.runner import WorkerRunner

from plexis.device.audio import HINT, SystemVoice, clean_transcript
from plexis.device.device import Device

IN_RATE, OUT_RATE = 16_000, 24_000
VOICE = "en-US-AvaMultilingualNeural"  # edge-tts
KOKORO_VOICE = "af_heart"  # warm US English; others: af_bella, af_nicole, am_michael, bf_emma
KOKORO_DIR = Path.home() / ".cache" / "pipecat" / "kokoro-onnx"
NEURAL_TIMEOUT_S = 2.0  # slower than this and the conversation feels broken
SEGMENT_S = 0.5  # silence that closes a speech segment (Whisper transcribes each one)
HOLD_S = 0.3  # extra wait once a turn sounds finished
MAX_PAUSE_S = 2.2  # longest mid-sentence pause Plexis waits through
FOLLOW_UP_S = 45.0  # how long after speaking Plexis takes a reply without its name
DANGLING = re.compile(
    r"(?:\b(?:to|from|and|or|but|the|a|an|in|on|at|for|with|of|my|our|is|are|so|then|like|about|maybe"
    r"|um+|uh+|er+)|,|\.\.\.|…)\s*$",
    re.IGNORECASE,
)
WAKE = re.compile(r"^\W*(?:hey\s+|ok(?:ay)?\s+)?(?:plexis|plexus|plexes|lexis|lexus|alexa)\b[\s,.!?]*", re.I)
WORDS = re.compile(r"[a-z']+")
SENTENCES = re.compile(r"(?<=[.!?])\s+")
BARGE_IN = re.compile(
    r"^\W*(?:stop|wait|hold on|hang on|pause|cancel|no+|nope|quiet|shut up|enough|excuse me|sorry|plexis)\b",
    re.IGNORECASE,
)


@dataclass
class Heard(SystemFrame):
    """What Whisper made of one speech segment ("" if nothing usable). A system frame, so an
    interruption can't drop it; otherwise the turn would wait for a transcript that never comes."""

    text: str = ""


class Whisper(WhisperSTTService):
    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        text = []
        async for frame in super().run_stt(audio):
            if isinstance(frame, TranscriptionFrame):
                text.append(frame.text)
            else:
                yield frame
        yield Heard(text=" ".join(text))


class NeuralVoice(TTSService):
    """Microsoft neural voices via edge-tts, decoded to PCM with FFmpeg."""

    def __init__(self, voice: str = VOICE, rate: str = "+8%", **kwargs: object) -> None:
        import imageio_ffmpeg

        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            # Long enough to cover the neural voice timing out and the offline voice answering,
            # and never give up on the voice: the offline fallback always produces speech.
            stop_frame_timeout_s=NEURAL_TIMEOUT_S + 4.0,
            max_consecutive_zero_audio_contexts=0,
            sample_rate=OUT_RATE,
            settings=TTSSettings(model=None, voice=voice, language=None),
            **kwargs,  # type: ignore[arg-type]
        )
        self.voice_name, self.rate = voice, rate
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        self.offline_until = 0.0  # after a failure, use the offline voice for a while
        self._system: SystemVoice | None = None

    def can_generate_metrics(self) -> bool:
        return True

    def _decode(self, audio: bytes) -> bytes:
        """MP3 or WAV in, 16-bit mono PCM at OUT_RATE out."""
        args = ["-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1"]
        # Runs only the FFmpeg bundled with imageio-ffmpeg, with fixed arguments and no shell.
        out = subprocess.run(  # noqa: S603  # nosec B603
            [self.ffmpeg, *args, "-ar", str(OUT_RATE), "pipe:1"],
            input=audio,
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout

    async def _neural(self, sentence: str) -> bytes:
        import edge_tts

        mp3 = bytearray()
        async for chunk in edge_tts.Communicate(sentence, self.voice_name, rate=self.rate).stream():
            if chunk["type"] == "audio":
                mp3 += chunk["data"]
        if not mp3:
            raise RuntimeError("no audio from the neural voice")
        return bytes(mp3)

    def _offline(self, sentence: str) -> bytes:
        import tempfile

        self._system = self._system or SystemVoice(rate=190)
        with tempfile.TemporaryDirectory() as tmp:
            wav = str(Path(tmp) / "say.wav")
            self._system.to_wav(sentence, wav)
            return Path(wav).read_bytes()

    async def _synthesize(self, sentence: str) -> bytes:
        """The neural voice when it answers quickly; otherwise this PC's own voice, so Plexis never goes silent."""
        if time.monotonic() >= self.offline_until:
            try:
                audio = await asyncio.wait_for(self._neural(sentence), timeout=NEURAL_TIMEOUT_S)
                return await asyncio.to_thread(self._decode, audio)
            except Exception as exc:
                print(
                    f"  (neural voice unavailable: {type(exc).__name__}; using this PC's voice for a minute)"
                )
                self.offline_until = time.monotonic() + 60
        return await asyncio.to_thread(lambda: self._decode(self._offline(sentence)))

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        # All sentences are synthesized at once and played in order, so speech
        # starts after the first sentence is ready rather than the whole reply.
        sentences = [s for s in SENTENCES.split(text) if s.strip()]
        jobs = [asyncio.ensure_future(self._synthesize(s)) for s in sentences]

        async def in_order() -> AsyncIterator[bytes]:
            for job in jobs:
                yield await job

        try:
            await self.start_tts_usage_metrics(text)
            async for frame in self._stream_audio_frames_from_iterator(
                in_order(), in_sample_rate=OUT_RATE, context_id=context_id
            ):
                await self.stop_ttfb_metrics()
                yield frame
        except Exception as exc:
            yield ErrorFrame(error=f"voice unavailable: {exc}")
        finally:
            for job in jobs:
                job.cancel()


class Turns(FrameProcessor):
    """Turn-taking: when the user is done, when they barge in, and what was really them speaking."""

    def __init__(self, *, headphones: bool, wake_word: bool = True) -> None:
        super().__init__()
        self.heard: asyncio.Queue[str] = asyncio.Queue()
        self.headphones, self.wake_word = headphones, wake_word
        self.smart = LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=MAX_PAUSE_S))
        self.parts: list[str] = []
        self.segments: deque[bool] = deque()  # per open segment: did it start while Plexis was talking?
        self.user_speaking = self.bot_speaking = False
        self.sounds_finished = True
        self.paused_at = 0.0
        self.bot_said: deque[str] = deque(maxlen=4)
        self.awake_until = float("inf")  # awake for the greeting's reply
        self._finish: asyncio.Task[None] | None = None

    async def setup(self, setup: FrameProcessorSetup) -> None:  # type: ignore[override]  # as Pipecat's own
        await super().setup(setup)
        self.smart.set_sample_rate(setup.audio_in_sample_rate or IN_RATE)

    def said(self, text: str) -> None:
        self.bot_said.append(text)
        self.awake_until = float("inf")  # until Plexis finishes speaking, then FOLLOW_UP_S more

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.smart.append_audio(frame.audio, self.user_speaking)
            return  # nothing downstream needs the microphone
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self.user_speaking = True
            self.segments.append(self.bot_speaking)
            self._cancel_finish()
            if self.headphones and self.bot_speaking:
                await self.broadcast_interruption()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.user_speaking = False
            self.paused_at = time.monotonic()
            state, _ = await self.smart.analyze_end_of_turn()
            self.sounds_finished = state == EndOfTurnState.COMPLETE
            self._cancel_finish()
            self._finish = self.create_task(self._finish_turn())
        elif isinstance(frame, Heard):
            during_bot = self.segments.popleft() if self.segments else self.bot_speaking
            await self._transcript(frame.text, during_bot)
            return
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            self.awake_until = time.monotonic() + FOLLOW_UP_S
        await self.push_frame(frame, direction)

    async def _transcript(self, text: str, during_bot: bool) -> None:
        text = clean_transcript(text)
        if not text or (during_bot and not self.headphones and self._is_echo(text)):
            if not self.parts:
                self.smart.clear()
            return
        if self.bot_speaking and not self.headphones:
            await self.broadcast_interruption()  # the user talked over Plexis
        self.parts.append(text)

    def _is_echo(self, text: str) -> bool:
        """Plexis's own voice, picked up by the microphone from the speakers.

        "Stop" or "wait" is always the user. Other short phrases are echo only if
        Plexis just said those exact words; longer ones if most words match.
        """
        if BARGE_IN.match(text):
            return False
        words = WORDS.findall(text.lower())
        if not words:
            return True
        spoken = " ".join(WORDS.findall(" ".join(self.bot_said).lower()))
        if len(words) < 3:
            return f" {' '.join(words)} " in f" {spoken} "
        said = set(spoken.split())
        return sum(w in said for w in words) / len(words) >= 0.6

    async def _finish_turn(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            waited = time.monotonic() - self.paused_at
            if self.segments and waited < 5.0:
                continue  # Whisper is still writing down the last thing said
            self.segments.clear()
            text = " ".join(self.parts)
            done = self.sounds_finished and not DANGLING.search(text)
            if waited >= (HOLD_S if done else MAX_PAUSE_S):
                break
        self.parts.clear()
        self.smart.clear()
        if text:
            self._deliver(text)

    def _deliver(self, text: str) -> None:
        if self.wake_word and time.monotonic() > self.awake_until:
            if not WAKE.match(text):
                print(f"  (ignored, not addressed to Plexis: {text!r})")
                return
            text = WAKE.sub("", text) or "hello"
        self.heard.put_nowait(text)

    def _cancel_finish(self) -> None:
        if self._finish is not None and not self._finish.done():
            self._finish.cancel()
        self._finish = None


def make_voice(engine: str, voice: str | None) -> TTSService:
    """Kokoro on this PC by default; the online voice if Kokoro isn't installed or downloaded."""
    if engine == "kokoro":
        model, voices = KOKORO_DIR / "kokoro-v1.0.onnx", KOKORO_DIR / "voices-v1.0.bin"
        if model.exists() and voices.exists():
            try:
                from pipecat.services.kokoro.tts import KokoroTTSService
                from pipecat.transcriptions.language import Language

                return KokoroTTSService(
                    model_path=str(model),
                    voices_path=str(voices),
                    settings=KokoroTTSService.Settings(
                        voice=voice or KOKORO_VOICE, language=Language.EN_US, speed=1.05
                    ),
                )
            except ImportError:
                print("  (Kokoro isn't installed; using the online voice)")
            except Exception as exc:  # e.g. a half-downloaded model file
                print(f"  (Kokoro couldn't load: {exc}; using the online voice)")
        else:
            print(f"  (Kokoro voice files not found in {KOKORO_DIR}; using the online voice)")
    return NeuralVoice(voice if voice and "Neural" in voice else VOICE)


class LiveDevice(Device):
    def __init__(
        self,
        base: str,
        *,
        model: str,
        headphones: bool,
        voice: str | None = None,
        engine: str = "kokoro",
        wake_word: bool = True,
    ) -> None:
        super().__init__(base, ears=None, voice=None)  # type: ignore[arg-type]
        self.model, self.voice_name, self.engine = model, voice, engine
        self.turns = Turns(headphones=headphones, wake_word=wake_word)
        self.task: PipelineWorker | None = None

    async def hear(self) -> str:
        return await self.turns.heard.get()

    async def say(self, text: str) -> None:
        if text and self.task is not None:
            print(f"  plexis: {text}")
            self.turns.said(text)
            await self.task.queue_frame(TTSSpeakFrame(text))

    def pipeline(self) -> Pipeline:
        from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=IN_RATE,
                audio_out_sample_rate=OUT_RATE,
            )
        )
        return Pipeline(
            [
                transport.input(),
                VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=SEGMENT_S))),
                Whisper(
                    compute_type="int8",
                    settings=WhisperSTTService.Settings(model=self.model, initial_prompt=HINT),
                ),
                self.turns,
                make_voice(self.engine, self.voice_name),
                transport.output(),
            ]
        )

    async def run(self) -> None:
        self.task = PipelineWorker(
            self.pipeline(),
            params=PipelineParams(audio_in_sample_rate=IN_RATE, audio_out_sample_rate=OUT_RATE),
            idle_timeout_secs=None,
            enable_rtvi=False,
        )
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(self.task)
        audio = asyncio.create_task(runner.run())
        print("  listening hands-free. Just talk; say 'goodbye' to finish.")
        try:
            await super().run()
            await self.task.queue_frame(EndFrame())  # lets the goodbye finish playing
            await asyncio.wait_for(audio, timeout=15)
        finally:
            if not audio.done():
                await self.task.cancel()
                await audio


def main(base: str, *, model: str, headphones: bool, voice: str | None, engine: str, wake_word: bool) -> None:
    import sys

    from loguru import logger

    logger.remove()  # keep the console for the conversation; Pipecat's own chatter only when it matters
    logger.add(sys.stderr, level="WARNING")
    try:
        asyncio.run(
            LiveDevice(
                base, model=model, headphones=headphones, voice=voice, engine=engine, wake_word=wake_word
            ).run()
        )
    except KeyboardInterrupt:
        print("\n  Goodbye!", flush=True)
        os._exit(0)  # audio and speech threads would otherwise hold Ctrl-C up
