"""Server-side V3 text-to-speech with a persisted-assistant-turn boundary.

The Volcengine/Doubao adapter uses the current V3 unidirectional WebSocket
endpoint and the new console API Key.  It intentionally accepts a
``PersistedAITurn`` instead of free-form text: callers must load an assistant
turn from the database before any text can leave the application.

Audio is assembled in memory and is never written to disk.  Provider failures
become a browser-speech fallback result so TTS availability cannot change
assessment state, evidence, or scoring.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


DOUBAO_V3_WEBSOCKET_URL = (
    "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream"
)
DOUBAO_V3_RESOURCE_IDS = frozenset({"seed-tts-2.0", "seed-icl-2.0"})
DEFAULT_DOUBAO_INTERVIEWER_SPEAKER = "zh_female_cancan_uranus_bigtts"
DEFAULT_DOUBAO_INTERVIEW_CONTEXT = (
    "请像一位成熟、温和、专注的访谈者面对面自然交谈。语气真诚克制，不要播音腔，"
    "不要教学腔，不要逐字重读；停顿自然，句尾轻收。"
)
MP3_CONTENT_TYPE = "audio/mpeg"

_FULL_CLIENT_JSON_HEADER = bytes((0x11, 0x10, 0x10, 0x00))
_FULL_SERVER_RESPONSE = 0x9
_AUDIO_ONLY_SERVER = 0xB
_ERROR_MESSAGE = 0xF

_FLAG_NO_SEQUENCE = 0x0
_FLAG_POSITIVE_SEQUENCE = 0x1
_FLAG_LAST_NO_SEQUENCE = 0x2
_FLAG_NEGATIVE_SEQUENCE = 0x3
_FLAG_WITH_EVENT = 0x4

_COMPRESSION_NONE = 0x0
_COMPRESSION_GZIP = 0x1

_EVENT_CONNECTION_STARTED = 50
_EVENT_CONNECTION_FAILED = 51
_EVENT_CONNECTION_FINISHED = 52
_EVENT_SESSION_FINISHED = 152
_EVENT_USAGE_RESPONSE = 154
_EVENT_TTS_SENTENCE_START = 350
_EVENT_TTS_SENTENCE_END = 351
_EVENT_TTS_RESPONSE = 352
_EVENT_TTS_SUBTITLE = 364
_CONNECTION_EVENTS = {
    _EVENT_CONNECTION_STARTED,
    _EVENT_CONNECTION_FAILED,
    _EVENT_CONNECTION_FINISHED,
}

# A deterministic, silence-like MP3 fixture for local automation.  It contains
# an empty ID3v2 header followed by two small MPEG-1 Layer III frames.  Fake
# mode validates delivery only; natural voice requires a real-provider UAT.
_FAKE_MP3_BYTES = (
    b"ID3\x04\x00\x00\x00\x00\x00\x00"
    + (b"\xff\xfb\x10\xc4" + (b"\x00" * 100)) * 2
)


TTSMode = Literal["fake", "doubao", "disabled"]


class InvalidPersistedAITurnError(ValueError):
    """Raised before networking when the caller violates the privacy boundary."""


class DoubaoProtocolError(RuntimeError):
    """Raised internally for malformed or unexpected V3 provider frames."""


class DoubaoProviderError(RuntimeError):
    """Raised internally when Doubao returns an explicit error frame."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Doubao TTS error {code}")
        self.code = code
        # Raw provider text can contain request details.  Keep it internal and
        # never return or persist it.
        self.provider_message = message


@dataclass(frozen=True)
class PersistedAITurn:
    """Minimal DTO constructed only after loading an AI turn from storage."""

    id: int | str
    session_uuid: str
    turn_index: int
    role: Literal["assistant"]
    content: str
    persisted: Literal[True]

    def __post_init__(self) -> None:
        valid_id = (
            isinstance(self.id, int)
            and not isinstance(self.id, bool)
            and self.id > 0
        ) or (isinstance(self.id, str) and bool(self.id.strip()))
        if not valid_id:
            raise InvalidPersistedAITurnError("AI turn must have a persisted database id")
        if not self.session_uuid.strip():
            raise InvalidPersistedAITurnError("AI turn must belong to a persisted session")
        if self.turn_index < 0:
            raise InvalidPersistedAITurnError("AI turn index must be non-negative")
        if self.role != "assistant":
            raise InvalidPersistedAITurnError("only persisted assistant turns may be synthesized")
        if self.persisted is not True:
            raise InvalidPersistedAITurnError("AI turn must be marked as persisted")
        if not self.content.strip():
            raise InvalidPersistedAITurnError("AI turn text is empty")


@dataclass(frozen=True)
class TTSConfig:
    mode: TTSMode = "fake"
    api_key: str = field(default="", repr=False)
    resource_id: str = "seed-tts-2.0"
    speaker: str = DEFAULT_DOUBAO_INTERVIEWER_SPEAKER
    sample_rate: int = 24_000
    bit_rate: int = 128_000
    speech_rate: int = -5
    context_text: str = DEFAULT_DOUBAO_INTERVIEW_CONTEXT
    timeout_seconds: float = 25.0
    max_attempts: int = 2
    retry_delay_seconds: float = 0.25
    max_text_chars: int = 2_000
    max_audio_bytes: int = 10 * 1024 * 1024
    endpoint: str = DOUBAO_V3_WEBSOCKET_URL

    def __post_init__(self) -> None:
        if self.mode not in {"fake", "doubao", "disabled"}:
            raise ValueError("TTS mode must be fake, doubao, or disabled")
        if self.sample_rate not in {8_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000}:
            raise ValueError("unsupported Doubao V3 sample rate")
        if not 64_000 <= self.bit_rate <= 160_000:
            raise ValueError("Doubao V3 MP3 bit rate must be between 64000 and 160000")
        if not -50 <= self.speech_rate <= 100:
            raise ValueError("Doubao V3 speech rate must be between -50 and 100")
        if len(self.context_text) > 500:
            raise ValueError("Doubao V3 context instruction is too long")
        if self.timeout_seconds <= 0:
            raise ValueError("TTS timeout must be positive")
        if self.max_attempts not in {1, 2}:
            raise ValueError("TTS max_attempts must be one or two")
        if not 0 <= self.retry_delay_seconds <= 2:
            raise ValueError("TTS retry delay must be between zero and two seconds")
        if self.max_text_chars <= 0:
            raise ValueError("TTS max_text_chars must be positive")
        if self.max_audio_bytes <= 0:
            raise ValueError("TTS max_audio_bytes must be positive")
        # The API key must never be routable to a user-configured host.
        if self.endpoint != DOUBAO_V3_WEBSOCKET_URL:
            raise ValueError("Doubao TTS endpoint must be the official V3 WebSocket URL")

    @classmethod
    def from_app_settings(cls) -> "TTSConfig":
        # Import lazily so protocol tests don't initialize the whole app.
        from app.core.config import settings

        return cls(
            mode=settings.tts_mode,
            api_key=settings.doubao_tts_api_key,
            resource_id=settings.doubao_tts_resource_id,
            speaker=settings.doubao_tts_speaker,
            speech_rate=settings.doubao_tts_speech_rate,
            bit_rate=settings.doubao_tts_bit_rate,
            context_text=settings.doubao_tts_context_text,
            timeout_seconds=settings.doubao_tts_timeout_seconds,
            max_attempts=settings.doubao_tts_max_attempts,
        )


@dataclass(frozen=True)
class TTSSynthesisResult:
    """In-memory synthesis result consumed by the speech endpoint."""

    audio: bytes | None
    content_type: str
    provider: Literal["fake", "doubao", "disabled"]
    fallback_required: bool
    fallback_reason: str | None = None
    request_id: str | None = None
    provider_error_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.audio is not None and not self.fallback_required


@dataclass(frozen=True)
class DoubaoResponseFrame:
    message_type: int
    flags: int
    payload: bytes
    event: int | None = None
    session_id: str | None = None
    connect_id: str | None = None
    sequence: int | None = None
    is_final: bool = False
    error_code: int | None = None
    error_message: str | None = None


def build_doubao_request_frame(*, config: TTSConfig, ai_text: str) -> bytes:
    """Build the official V3 FullClientRequest frame.

    Credentials and request identifiers are header-only.  The JSON body has no
    participant or session identifier and contains only the persisted AI text,
    the configured speaker, and MP3 audio parameters.
    """

    request_params: dict[str, Any] = {
        "speaker": config.speaker,
        "text": ai_text,
        "audio_params": {
            "format": "mp3",
            "sample_rate": config.sample_rate,
            "bit_rate": config.bit_rate,
            "speech_rate": config.speech_rate,
        },
    }
    if config.resource_id == "seed-tts-2.0" and config.context_text.strip():
        request_params["context_texts"] = [config.context_text.strip()]
    request_payload = {"req_params": request_params}
    serialized = json.dumps(
        request_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _FULL_CLIENT_JSON_HEADER + len(serialized).to_bytes(4, "big") + serialized


def _read_bytes(frame: bytes, cursor: int, size: int, label: str) -> tuple[bytes, int]:
    if size < 0 or cursor + size > len(frame):
        raise DoubaoProtocolError(f"response frame is missing {label}")
    return frame[cursor : cursor + size], cursor + size


def _read_uint32(frame: bytes, cursor: int, label: str) -> tuple[int, int]:
    raw, cursor = _read_bytes(frame, cursor, 4, label)
    return int.from_bytes(raw, "big", signed=False), cursor


def _read_int32(frame: bytes, cursor: int, label: str) -> tuple[int, int]:
    raw, cursor = _read_bytes(frame, cursor, 4, label)
    return int.from_bytes(raw, "big", signed=True), cursor


def _read_text(frame: bytes, cursor: int, label: str) -> tuple[str, int]:
    size, cursor = _read_uint32(frame, cursor, f"{label} length")
    raw, cursor = _read_bytes(frame, cursor, size, label)
    try:
        return raw.decode("utf-8"), cursor
    except UnicodeDecodeError as exc:
        raise DoubaoProtocolError(f"response {label} is not UTF-8") from exc


def parse_doubao_response_frame(
    raw: bytes | bytearray | memoryview,
) -> DoubaoResponseFrame:
    """Parse one V3 binary-protocol server frame and fail closed on mismatch."""

    frame = bytes(raw)
    if len(frame) < 4:
        raise DoubaoProtocolError("response frame is shorter than the V3 header")

    protocol_version = frame[0] >> 4
    header_words = frame[0] & 0x0F
    message_type = frame[1] >> 4
    flags = frame[1] & 0x0F
    serialization = frame[2] >> 4
    compression = frame[2] & 0x0F
    header_size = header_words * 4

    if protocol_version != 1:
        raise DoubaoProtocolError("unsupported V3 binary protocol version")
    if header_words < 1 or header_size > len(frame):
        raise DoubaoProtocolError("invalid V3 response header size")
    if message_type not in {_FULL_SERVER_RESPONSE, _AUDIO_ONLY_SERVER, _ERROR_MESSAGE}:
        raise DoubaoProtocolError("unsupported Doubao V3 response message type")
    if flags not in {
        _FLAG_NO_SEQUENCE,
        _FLAG_POSITIVE_SEQUENCE,
        _FLAG_LAST_NO_SEQUENCE,
        _FLAG_NEGATIVE_SEQUENCE,
        _FLAG_WITH_EVENT,
    }:
        raise DoubaoProtocolError("unsupported Doubao V3 response flags")
    if serialization not in {0, 1}:
        raise DoubaoProtocolError("unsupported Doubao V3 serialization")
    if compression not in {_COMPRESSION_NONE, _COMPRESSION_GZIP}:
        raise DoubaoProtocolError("unsupported Doubao V3 compression")

    cursor = header_size
    sequence: int | None = None
    event: int | None = None
    session_id: str | None = None
    connect_id: str | None = None
    error_code: int | None = None

    if flags in {_FLAG_POSITIVE_SEQUENCE, _FLAG_NEGATIVE_SEQUENCE}:
        sequence, cursor = _read_int32(frame, cursor, "sequence")

    if message_type == _ERROR_MESSAGE:
        error_code, cursor = _read_uint32(frame, cursor, "error code")
    elif flags == _FLAG_WITH_EVENT:
        event, cursor = _read_int32(frame, cursor, "event")
        if event not in _CONNECTION_EVENTS:
            session_id, cursor = _read_text(frame, cursor, "session id")
        else:
            connect_id, cursor = _read_text(frame, cursor, "connect id")

    payload_size, cursor = _read_uint32(frame, cursor, "payload length")
    payload, cursor = _read_bytes(frame, cursor, payload_size, "payload")
    if cursor != len(frame):
        raise DoubaoProtocolError("unexpected bytes after the V3 response payload")

    if compression == _COMPRESSION_GZIP:
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            raise DoubaoProtocolError("invalid gzip V3 response payload") from exc

    error_message = None
    if error_code is not None:
        error_message = payload.decode("utf-8", errors="replace")

    return DoubaoResponseFrame(
        message_type=message_type,
        flags=flags,
        payload=b"" if error_code is not None else payload,
        event=event,
        session_id=session_id,
        connect_id=connect_id,
        sequence=sequence,
        is_final=event == _EVENT_SESSION_FINISHED or error_code is not None,
        error_code=error_code,
        error_message=error_message,
    )


class TTSService:
    """Synthesize persisted AI turns without affecting assessment control flow."""

    def __init__(
        self,
        config: TTSConfig | None = None,
        *,
        connect_factory: Callable[..., Any] | None = None,
        request_id_factory: Callable[[], str] | None = None,
        connect_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config or TTSConfig.from_app_settings()
        self._connect_factory = connect_factory
        self._request_id_factory = request_id_factory or (lambda: str(uuid.uuid4()))
        self._connect_id_factory = connect_id_factory or (lambda: str(uuid.uuid4()))

    async def synthesize(self, turn: PersistedAITurn) -> TTSSynthesisResult:
        """Return MP3 bytes or a stable browser-speech fallback instruction."""

        if not isinstance(turn, PersistedAITurn):
            raise InvalidPersistedAITurnError(
                "TTSService.synthesize requires a PersistedAITurn, not raw text"
            )
        turn.__post_init__()
        if len(turn.content) > self.config.max_text_chars:
            return self._fallback("text_too_long")

        if self.config.mode == "disabled":
            return self._fallback("tts_disabled", provider="disabled")
        if self.config.mode == "fake":
            return TTSSynthesisResult(
                audio=_FAKE_MP3_BYTES,
                content_type=MP3_CONTENT_TYPE,
                provider="fake",
                fallback_required=False,
            )

        if not all(
            (
                self.config.api_key.strip(),
                self.config.resource_id.strip(),
                self.config.speaker.strip(),
            )
        ):
            return self._fallback("credentials_missing")
        if self.config.resource_id not in DOUBAO_V3_RESOURCE_IDS:
            return self._fallback("configuration_invalid")

        for attempt in range(self.config.max_attempts):
            request_id = self._request_id_factory()
            connect_id = self._connect_id_factory()
            try:
                audio = await self._synthesize_doubao(
                    turn.content,
                    request_id=request_id,
                    connect_id=connect_id,
                )
            except DoubaoProviderError as exc:
                # Explicit provider rejection is deterministic and must not be retried.
                return self._fallback(
                    "provider_rejected_request",
                    request_id=request_id,
                    provider_error_code=exc.code,
                )
            except DoubaoProtocolError:
                # A malformed stream is not safe to replay automatically.
                return self._fallback("provider_protocol_error", request_id=request_id)
            except (asyncio.TimeoutError, TimeoutError):
                if attempt + 1 >= self.config.max_attempts:
                    return self._fallback("provider_timeout", request_id=request_id)
            except Exception:
                # Network/client exceptions get one bounded retry. Exception text and
                # request headers never leave this boundary or enter the database.
                if attempt + 1 >= self.config.max_attempts:
                    return self._fallback("provider_unavailable", request_id=request_id)
            else:
                return TTSSynthesisResult(
                    audio=audio,
                    content_type=MP3_CONTENT_TYPE,
                    provider="doubao",
                    fallback_required=False,
                    request_id=request_id,
                )

            if self.config.retry_delay_seconds:
                await asyncio.sleep(self.config.retry_delay_seconds)

        raise RuntimeError("unreachable TTS retry state")

    async def _synthesize_doubao(
        self,
        ai_text: str,
        *,
        request_id: str,
        connect_id: str,
    ) -> bytes:
        frame = build_doubao_request_frame(config=self.config, ai_text=ai_text)
        headers = {
            "X-Api-Key": self.config.api_key,
            "X-Api-Resource-Id": self.config.resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Connect-Id": connect_id,
            "X-Control-Require-Usage-Tokens-Return": "*",
        }
        connect = self._connect_factory or self._default_connect_factory()
        deadline = asyncio.get_running_loop().time() + self.config.timeout_seconds
        connection = connect(
            self.config.endpoint,
            additional_headers=headers,
            max_size=self.config.max_audio_bytes,
            proxy=None,
            ping_interval=None,
            open_timeout=self.config.timeout_seconds,
            close_timeout=min(self.config.timeout_seconds, 2.0),
        )

        audio = bytearray()
        saw_session_finished = False
        provider_session_id: str | None = None
        async with connection as websocket:
            await asyncio.wait_for(websocket.send(frame), timeout=self._remaining(deadline))
            while not saw_session_finished:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=self._remaining(deadline),
                )
                if not isinstance(raw, (bytes, bytearray, memoryview)):
                    raise DoubaoProtocolError("provider returned a text WebSocket frame")
                parsed = parse_doubao_response_frame(raw)
                if parsed.error_code is not None:
                    raise DoubaoProviderError(
                        parsed.error_code,
                        parsed.error_message or "unknown provider error",
                    )

                if parsed.session_id:
                    if provider_session_id is None:
                        provider_session_id = parsed.session_id
                    elif parsed.session_id != provider_session_id:
                        raise DoubaoProtocolError("provider session id changed mid-stream")

                if parsed.message_type == _AUDIO_ONLY_SERVER:
                    if parsed.event != _EVENT_TTS_RESPONSE:
                        raise DoubaoProtocolError("unexpected V3 audio event")
                    if parsed.payload:
                        if len(audio) + len(parsed.payload) > self.config.max_audio_bytes:
                            raise DoubaoProtocolError("V3 audio exceeds configured size limit")
                        audio.extend(parsed.payload)
                elif parsed.message_type == _FULL_SERVER_RESPONSE:
                    if parsed.event not in {
                        _EVENT_TTS_SENTENCE_START,
                        _EVENT_TTS_SENTENCE_END,
                        _EVENT_TTS_SUBTITLE,
                        _EVENT_SESSION_FINISHED,
                        _EVENT_USAGE_RESPONSE,
                    }:
                        raise DoubaoProtocolError("unexpected V3 server event")
                    saw_session_finished = parsed.event == _EVENT_SESSION_FINISHED

        if not saw_session_finished or not audio:
            raise DoubaoProtocolError("provider stream ended without final MP3 audio")
        return bytes(audio)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    @staticmethod
    def _default_connect_factory() -> Callable[..., Any]:
        # Lazy import keeps fake/disabled local acceptance independent from the
        # optional provider network path.
        from websockets.asyncio.client import connect

        return connect

    @staticmethod
    def _fallback(
        reason: str,
        *,
        provider: Literal["doubao", "disabled"] = "doubao",
        request_id: str | None = None,
        provider_error_code: int | None = None,
    ) -> TTSSynthesisResult:
        return TTSSynthesisResult(
            audio=None,
            content_type=MP3_CONTENT_TYPE,
            provider=provider,
            fallback_required=True,
            fallback_reason=reason,
            request_id=request_id,
            provider_error_code=provider_error_code,
        )


__all__ = [
    "DEFAULT_DOUBAO_INTERVIEW_CONTEXT",
    "DEFAULT_DOUBAO_INTERVIEWER_SPEAKER",
    "DOUBAO_V3_RESOURCE_IDS",
    "DOUBAO_V3_WEBSOCKET_URL",
    "DoubaoProtocolError",
    "DoubaoProviderError",
    "DoubaoResponseFrame",
    "InvalidPersistedAITurnError",
    "MP3_CONTENT_TYPE",
    "PersistedAITurn",
    "TTSConfig",
    "TTSService",
    "TTSSynthesisResult",
    "build_doubao_request_frame",
    "parse_doubao_response_frame",
]
