from __future__ import annotations

import asyncio
import base64
import os
import re
from typing import Any

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import AuthorizedSession
from requests import RequestException
from requests.exceptions import Timeout

MAX_SHORT_AUDIO_BYTES = 10 * 1024 * 1024
SPEECH_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SPEECH_LOCATION = "us"
SPEECH_MODEL = "chirp_3"
SPEECH_LANGUAGE = "pt-BR"
SPEECH_TIMEOUT_SECONDS = 30


class SpeechTranscriptionError(RuntimeError):
    """Sanitized failure while transcribing customer audio."""


def _project_id(detected_project_id: str | None) -> str:
    project_id = (
        os.getenv("CONVERSATION_LAB_SPEECH_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or detected_project_id
        or ""
    ).strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id):
        raise SpeechTranscriptionError("Speech project is not configured")
    return project_id


def _payload(audio_bytes: bytes) -> dict[str, Any]:
    if not audio_bytes:
        raise SpeechTranscriptionError("Audio is empty")
    if len(audio_bytes) > MAX_SHORT_AUDIO_BYTES:
        raise SpeechTranscriptionError("Audio exceeds short recognition limit")
    return {
        "config": {
            "autoDecodingConfig": {},
            "languageCodes": [SPEECH_LANGUAGE],
            "model": SPEECH_MODEL,
            "features": {"enableAutomaticPunctuation": True},
        },
        "content": base64.b64encode(audio_bytes).decode("ascii"),
    }


def _extract_transcript(response_data: Any) -> str:
    if not isinstance(response_data, dict):
        raise SpeechTranscriptionError("Speech response is invalid")
    results = response_data.get("results")
    if not isinstance(results, list):
        raise SpeechTranscriptionError("Speech response is invalid")

    transcripts: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        alternatives = result.get("alternatives")
        if not isinstance(alternatives, list) or not alternatives:
            continue
        first = alternatives[0]
        if not isinstance(first, dict):
            continue
        transcript = first.get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            transcripts.append(transcript.strip())

    return " ".join(transcripts).strip()


def _transcribe_short_audio_sync(audio_bytes: bytes) -> str:
    try:
        credentials, detected_project = google.auth.default(
            scopes=[SPEECH_SCOPE],
        )
    except DefaultCredentialsError as exc:
        raise SpeechTranscriptionError(
            "Speech credentials are unavailable"
        ) from exc

    project_id = _project_id(detected_project)
    url = (
        f"https://{SPEECH_LOCATION}-speech.googleapis.com/v2/"
        f"projects/{project_id}/locations/{SPEECH_LOCATION}/"
        "recognizers/_:recognize"
    )
    session = AuthorizedSession(credentials)
    try:
        response = session.post(
            url,
            json=_payload(audio_bytes),
            timeout=SPEECH_TIMEOUT_SECONDS,
        )
    except Timeout as exc:
        raise SpeechTranscriptionError("Speech request timed out") from exc
    except RequestException as exc:
        raise SpeechTranscriptionError("Speech request failed") from exc
    finally:
        session.close()

    if response.status_code < 200 or response.status_code >= 300:
        raise SpeechTranscriptionError(
            f"Speech API rejected request with status {response.status_code}"
        )

    try:
        response_data = response.json()
    except ValueError as exc:
        raise SpeechTranscriptionError("Speech response is invalid") from exc

    return _extract_transcript(response_data)


async def transcribe_short_audio(audio_bytes: bytes) -> str:
    return await asyncio.to_thread(
        _transcribe_short_audio_sync,
        audio_bytes,
    )
