from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from app.services.tts_service import (
    DOUBAO_V3_WEBSOCKET_URL,
    DoubaoProtocolError,
    InvalidPersistedAITurnError,
    PersistedAITurn,
    TTSConfig,
    TTSService,
    build_doubao_request_frame,
    parse_doubao_response_frame,
)


FULL_SERVER_RESPONSE = 0x9
AUDIO_ONLY_SERVER = 0xB
ERROR_MESSAGE = 0xF
WITH_EVENT = 0x4
TTS_SENTENCE_START = 350
TTS_SENTENCE_END = 351
TTS_RESPONSE = 352
SESSION_FINISHED = 152


def ai_turn(*, content: str = "我先确认相关方的约束。") -> PersistedAITurn:
    return PersistedAITurn(
        id=41,
        session_uuid="session-verified-in-database",
        turn_index=7,
        role="assistant",
        content=content,
        persisted=True,
    )


def event_frame(
    message_type: int,
    event: int,
    payload: bytes = b"",
    *,
    session_id: str = "provider-session",
    compression: int = 0,
) -> bytes:
    encoded_payload = gzip.compress(payload, mtime=0) if compression == 1 else payload
    encoded_session = session_id.encode("utf-8")
    return (
        bytes((0x11, (message_type << 4) | WITH_EVENT, 0x10 | compression, 0x00))
        + event.to_bytes(4, "big", signed=True)
        + len(encoded_session).to_bytes(4, "big")
        + encoded_session
        + len(encoded_payload).to_bytes(4, "big")
        + encoded_payload
    )


def error_frame(code: int, message: str, *, compressed: bool = False) -> bytes:
    payload = message.encode("utf-8")
    compression = 1 if compressed else 0
    if compressed:
        payload = gzip.compress(payload, mtime=0)
    return (
        bytes((0x11, ERROR_MESSAGE << 4, 0x10 | compression, 0x00))
        + code.to_bytes(4, "big")
        + len(payload).to_bytes(4, "big")
        + payload
    )


class FakeWebSocket:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = list(messages)
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def recv(self) -> Any:
        if not self.messages:
            raise RuntimeError("test stream exhausted")
        return self.messages.pop(0)


class HangingWebSocket(FakeWebSocket):
    async def recv(self) -> bytes:
        await asyncio.Future()
        raise AssertionError("unreachable")


class FakeConnection:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *args: Any) -> None:
        return None


class FailingConnection:
    async def __aenter__(self) -> FakeWebSocket:
        raise OSError("transient test connection failure")

    async def __aexit__(self, *args: Any) -> None:
        return None


class CapturingConnectFactory:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, endpoint: str, **kwargs: Any) -> FakeConnection:
        self.calls.append((endpoint, kwargs))
        return FakeConnection(self.websocket)


class SequenceConnectFactory:
    def __init__(self, connections: list[Any]) -> None:
        self.connections = list(connections)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, endpoint: str, **kwargs: Any) -> Any:
        self.calls.append((endpoint, kwargs))
        return self.connections.pop(0)


def doubao_config(**overrides: Any) -> TTSConfig:
    values: dict[str, Any] = {
        "mode": "doubao",
        "api_key": "secret-new-api-key",
        "resource_id": "seed-tts-2.0",
        "speaker": "test-speaker",
        "timeout_seconds": 0.5,
    }
    values.update(overrides)
    return TTSConfig(**values)


def decode_request(frame: bytes) -> dict[str, Any]:
    assert frame[:4] == bytes((0x11, 0x10, 0x10, 0x00))
    payload_size = int.from_bytes(frame[4:8], "big")
    assert payload_size == len(frame[8:])
    return json.loads(frame[8:].decode("utf-8"))


def success_messages(*, audio: bytes = b"mp3-audio") -> list[bytes]:
    return [
        event_frame(FULL_SERVER_RESPONSE, TTS_SENTENCE_START, b'{"text":""}'),
        event_frame(AUDIO_ONLY_SERVER, TTS_RESPONSE, audio[:4]),
        event_frame(AUDIO_ONLY_SERVER, TTS_RESPONSE, audio[4:]),
        event_frame(FULL_SERVER_RESPONSE, TTS_SENTENCE_END, b'{"text":"done"}'),
        event_frame(FULL_SERVER_RESPONSE, SESSION_FINISHED, b'{"usage":{"text_words":9}}'),
    ]


def test_v3_config_hides_api_key_and_restricts_provider_endpoint() -> None:
    config = doubao_config()
    assert "secret-new-api-key" not in repr(config)
    with pytest.raises(ValueError, match="official V3"):
        doubao_config(endpoint="wss://example.invalid/steal-key")


def test_build_request_matches_v3_protocol_and_keeps_headers_out_of_json() -> None:
    config = doubao_config()
    frame = build_doubao_request_frame(
        config=config,
        ai_text="这是已持久化的访谈官文本。",
    )

    payload = decode_request(frame)
    assert payload == {
        "req_params": {
            "speaker": "test-speaker",
            "text": "这是已持久化的访谈官文本。",
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "bit_rate": 128000,
                "speech_rate": -5,
            },
            "context_texts": [config.context_text],
        }
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "secret-new-api-key" not in serialized
    assert "seed-tts-2.0" not in serialized
    assert "session-verified-in-database" not in serialized


def test_clone_resource_omits_unsupported_context_instruction() -> None:
    config = doubao_config(resource_id="seed-icl-2.0")
    payload = decode_request(build_doubao_request_frame(config=config, ai_text="复刻音色文本"))

    assert "context_texts" not in payload["req_params"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"speech_rate": -51},
        {"speech_rate": 101},
        {"bit_rate": 63_999},
        {"bit_rate": 160_001},
        {"max_attempts": 3},
        {"retry_delay_seconds": 2.1},
    ],
)
def test_v3_natural_voice_and_retry_config_is_bounded(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        doubao_config(**overrides)


def test_parse_v3_event_audio_and_final_frames() -> None:
    started = parse_doubao_response_frame(
        event_frame(FULL_SERVER_RESPONSE, TTS_SENTENCE_START, b"{}")
    )
    audio = parse_doubao_response_frame(
        event_frame(AUDIO_ONLY_SERVER, TTS_RESPONSE, b"first")
    )
    final = parse_doubao_response_frame(
        event_frame(FULL_SERVER_RESPONSE, SESSION_FINISHED, b'{"usage":{}}')
    )

    assert started.event == TTS_SENTENCE_START and not started.is_final
    assert audio.event == TTS_RESPONSE and audio.payload == b"first"
    assert audio.session_id == "provider-session" and not audio.is_final
    assert final.event == SESSION_FINISHED and final.is_final


def test_parse_error_frame_decodes_payload_without_exposing_it_as_audio() -> None:
    parsed = parse_doubao_response_frame(
        error_frame(45000000, "invalid private provider detail", compressed=True)
    )

    assert parsed.error_code == 45000000
    assert parsed.error_message == "invalid private provider detail"
    assert parsed.payload == b""
    assert parsed.is_final


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        bytes((0x21, 0x94, 0x10, 0x00)),
        event_frame(AUDIO_ONLY_SERVER, TTS_RESPONSE, b"abc")[:-1],
        bytes((0x11, 0xD0, 0x10, 0x00)),
        bytes((0x11, 0x95, 0x10, 0x00)),
    ],
)
def test_malformed_or_unsupported_v3_frames_fail_closed(frame: bytes) -> None:
    with pytest.raises(DoubaoProtocolError):
        parse_doubao_response_frame(frame)


def test_fake_mode_is_deterministic_and_does_not_touch_the_filesystem(tmp_path: Path) -> None:
    before = list(tmp_path.iterdir())
    service = TTSService(TTSConfig(mode="fake"))

    first = asyncio.run(service.synthesize(ai_turn()))
    second = asyncio.run(service.synthesize(ai_turn()))

    assert first.ok and second.ok
    assert first.provider == "fake"
    assert first.content_type == "audio/mpeg"
    assert first.audio == second.audio
    assert first.audio is not None and first.audio.startswith(b"ID3")
    assert list(tmp_path.iterdir()) == before


def test_service_rejects_raw_text_and_non_assistant_or_unpersisted_turns() -> None:
    service = TTSService(TTSConfig(mode="fake"))
    with pytest.raises(InvalidPersistedAITurnError):
        asyncio.run(service.synthesize("user answer"))  # type: ignore[arg-type]

    with pytest.raises(InvalidPersistedAITurnError):
        PersistedAITurn(
            id=1,
            session_uuid="session",
            turn_index=1,
            role="user",  # type: ignore[arg-type]
            content="这是用户答案，绝不能发送给 TTS。",
            persisted=True,
        )
    with pytest.raises(InvalidPersistedAITurnError):
        PersistedAITurn(
            id=1,
            session_uuid="session",
            turn_index=1,
            role="assistant",
            content="not persisted",
            persisted=False,  # type: ignore[arg-type]
        )


def test_doubao_v3_combines_audio_and_uses_new_api_key_headers_only() -> None:
    websocket = FakeWebSocket(success_messages())
    connect = CapturingConnectFactory(websocket)
    service = TTSService(
        doubao_config(),
        connect_factory=connect,
        request_id_factory=lambda: "request-123",
        connect_id_factory=lambda: "connect-456",
    )

    result = asyncio.run(service.synthesize(ai_turn()))

    assert result.ok
    assert result.audio == b"mp3-audio"
    assert result.provider == "doubao"
    assert result.request_id == "request-123"
    assert connect.calls[0][0] == DOUBAO_V3_WEBSOCKET_URL
    headers = connect.calls[0][1]["additional_headers"]
    assert headers == {
        "X-Api-Key": "secret-new-api-key",
        "X-Api-Resource-Id": "seed-tts-2.0",
        "X-Api-Request-Id": "request-123",
        "X-Api-Connect-Id": "connect-456",
        "X-Control-Require-Usage-Tokens-Return": "*",
    }
    assert "Authorization" not in headers
    assert connect.calls[0][1]["max_size"] == 10 * 1024 * 1024
    assert connect.calls[0][1]["proxy"] is None
    assert len(websocket.sent) == 1
    assert decode_request(websocket.sent[0])["req_params"]["text"] == ai_turn().content


def test_transient_network_failure_gets_one_bounded_retry() -> None:
    second_websocket = FakeWebSocket(success_messages())
    connect = SequenceConnectFactory(
        [FailingConnection(), FakeConnection(second_websocket)]
    )
    request_ids = iter(["request-first", "request-second"])
    connect_ids = iter(["connect-first", "connect-second"])
    service = TTSService(
        doubao_config(retry_delay_seconds=0),
        connect_factory=connect,
        request_id_factory=lambda: next(request_ids),
        connect_id_factory=lambda: next(connect_ids),
    )

    result = asyncio.run(service.synthesize(ai_turn()))

    assert result.ok
    assert result.request_id == "request-second"
    assert len(connect.calls) == 2
    assert decode_request(second_websocket.sent[0])["req_params"]["text"] == ai_turn().content


def test_provider_error_becomes_stable_browser_fallback() -> None:
    websocket = FakeWebSocket(
        [error_frame(45000000, "request rejected with private detail")]
    )
    connect = CapturingConnectFactory(websocket)
    service = TTSService(
        doubao_config(),
        connect_factory=connect,
        request_id_factory=lambda: "rejected-request",
    )

    result = asyncio.run(service.synthesize(ai_turn()))

    assert not result.ok
    assert result.audio is None
    assert result.fallback_required
    assert result.fallback_reason == "provider_rejected_request"
    assert result.provider_error_code == 45000000
    assert "private detail" not in repr(result)
    assert "secret-new-api-key" not in repr(result)
    assert len(connect.calls) == 1


def test_timeout_becomes_browser_fallback_without_escaping() -> None:
    connect = CapturingConnectFactory(HangingWebSocket([]))
    service = TTSService(
        doubao_config(timeout_seconds=0.01, retry_delay_seconds=0),
        connect_factory=connect,
        request_id_factory=lambda: "timeout-request",
    )

    result = asyncio.run(service.synthesize(ai_turn()))

    assert result.audio is None
    assert result.fallback_required
    assert result.fallback_reason == "provider_timeout"
    assert result.request_id == "timeout-request"
    assert len(connect.calls) == 2


@pytest.mark.parametrize(
    ("config", "reason", "provider"),
    [
        (TTSConfig(mode="disabled"), "tts_disabled", "disabled"),
        (TTSConfig(mode="doubao"), "credentials_missing", "doubao"),
        (doubao_config(resource_id="legacy-resource"), "configuration_invalid", "doubao"),
        (doubao_config(max_text_chars=3), "text_too_long", "doubao"),
    ],
)
def test_non_available_modes_and_oversized_text_return_fallback(
    config: TTSConfig,
    reason: str,
    provider: str,
) -> None:
    result = asyncio.run(TTSService(config).synthesize(ai_turn(content="four")))

    assert result.audio is None
    assert result.fallback_required
    assert result.fallback_reason == reason
    assert result.provider == provider


@pytest.mark.parametrize(
    "messages",
    [
        [b"bad"],
        [event_frame(FULL_SERVER_RESPONSE, SESSION_FINISHED, b"{}")],
        [event_frame(AUDIO_ONLY_SERVER, TTS_SENTENCE_START, b"not-audio")],
        [event_frame(FULL_SERVER_RESPONSE, 999, b"{}")],
        [event_frame(AUDIO_ONLY_SERVER, TTS_RESPONSE, b"12345"), event_frame(FULL_SERVER_RESPONSE, SESSION_FINISHED, b"{}")],
        [
            event_frame(AUDIO_ONLY_SERVER, TTS_RESPONSE, b"a", session_id="session-a"),
            event_frame(FULL_SERVER_RESPONSE, SESSION_FINISHED, b"{}", session_id="session-b"),
        ],
        ["provider text frame"],
    ],
)
def test_bad_incomplete_or_oversized_provider_stream_becomes_protocol_fallback(
    messages: list[Any],
) -> None:
    config = doubao_config(max_audio_bytes=4)
    service = TTSService(
        config,
        connect_factory=CapturingConnectFactory(FakeWebSocket(messages)),
    )

    result = asyncio.run(service.synthesize(ai_turn()))

    assert result.fallback_required
    assert result.fallback_reason == "provider_protocol_error"
