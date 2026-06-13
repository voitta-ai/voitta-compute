"""Voice assistant service — "hey voitta" → transcribe → inject into chat.

Runs the capture pipeline (sherpa-onnx wake word → Silero VAD
endpointing → mlx-whisper transcription) on a daemon thread inside the
menu-bar app process. A completed utterance is routed to the active
bookmarklet session (last-focused, see ``cl_sessions.get_active_session``)
through the same ``call_fn`` machinery the MCP debug tools use, where the
``submit_user_text`` primitive submits it exactly like a typed message.

Cocoa-free by design (the ``app.activity`` philosophy): state lives
behind a lock and the desktop layer's rumps timer polls ``snapshot()``
to render the menu-bar feedback. Notifications are raised by the
desktop poller on state transitions — this module never touches AppKit.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Callable

_log = logging.getLogger("voitta.voice")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms — KWS granularity
VAD_FRAME = 480  # 30 ms — Silero VAD granularity

MAX_UTTERANCE_S = 30.0  # hard cap on a single question
SILENCE_END_S = 1.2  # trailing silence that ends the utterance
SPEECH_TIMEOUT_S = 6.0  # give up if no speech follows the wake word
REFRACTORY_S = 2.0  # ignore wake re-triggers for this long

# Wake phrases that fire a command (open the sessions window) instead of
# recording an utterance. Kept as a literal — mirrors
# ``voice_wake.COMMAND_PHRASES`` but avoids importing that module (and
# its numpy dependency) before the voice components are installed.
_COMMAND_PHRASES = ("tasks voitta",)
SILENT_MIC_WATCHDOG_S = 5.0  # all-zero frames for this long → mic problem
INJECT_TIMEOUT_S = 20.0

# ---------------------------------------------------------------------------
# Shared state (poll via snapshot() — desktop renders from this)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state: str = "off"  # off|loading|listening|recording|transcribing|sending|sent|no_chat|error
_level: float = 0.0  # mic level 0..1 for the braille meter
_detail: str = ""  # last transcript or error message
_state_seq: int = 0  # bumped on every state change — lets the poller
#                      detect transitions (for one-shot notifications)

_thread: threading.Thread | None = None
_stop = threading.Event()

# Event loops of the uvicorn listeners (TLS first), registered from the
# FastAPI lifespan. Injection prefers _loops[0]; retries others.
_loops: list[asyncio.AbstractEventLoop] = []

# Hook for command phrases (e.g. "tasks voitta"). The desktop layer
# registers a plain callable; the voice thread invokes it with the
# matched phrase. Kept Cocoa-free here — the callback does any AppKit
# work itself (via AppHelper.callAfter).
_command_hook: "Callable[[str], None] | None" = None


def set_command_hook(fn: "Callable[[str], None] | None") -> None:
    """Register the handler for command wake-phrases. ``None`` clears it."""
    global _command_hook
    _command_hook = fn


# Mic-sensitivity ceiling, cached for the capture loop. This is the MAX
# boost the adaptive gain (AutoGain) may apply — NOT a fixed multiplier.
# Seeded from settings at pipeline start; the tray submenu calls
# set_mic_gain_runtime() to change it live (atomic float write, no lock).
_mic_gain: float = 1.0


def set_mic_gain_runtime(gain: float) -> None:
    """Update the live mic-sensitivity ceiling. Persisting is the caller's job."""
    global _mic_gain
    _mic_gain = max(1.0, min(24.0, float(gain)))


def _fire_command(phrase: str) -> None:
    hook = _command_hook
    if hook is None:
        _log.info("voice: command %r ignored — no hook registered", phrase)
        return
    try:
        hook(phrase)
    except Exception:  # noqa: BLE001
        _log.exception("voice: command hook failed for %r", phrase)


def _set(state: str, *, level: float | None = None, detail: str | None = None) -> None:
    global _state, _level, _detail, _state_seq
    with _lock:
        if state != _state:
            _state_seq += 1
        _state = state
        if level is not None:
            _level = level
        if detail is not None:
            _detail = detail


def snapshot() -> dict:
    with _lock:
        return {"state": _state, "level": _level, "detail": _detail, "seq": _state_seq}


def register_loop(loop: asyncio.AbstractEventLoop) -> None:
    if loop not in _loops:
        _loops.append(loop)


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def start() -> None:
    global _thread
    if is_running():
        return
    _stop.clear()
    _set("loading", level=0.0, detail="")
    _thread = threading.Thread(target=_run, name="voitta-voice", daemon=True)
    _thread.start()
    _log.info("voice thread started")


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    _set("off", level=0.0)
    _log.info("voice thread stopped")


# ---------------------------------------------------------------------------
# Endpointing (ported from audio/assistant.py)
# ---------------------------------------------------------------------------


class AutoGain:
    """Adaptive mic gain — fixes "I have to yell" without the over-gain
    footgun of a fixed multiplier.

    Boosts quiet/distant input toward a comfortable target RMS, but the
    boost is computed from the *measured* level, so a normal-volume voice
    is never amplified into clipping (which destroys the waveform the
    wake-word model needs). ``ceiling`` (the sensitivity setting) only
    caps how much help a very quiet mic can get; ``1.0`` disables it.
    A noise gate keeps near-silence from being pumped up into false
    wakes, and a fast-attack / slow-release smoother avoids gain pumping.
    """

    TARGET_RMS = 0.06   # ~-24 dBFS — a healthy speech level for the KWS model
    GATE_RMS = 0.004    # below this is silence/noise; hold gain, don't pump

    def __init__(self) -> None:
        self.gain = 1.0

    def apply(self, frame_i16, ceiling: float):
        import numpy as np

        if ceiling <= 1.0:
            self.gain = 1.0
            return frame_i16
        x = frame_i16.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x)))
        if rms < self.GATE_RMS:
            desired = self.gain  # silence: hold, don't amplify the noise floor
        else:
            desired = min(ceiling, max(1.0, self.TARGET_RMS / rms))
        # Fast to boost (don't clip the start of a phrase), slow to back off.
        alpha = 0.3 if desired > self.gain else 0.1
        self.gain += (desired - self.gain) * alpha
        y = x * self.gain
        np.clip(y, -1.0, 1.0, out=y)  # safety only — AGC keeps us well under
        return (y * 32767.0).astype(np.int16)


class Endpointer:
    """Silero-VAD-based utterance endpointing over 80 ms frames."""

    def __init__(self, vad) -> None:
        self.vad = vad
        self.speech_started = False
        self.last_speech_t = time.monotonic()
        self.started_t = time.monotonic()

    def feed(self, frame_i16) -> str:
        """Returns 'continue', 'end', or 'timeout'."""
        import numpy as np

        # 1280 samples isn't a multiple of 480: score the first 960
        # (two VAD frames) — plenty for a per-80ms speech decision.
        scores = self.vad.predict(frame_i16[: VAD_FRAME * 2], frame_size=VAD_FRAME)
        speaking = float(np.max(scores)) > 0.4
        now = time.monotonic()
        if speaking:
            self.speech_started = True
            self.last_speech_t = now
        if not self.speech_started:
            return "timeout" if now - self.started_t > SPEECH_TIMEOUT_S else "continue"
        if now - self.last_speech_t > SILENCE_END_S:
            return "end"
        if now - self.started_t > MAX_UTTERANCE_S:
            return "end"
        return "continue"


# ---------------------------------------------------------------------------
# Injection into the active chat
# ---------------------------------------------------------------------------


def _inject(text: str) -> None:
    """Send the transcript into the active session. Sets state to
    sent / no_chat / error accordingly."""
    from app.services import cl_sessions
    from app.services.mcp_server import _call_in_session

    info = cl_sessions.get_active_session()
    if info is None or not _loops:
        _set("no_chat", detail=text)
        return

    _set("sending", detail=text)
    last_err = "dispatch_failed"
    for loop in _loops:
        try:
            fut = asyncio.run_coroutine_threadsafe(
                _call_in_session(info.session_id, "submit_user_text", {"text": text}),
                loop,
            )
            res = fut.result(timeout=INJECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            continue
        if res.get("ok"):
            _set("sent", detail=text)
            _log.info("voice: injected %r into %s (%s)", text, info.session_id, info.host)
            return
        last_err = res.get("message") or res.get("error") or "unknown error"
        if res.get("error") != "dispatch_failed":
            break  # session-level failure — retrying another loop won't help
    if last_err == "busy" or "busy" in str(last_err):
        _set("error", detail="chat is busy — try again")
    else:
        _set("error", detail=str(last_err))
    _log.warning("voice: injection failed: %s", last_err)


# ---------------------------------------------------------------------------
# Capture thread
# ---------------------------------------------------------------------------


def _run() -> None:
    try:
        _run_pipeline()
    except Exception as exc:  # noqa: BLE001
        _log.exception("voice pipeline crashed")
        _set("error", level=0.0, detail=f"voice failed: {exc}")


def _run_pipeline() -> None:
    import numpy as np
    import sounddevice as sd
    import mlx_whisper
    from openwakeword.vad import VAD

    from app.services import voice_install
    from app.services.voice_wake import KwsWake

    whisper_dir = str(voice_install.WHISPER_DIR)

    # Seed the live sensitivity ceiling from saved settings.
    try:
        from app.services import user_settings as _us
        set_mic_gain_runtime(_us.mic_gain())
    except Exception:
        pass
    autogain = AutoGain()

    wake = KwsWake()
    # The openwakeword wheel ships no model files — use the silero_vad.onnx
    # our installer downloaded (the class default points inside the package).
    vad = VAD(model_path=str(voice_install.SILERO_VAD_PATH))
    # Warm up whisper so the first real transcription isn't slow.
    mlx_whisper.transcribe(
        np.zeros(SAMPLE_RATE, dtype=np.float32), path_or_hf_repo=whisper_dir
    )
    if _stop.is_set():
        return

    audio_q: queue.Queue = queue.Queue()

    def callback(indata, frames, t, status):
        audio_q.put(indata[:, 0].copy())

    state = "wake"
    endpointer: Endpointer | None = None
    utterance: list = []
    refractory_until = 0.0
    hold_until = 0.0  # keep sent/no_chat/error visible this long
    silent_since: float | None = None
    warned_silent = False

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            callback=callback,
        )
        stream.start()
    except Exception as exc:  # noqa: BLE001
        _set("error", detail=(
            "microphone unavailable — check System Settings → "
            f"Privacy & Security → Microphone ({exc})"
        ))
        return

    _set("listening", level=0.0)
    _log.info("voice: listening for %r", wake.phrase)

    try:
        while not _stop.is_set():
            try:
                frame = audio_q.get(timeout=0.3)
            except queue.Empty:
                continue

            # Mic sensitivity: adaptively boost quiet input toward a
            # target level before the wake spotter, VAD, and transcription
            # see it. _mic_gain is the CEILING (1.0 = off); AutoGain never
            # over-amplifies a normal voice into clipping.
            frame = autogain.apply(frame, _mic_gain)

            # Mic level for the menu-bar braille meter (post-gain, so the
            # meter reflects what the pipeline actually hears).
            rms = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))
            level = min(1.0, rms * 12.0)

            # Permission-denied watchdog: macOS often delivers silence
            # instead of failing the stream open.
            if float(np.abs(frame).max()) == 0.0:
                silent_since = silent_since or time.monotonic()
                if (not warned_silent
                        and time.monotonic() - silent_since > SILENT_MIC_WATCHDOG_S
                        and state == "wake"):
                    warned_silent = True
                    _set("error", detail=(
                        "microphone is silent — check System Settings → "
                        "Privacy & Security → Microphone"
                    ))
            else:
                silent_since = None
                if warned_silent:
                    warned_silent = False
                    _set("listening")

            if state == "wake":
                matched = wake.process(frame)
                shown = snapshot()["state"]
                if shown == "listening":
                    _set("listening", level=level)
                elif shown in ("sent", "no_chat", "error") and not warned_silent \
                        and time.monotonic() > hold_until:
                    # Outcome flash is over — resume the live meter.
                    _set("listening", level=level)
                if matched and time.monotonic() > refractory_until:
                    wake.reset()
                    refractory_until = time.monotonic() + REFRACTORY_S
                    if matched in _COMMAND_PHRASES:
                        # Command phrase — fire its action, no recording.
                        _log.info("voice: command phrase %r detected", matched)
                        _fire_command(matched)
                        _set("sent", detail=f"⌘ {matched}", level=level)
                        hold_until = time.monotonic() + 1.5
                    else:
                        _log.info("voice: wake word detected")
                        endpointer = Endpointer(vad)
                        utterance = []
                        state = "record"
                        _set("recording", level=level)

            elif state == "record":
                utterance.append(frame)
                _set("recording", level=level)
                verdict = endpointer.feed(frame)
                if verdict == "timeout":
                    _log.info("voice: no speech after wake word")
                    state = "wake"
                    refractory_until = time.monotonic() + REFRACTORY_S
                    _set("listening", level=level)
                elif verdict == "end":
                    audio = np.concatenate(utterance).astype(np.float32) / 32768.0
                    _set("transcribing", level=0.0)
                    t0 = time.time()
                    result = mlx_whisper.transcribe(audio, path_or_hf_repo=whisper_dir)
                    text = (result.get("text") or "").strip()
                    _log.info("voice: transcribed %.1fs in %.2fs: %r",
                              len(audio) / SAMPLE_RATE, time.time() - t0, text)
                    if _stop.is_set():
                        return
                    if text:
                        _inject(text)
                        hold_until = time.monotonic() + (
                            1.5 if snapshot()["state"] == "sent" else 2.5
                        )
                    else:
                        _set("listening")
                    state = "wake"
                    refractory_until = time.monotonic() + REFRACTORY_S
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            pass
