"""Sanity checks on a transcription result before it is stored in Engram.

Two problems seen in Engram:
- transcripts that stop after the first 20-150 s of a long video (a transient
  transcriber fault: transcribing again fixed them);
- silent coding streams transcribed as Whisper's silence hallucinations
  ("you Bye. Thank you. . .", or Japanese for an English video).

Thresholds are deliberately loose: on tela, the quietest real videos (coding
devlogs) have 150+ words at ~7 words a minute, short music clips a dozen words.
"""

import re

# A transcript whose last segment ends before this share of the video is
# treated as a transcriber fault (retryable) ...
MIN_COVERAGE = 0.5
# ... but only for videos at least this long (short clips often end in music).
COVERAGE_MIN_DURATION_SECONDS = 300

# Sparse: a video this long with fewer words than this, at under this many
# words a minute, is a silent stream with hallucinated fragments. (Only for
# mostly-Latin transcripts: a run of CJK text counts as one "word".)
SPARSE_MIN_DURATION_SECONDS = 300
SPARSE_MAX_WORDS = 50
SPARSE_MAX_WORDS_PER_MINUTE = 3.0

# Filler: at most this many words, at least this share of them Whisper's
# usual silence output.
FILLER_MAX_WORDS = 20
FILLER_MIN_SHARE = 0.6
FILLER_WORDS = frozenset({
    "you", "bye", "thank", "thanks", "uh", "um", "umm", "hmm", "mm", "oh", "ah", "huh",
})

# Wrong script: a Latin-script title, a transcript mostly in another script,
# and sparse with it (real speech in another language runs to hundreds of
# letters a minute, so it is left alone).
LATIN_TITLE_MIN_SHARE = 0.9
NON_LATIN_MIN_SHARE = 0.8
NON_LATIN_MAX_LETTERS_PER_MINUTE = 60

_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")


class IncompleteTranscriptError(Exception):
    """The transcript covers too little of the video; transcribe again later."""


class NoSpeechDetectedError(Exception):
    """The transcript is only silence hallucinations; nothing worth storing."""


def _is_latin(ch: str) -> bool:
    code = ord(ch)
    return code < 0x250 or 0x1E00 <= code <= 0x1EFF


def _latin_share(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 1.0
    return sum(_is_latin(ch) for ch in letters) / len(letters)


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min"


def check_transcript(segments: list[dict], duration_seconds: float | None, title: str) -> None:
    """Raise NoSpeechDetectedError or IncompleteTranscriptError for a bad transcript."""
    duration = float(duration_seconds or 0)
    text = " ".join((seg.get("text") or "") for seg in segments)
    words = [w.lower() for w in _WORD_RE.findall(text)]
    minutes = duration / 60

    if not words:
        raise NoSpeechDetectedError("No speech detected: empty transcript")

    if len(words) <= FILLER_MAX_WORDS:
        filler = sum(w in FILLER_WORDS for w in words)
        if filler / len(words) >= FILLER_MIN_SHARE:
            raise NoSpeechDetectedError(
                f"No speech detected: transcript is only filler ({' '.join(words)!r})"
            )

    if (
        duration >= SPARSE_MIN_DURATION_SECONDS
        and _latin_share(text) >= 0.5
        and len(words) < SPARSE_MAX_WORDS
        and len(words) / minutes < SPARSE_MAX_WORDS_PER_MINUTE
    ):
        raise NoSpeechDetectedError(
            f"No speech detected: {len(words)} words in {_minutes(duration)}"
        )

    if duration > 0 and _latin_share(title) >= LATIN_TITLE_MIN_SHARE:
        letters = [ch for ch in text if ch.isalpha()]
        non_latin = sum(not _is_latin(ch) for ch in letters)
        if (
            non_latin / len(letters) >= NON_LATIN_MIN_SHARE
            and non_latin / minutes < NON_LATIN_MAX_LETTERS_PER_MINUTE
        ):
            raise NoSpeechDetectedError(
                f"No speech detected: sparse non-Latin transcript ({non_latin} letters "
                f"in {_minutes(duration)}) for a Latin-script title"
            )

    if duration >= COVERAGE_MIN_DURATION_SECONDS:
        covered = max((float(seg.get("end") or 0) for seg in segments), default=0.0)
        if covered < MIN_COVERAGE * duration:
            raise IncompleteTranscriptError(
                f"Transcript covers only {_minutes(covered)} of {_minutes(duration)} "
                f"({covered / duration:.0%}); likely a transcriber fault, will retry"
            )
