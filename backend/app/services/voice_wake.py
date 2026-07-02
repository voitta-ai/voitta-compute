"""Wake-word engine for the voice assistant.

sherpa-onnx keyword spotting against typed keywords — no model training
needed. "voitta" isn't an English word, so the keywords file lists
phonetic spellings of how the phrase actually sounds; spoken "hey
voitta" typically matches the HEY VOIDA spelling.

Ported from the standalone prototype in ``audio/wake_engines.py``
(which stays untouched as the test bench).
"""

from __future__ import annotations

import numpy as np

from app.services import voice_install

SAMPLE_RATE = 16000

WAKE_PHRASE = "hey voitta"
# Row-selection phrases, only acted on while the sessions list is visible
# (see voice.py task-mode gating). "task quit" closes the list.
TASK_PHRASES = (
    "task one", "task two", "task three", "task four", "task five",
    "task six", "task seven", "task eight", "task nine",
    "task quit",
)
# Phrases spotted in addition to the wake word. Each is its own command
# (no transcription) — voice.py maps the matched phrase to an action.
COMMAND_PHRASES = ("tasks voitta", *TASK_PHRASES)
# Default phrase set for the live pipeline: the wake word plus commands.
DEFAULT_PHRASES = (WAKE_PHRASE, *COMMAND_PHRASES)

# Spelling variants per phrase — the KWS model only knows English BPE,
# so unusual words need spellings matching how they *sound*. The number
# words include common homophones ("WON"/"ONE", "TWO"/"TOO"/"TO") so a
# slightly slurred digit still spots.
SPELLINGS = {
    "hey voitta": ["HEY VOITTA", "HEY VOYTA", "HEY VOIDA", "HEY VOY TA"],
    "tasks voitta": [
        "TASKS VOITTA", "TASKS VOYTA", "TASKS VOIDA", "TASKS VOY TA",
        "TASK VOITTA", "TASK VOIDA",
    ],
    "voitta": ["VOITTA", "VOYTA", "VOIDA", "VOITA", "VOY TA", "VOY DA"],
    "task one": ["TASK ONE", "TASK WON"],
    "task two": ["TASK TWO", "TASK TOO", "TASK TO"],
    "task three": ["TASK THREE", "TASK TREE"],
    "task four": ["TASK FOUR", "TASK FOR", "TASK FORE"],
    "task five": ["TASK FIVE"],
    "task six": ["TASK SIX", "TASK SICKS"],
    "task seven": ["TASK SEVEN"],
    "task eight": ["TASK EIGHT", "TASK ATE"],
    "task nine": ["TASK NINE"],
    "task quit": ["TASK QUIT", "TASK QUITE", "TASK KWIT"],
}


def _tag(phrase: str) -> str:
    return phrase.replace(" ", "_")


class KwsWake:
    """Streaming keyword spotter. Feed 16 kHz int16 frames via process().

    Spots one or more phrases at once. ``process()`` returns the matched
    phrase (lowercased, spaces restored) or ``None`` — so the caller can
    route the wake word and command phrases differently.
    """

    def __init__(self, phrases: tuple[str, ...] | None = None,
                 threshold: float = 0.15, boost: float = 2.0) -> None:
        import sherpa_onnx
        import sentencepiece as spm

        phrase_list = list(phrases) if phrases else list(DEFAULT_PHRASES)

        d = voice_install.KWS_MODEL_DIR
        sp = spm.SentencePieceProcessor()
        sp.load(str(d / "bpe.model"))

        # ``@<tag>`` makes ``get_result`` return that tag for any spelling
        # of the phrase; we map it back to the phrase. Underscores stand
        # in for spaces (the tag must be a single token).
        self._by_tag = {_tag(p.lower()): p.lower() for p in phrase_list}
        keywords_file = voice_install.MODELS_DIR / "keywords_active.txt"
        with open(keywords_file, "w") as f:
            for phrase in phrase_list:
                spellings = SPELLINGS.get(phrase.lower(), [phrase.upper()])
                for s in spellings:
                    tokens = " ".join(sp.encode_as_pieces(s))
                    f.write(f"{tokens} :{boost} @{_tag(phrase.lower())}\n")

        def _onnx(part: str) -> str:
            matches = sorted(d.glob(f"{part}-*.onnx"))
            if not matches:
                raise FileNotFoundError(f"no {part}-*.onnx in {d}")
            return str(matches[0])

        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(d / "tokens.txt"),
            encoder=_onnx("encoder"),
            decoder=_onnx("decoder"),
            joiner=_onnx("joiner"),
            num_threads=2,
            keywords_file=str(keywords_file),
            keywords_threshold=threshold,
        )
        self.stream = self.kws.create_stream()
        self.phrases = [p.lower() for p in phrase_list]
        # Back-compat label for log lines.
        self.phrase = " / ".join(self.phrases)

    def process(self, frame_i16: np.ndarray) -> str | None:
        """Feed one frame; return the matched phrase, or None.

        The phrase is lowercased with spaces restored (e.g.
        ``"hey voitta"`` / ``"tasks voitta"``).
        """
        self.stream.accept_waveform(
            SAMPLE_RATE, frame_i16.astype(np.float32) / 32768.0
        )
        matched: str | None = None
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
            res = self.kws.get_result(self.stream)
            if res:
                matched = self._by_tag.get(res, res.replace("_", " "))
                self.kws.reset_stream(self.stream)
        return matched

    def reset(self) -> None:
        self.kws.reset_stream(self.stream)
