"""Tests for the transcript sanity checks run before storing in Engram.

Fixtures are modelled on real transcripts found in Engram on tela.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from curator.config import CuratorSettings
from curator.orchestrator import IngestionOrchestrator
from curator.plugins.base import ContentMetadata, ContentResult
from curator.storage import CuratorStorage
from curator.transcript_quality import (
    IncompleteTranscriptError,
    NoSpeechDetectedError,
    check_transcript,
)

SENTENCE = ("So the next thing we want to do here is wire the composable into the "
            "component and check that the state updates when the prop changes.")  # 25 words


def _spread(texts, duration, until=None):
    """Segments with the given texts spread evenly up to `until` (default: the whole video)."""
    until = until or duration * 0.98
    step = until / len(texts)
    return [{"start": i * step, "end": (i + 1) * step, "text": t, "speaker": "SPEAKER_00"}
            for i, t in enumerate(texts)]


# --- Stored as normal --------------------------------------------------------

def test_normal_talk_passes():
    check_transcript(_spread([SENTENCE] * 200, 1800), 1800, "Building a parser in Rust")


def test_quiet_coding_devlog_passes():
    # ~150 words over 23 min (6.6 words/min), like "Introducing DIFFBRO" on tela.
    check_transcript(_spread([SENTENCE] * 6, 1393, until=1150), 1393,
                     "Introducing DIFFBRO - Your AI powered PEER REVIEWS")


def test_short_music_clip_passes():
    # "Algo Dance" - an AI-generated song: 12 words in 161 s.
    lyrics = ["I'll go dance", "I'll go dance, trading view, a chance, a glance"]
    check_transcript(_spread(lyrics, 161, until=143), 161, '"Algo Dance" - AI Generated Song')


def test_short_clip_ending_early_passes():
    # 61 s clip whose speech ends at 33 s: too short for the coverage check.
    segs = _spread(["Yes, sir. Do you have some minutes to talk about Bitcoin dominance?"], 61, until=33)
    check_transcript(segs, 61, "Ben Goes to Coin Bureau's Office Again")


def test_short_thank_you_sentence_is_not_filler():
    segs = _spread(["Thank you so much for watching, see you next week, bye!"], 45)
    check_transcript(segs, 45, "Quick update")


def test_dense_foreign_language_transcript_is_left_alone():
    # Real (if mis-detected) speech in Japanese is hundreds of letters a minute.
    line = "サル・カン氏は、彼の教育用YouTube動画を使って学習を民主化しました。"
    check_transcript(_spread([line] * 300, 3099), 3099, "No Priors Ep. 28 | With Khan Academy's Creator")


def test_non_latin_title_with_non_latin_transcript_passes():
    check_transcript(_spread(["こんにちは、今日はパーサーを書きます"] * 150, 600), 600, "日本語の動画")


# --- No speech: skipped ------------------------------------------------------

@pytest.mark.parametrize("texts,duration,title", [
    ([" you", " Bye.", " Thank you.", " .", " ."], 5400, "Coding stream: Vue 3 hooks"),
    ([" you", " What the fuck?", " Basically.", " is this."], 2084,
     "Senior Engineer Codes New App Feature With Vue.js"),
    ([" Thank you.", " I love you, too.", " Thank you.", " Bye.", " Bye.",
      " I'm going to put it in my bag. I'm going to put it in my bag.", " Bye.",
      " Thank you.", " you"], 1187, "Programming Pomodoro Chaining PART 2 (Vue 3 Hooks)"),
    ([" ご視聴ありがとうございました"] * 12, 3600, "Late night live coding session"),
    ([], 900, "Silent stream"),
])
def test_no_speech_detected(texts, duration, title):
    with pytest.raises(NoSpeechDetectedError, match="No speech detected"):
        check_transcript(_spread(texts, duration) if texts else [], duration, title)


def test_sparse_non_latin_for_latin_title_is_no_speech():
    # More "words" than the sparse rule allows, but only ~20 letters a minute.
    segs = _spread(["ありがとう", "おやすみ", "はい"] * 20, 1800)
    with pytest.raises(NoSpeechDetectedError, match="non-Latin"):
        check_transcript(segs, 1800, "Writing code without coding")


# --- Cut off: retryable --------------------------------------------------------

def test_transcript_cut_off_early_is_incomplete():
    # 25 min talk, transcript stops at 9 min ("Engineering voice agents" on tela).
    segs = _spread([SENTENCE] * 50, 1475, until=543)
    with pytest.raises(IncompleteTranscriptError, match="covers only"):
        check_transcript(segs, 1475, "Engineering voice agents: Latency, quality, and scale")


def test_transcript_cut_off_after_two_minutes_is_incomplete():
    segs = _spread([SENTENCE] * 12, 3600, until=120)
    with pytest.raises(IncompleteTranscriptError):
        check_transcript(segs, 3600, "Frontier AI at Home")


# --- Orchestrator --------------------------------------------------------------

VIDEO_ID = "abcdefghijk"
VIDEO_URL = f"https://youtube.com/watch?v={VIDEO_ID}"


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield CuratorStorage(str(Path(tmpdir) / "test.db"))


async def _ingest_with_transcript(storage, segments, duration, tmp_path):
    settings = CuratorSettings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")
    orchestrator = IngestionOrchestrator(storage, settings)
    audio = tmp_path / f"{VIDEO_ID}.wav"
    audio.write_bytes(b"")
    plugin = MagicMock()
    plugin.source_type = "youtube"
    plugin.fetch_metadata = AsyncMock(return_value=ContentMetadata(
        content_id=VIDEO_ID, title="Coding stream", url=VIDEO_URL, duration_seconds=duration))
    plugin.fetch_content = AsyncMock(return_value=ContentResult(
        text=str(audio), segments=[], source="diarized_transcriber", needs_transcription=True))

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin), \
         patch.object(orchestrator, "_transcribe", new_callable=AsyncMock,
                      return_value={"segments": segments, "speakers": []}), \
         patch("curator.orchestrator.httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=MagicMock(status_code=404))
        client.post = AsyncMock()
        result = await orchestrator.ingest_url(VIDEO_URL)
    return result, client.post


@pytest.mark.asyncio
async def test_filler_transcript_is_skipped_and_not_stored(storage, tmp_path):
    segs = _spread([" you", " Bye.", " Thank you.", " ."], 5400)
    result, engram_post = await _ingest_with_transcript(storage, segs, 5400, tmp_path)

    assert result is False
    engram_post.assert_not_called()
    item = storage.get_ingested_item_by_source("youtube", VIDEO_ID)
    assert item["status"] == "skipped"
    assert item["error_message"].startswith("No speech detected")


@pytest.mark.asyncio
async def test_cut_off_transcript_fails_for_retry_and_is_not_stored(storage, tmp_path):
    segs = _spread([SENTENCE] * 12, 3600, until=120)
    result, engram_post = await _ingest_with_transcript(storage, segs, 3600, tmp_path)

    assert result is False
    engram_post.assert_not_called()
    item = storage.get_ingested_item_by_source("youtube", VIDEO_ID)
    assert item["status"] == "failed"
    assert "covers only" in item["error_message"]
