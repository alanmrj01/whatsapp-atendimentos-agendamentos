import base64

import pytest

from app.speech.transcription import (
    MAX_SHORT_AUDIO_BYTES,
    SpeechTranscriptionError,
    _extract_transcript,
    _payload,
)


def test_speech_payload_uses_pt_br_chirp3_and_inline_audio() -> None:
    audio = b"test-audio"
    payload = _payload(audio)

    assert payload["config"]["autoDecodingConfig"] == {}
    assert payload["config"]["languageCodes"] == ["pt-BR"]
    assert payload["config"]["model"] == "chirp_3"
    assert payload["content"] == base64.b64encode(audio).decode("ascii")


def test_speech_payload_rejects_oversized_audio() -> None:
    with pytest.raises(SpeechTranscriptionError):
        _payload(b"x" * (MAX_SHORT_AUDIO_BYTES + 1))


def test_speech_extracts_sequential_top_transcripts() -> None:
    response = {
        "results": [
            {"alternatives": [{"transcript": "preciso limpar meu ar"}]},
            {"alternatives": [{"transcript": "amanhã de manhã"}]},
        ]
    }

    assert (
        _extract_transcript(response)
        == "preciso limpar meu ar amanhã de manhã"
    )
