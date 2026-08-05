#!/usr/bin/env python3
"""Sealed real-model UAT for the V6.1 40-answer interview protocol.

The participant side is a deterministic simulator assembled exclusively from
the human-authored scenario facts and literal response templates in this file.
It never asks DeepSeek (or another model) to role-play a participant.

The runner is deliberately local-only.  It never starts a server, reads a
``.env`` file, or sends credentials.  The operator must start a V6 API in real
mode against a disposable local SQLite database, then explicitly confirm this
billable run and provide a new repository-external private output directory.

Example (do not run until the zero-call gate has passed)::

    backend/.venv/bin/python backend/scripts/check_real_deepseek_v6.py \
      --confirmation RUN_V6_1_40_ROUND_REAL_UAT \
      --base-url http://127.0.0.1:8060/api/v1 \
      --db-path /absolute/path/to/disposable-v6-uat.db \
      --budget-ledger-dir /absolute/private/path/v6_1_uat_budget_ledger \
      --output-dir /absolute/private/path/v6_1_real_uat_<run-id>

Normal execution is about 266 logical provider calls.  The script observes
physical attempts from AgentTrace/ScoringRun provenance, warns at 360, pauses
at 600 unless a second explicit confirmation is supplied, and never permits a
call once the 960 hard limit could be crossed.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.domain.interview_protocol import is_bounded_clarification_request


CONFIRMATION = "RUN_V6_1_40_ROUND_REAL_UAT"
PAUSE_CONFIRMATION = "ALLOW_V6_1_REAL_UAT_AFTER_600"
SETTLEMENT_CONFIRMATION = "SETTLE_RESOLVED_V6_1_HISTORY_AT_OBSERVED_ATTEMPTS"
EXPECTED_MODEL = "deepseek-v4-flash"
EXPECTED_PROTOCOL = "natural_interviewer_v6.1"
CONSENT_VERSION = "v6-research-pilot-2026-08"
MIN_VALID_ANSWERS = 40
MAX_VALID_ANSWERS = 45
SOFT_REQUEST_WARNING = 360
PAUSE_REQUEST_LIMIT = 600
HARD_REQUEST_LIMIT = 960
SETTLEMENT_EXPECTED_RESERVATION_COUNT = 71
SETTLEMENT_EXPECTED_HISTORICAL_RESERVED = 213
SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED = 75
SETTLEMENT_SCHEMA = "v6.1-budget-ledger-settlement-v1"
ABORT_SETTLEMENT_CONFIRMATION = (
    "SETTLE_V6_1_ABORTED_DISK_FULL_AT_CONSERVATIVE_CARRY_138"
)
ABORT_SETTLEMENT_SCHEMA = "v6.1-budget-ledger-aborted-run-settlement-v3"
ABORT_FAILURE_REASON = "aborted_disk_full"
ABORT_CONSERVATIVE_CARRY = 138
ABORT_SOURCE_OBSERVED = 96
ABORT_SOURCE_RESERVED = 138
ABORT_SOURCE_NEW_RESERVATIONS = 21
ABORT_SOURCE_NEW_PROVIDER_RECORDS = 20
ACCIDENTAL_CHECKPOINT_BEFORE = {
    "database": {
        "sha256": "fccb9737d4d43c96d168b0b525fd6a6826b2efe8dfab20be09707851795ad47a",
        "size": 196608,
    },
    "wal": {
        "sha256": "7e57eaf3de12dc2c6ca5dd5f89d5664bd610df6e2dbd2cbaca0ebb3f68c996cd",
        "size": 2068296,
    },
    "shm": {
        "sha256": "a5f167cd42cb22c736e92078e7e56b4d40ba95a75554d3868603e9bd118ca5dc",
        "size": 32768,
    },
}
ACCIDENTAL_CHECKPOINT_AFTER = {
    "database": {
        "sha256": "60067ebac5514de75dbca0dc3eeb3e016ff10b6bd992aeff30f2477f168582f8",
        "size": 507904,
    },
    "wal": {
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "size": 0,
    },
    "shm": {
        "sha256": "fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb",
        "size": 32768,
    },
}
REPO_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_VISIBLE = (
    "coverage",
    "candidate_questions",
    "target_dimension",
    "评分",
    "测评维度",
    "系统提示",
    "prompt",
    "你应该",
    "建议你",
)

INTENT_ORDER = (
    "fact",
    "evidence",
    "reason",
    "stakeholder",
    "tradeoff",
    "uncertainty",
    "update",
    "reflection",
    "decision",
)

INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "evidence": ("依据", "证据", "记录", "数据", "来源", "核实", "可信", "验证"),
    "stakeholder": ("谁", "他人", "成员", "家人", "同事", "参与者", "影响哪些人"),
    "tradeoff": ("权衡", "取舍", "优先", "比较", "冲突", "风险", "收益", "代价"),
    "uncertainty": ("不确定", "反例", "例外", "假设", "改变", "可能", "如果", "不成立"),
    "update": ("新信息", "变化", "调整", "后来", "现在", "下一步", "反馈"),
    "reflection": ("怎么看", "感受", "意味着", "理解", "学到", "反思", "意识到"),
    "decision": ("决定", "选择", "行动", "打算", "结论", "准备怎么做"),
    "reason": ("为什么", "理由", "原因", "判断", "考虑", "在意"),
    "fact": ("什么", "情况", "具体", "经过", "时间", "背景", "发生"),
}

RESPONSE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "fact": (
        "目前我能确认的事实是：{fact}；这是我现在理解情况的起点。",
        "把情况说具体一点，{fact}；我暂时没有把它当成全部原因。",
        "我先补充一条背景：{fact}；这与我面临的决定直接相关。",
        "如果只看已知情况，{fact}；其他细节我还在逐项确认。",
        "这件事里最明确的一点是：{fact}；我愿意再解释它的前后关系。",
    ),
    "evidence": (
        "我现在能核实的依据是：{fact}；这只是现有记录，不足以单独证明全部原因。",
        "这个判断不是只凭感觉，因为{fact}；但我还需要检查来源是否完整。",
        "作为可追溯的信息，{fact}；我会把它和另一个独立来源交叉核对。",
        "我会先把事实和推测分开：{fact}；关于这意味着什么，还需要额外证据。",
        "当前最值得检验的材料是：{fact}；如果它被后续信息否定，我会修正判断。",
    ),
    "reason": (
        "我之所以在意这一点，是因为{fact}；它会改变我对问题边界的理解。",
        "我的理由是：{fact}；如果忽略它，很可能会得到过度简化的结论。",
        "这背后的考虑是：{fact}；所以我希望先弄清原因再采取行动。",
        "我会把它列为关键原因，因为{fact}；但我也会保留对其他解释的空间。",
        "我当前的判断逻辑是：{fact}；这一点同时影响我对风险和收益的看法。",
    ),
    "stakeholder": (
        "从相关人的角度看，{fact}；我需要区分谁直接受影响，谁只是表达意见。",
        "这不只影响我自己，因为{fact}；各方承担的责任和后果并不完全一样。",
        "我会先听完不同人的说法，其中{fact}；然后再看哪些关切有事实支持。",
        "需要被纳入判断的一方是与这条信息相关的人：{fact}；我不想代替他们做假设。",
        "我看到的角度差异是：{fact}；这使我需要同时考虑结果和程序公平。",
    ),
    "tradeoff": (
        "我正在权衡的是：{fact}；它会同时影响进度与质量，不能只看其中一面。",
        "这个取舍的代价在于：{fact}；我会比较短期收益和后续可逆性。",
        "如果必须排序，我会先看{fact}；然后再评估是否有折中或分阶段方案。",
        "我不想把它变成简单的二选一，因为{fact}；真正的限制条件需要先说清。",
        "当前最难的权衡是：{fact}；我会优先避免不可逆的损失，再谈效率。",
    ),
    "uncertainty": (
        "我还不确定的地方是：{fact}；这意味着我需要给当前结论保留修正空间。",
        "一个可能推翻我判断的情况是：{fact}；如果真是这样，原先的方案就不够稳妥。",
        "我暂时只能把它当成假设，因为{fact}；在验证之前不会把它当成确定事实。",
        "这里存在一个例外或反例：{fact}；它提醒我不能只依靠之前的经验外推。",
        "如果后续发现{fact}，我会重新检查自己的前提，而不是勉强维持原结论。",
    ),
    "update": (
        "新出现的信息是：{fact}；它使我需要重新排列之前的优先顺序。",
        "和最初相比，现在{fact}；我会先确认这个变化是否稳定再调整。",
        "我根据反馈补充一点：{fact}；这会改变下一步的验证顺序。",
        "我的调整不是随意变化，而是因为{fact}；这条信息触发了新的限制。",
        "目前最需要纳入的更新是：{fact}；我会记录它如何改变了决策路径。",
    ),
    "reflection": (
        "回看我刚才的想法，{fact}；我意识到自己之前对这一点估计得过于简单。",
        "这个过程让我更清楚：{fact}；我需要把感受、事实和最终决定分开。",
        "我现在对问题的理解是：{fact}；这比我最初只看单一结果更完整。",
        "我愿意修正前面的表达，因为{fact}；现在我会给不确定性更合理的位置。",
        "如果要概括我学到的一点，那就是{fact}；这会影响我以后面对类似决定的方式。",
    ),
    "decision": (
        "基于目前信息，我的暂定决定是：{fact}；在执行前我会再做一次关键依据检查。",
        "我准备把下一步定为：{fact}；同时保留一个可逆的停止条件。",
        "如果现在必须选择，我会根据{fact}来行动；后续证据改变时也允许调整。",
        "我形成的结论是：{fact}；它并不是永久不变，而是当前证据下的最稳妥选择。",
        "我会用一个可检验的行动收束：{fact}；然后依据结果而不是预设立场进一步判断。",
    ),
}


@dataclass(frozen=True)
class Clarification:
    after_valid_answer: int
    text: str


@dataclass(frozen=True)
class ScenarioSpec:
    case_id: str
    label: str
    participant: dict[str, str]
    first_answer: str
    facts: tuple[str, ...]
    target_valid_answers: int
    clarifications: tuple[Clarification, ...] = ()


@dataclass(frozen=True)
class PreparedAnswer:
    answer_id: str
    intent: str
    text: str


@dataclass
class FlowResult:
    case_id: str
    label: str
    session_uuid: str
    target_valid_answers: int
    valid_answer_count: int
    clarification_count: int
    close_origin: str
    close_provider: str
    closed_by_model: bool
    finish_reason: str | None
    scored_dimensions: int
    null_dimensions: int
    report_sha256: str
    pdf_sha256: str
    transcript_sha256: str
    total_tokens: int
    observed_physical_attempts: int


def _settlement_sha256(payload: dict[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("settlement_sha256", None)
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass
class BudgetLedger:
    """Conservative provider-call budget shared by every real-UAT rerun.

    Real execution supplies a repository-external ``ledger_dir``. Every
    application-level call reserves the gateway's full three-attempt envelope
    before network I/O, and reservations are intentionally never refunded.
    Therefore a timeout, crash, client retry, new output directory, or process
    restart cannot reset the 960 hard ceiling. Database provenance is tracked
    separately for the cumulative 360 warning and 600 operator pause.

    Omitting ``ledger_dir`` keeps this class in-memory for zero-call tests.
    """

    soft_limit: int = SOFT_REQUEST_WARNING
    pause_limit: int = PAUSE_REQUEST_LIMIT
    hard_limit: int = HARD_REQUEST_LIMIT
    allow_after_pause: bool = False
    ledger_dir: Path | None = None
    run_id: str = "memory-run"
    record_namespace: str = "memory-db"
    expected_settlement_sha256: str = ""
    observed_physical_attempts: int = 0
    reserved_physical_upper_bound: int = 0
    warned: bool = False
    settlement: dict[str, Any] | None = None
    _record_keys: set[tuple[str, str, int]] = field(default_factory=set)
    _runs: list[dict[str, Any]] = field(default_factory=list)
    _state_version: int = field(default=1, init=False, repr=False)
    _lock_handle: Any = field(default=None, init=False, repr=False)
    _state_path: Path | None = field(default=None, init=False, repr=False)
    _events_path: Path | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.ledger_dir is None:
            if self.expected_settlement_sha256:
                raise AssertionError("budget_ledger_v2_required")
            return
        self.ledger_dir = _ensure_private_budget_ledger_dir(self.ledger_dir)
        lock_path = self.ledger_dir / "budget.lock"
        self._lock_handle = lock_path.open("a+", encoding="utf-8")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(
                self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError as exc:
            self._lock_handle.close()
            self._lock_handle = None
            raise AssertionError("budget_ledger_is_locked_by_another_run") from exc
        self._state_path = self.ledger_dir / "budget_state.json"
        self._events_path = self.ledger_dir / "budget_events.jsonl"
        try:
            if self._state_path.exists():
                self._load_state()
            elif self.expected_settlement_sha256:
                raise AssertionError("budget_ledger_v2_required")
            if self.expected_settlement_sha256:
                if self._state_version not in {2, 3} or self.settlement is None:
                    raise AssertionError("budget_ledger_v2_required")
                source_namespaces = set(
                    self.settlement.get("source_database_namespaces") or []
                )
                if self.record_namespace in source_namespaces:
                    raise AssertionError("settlement_source_database_reuse")
            self._runs.append(
                {
                    "run_id": self.run_id,
                    "record_namespace": self.record_namespace,
                    "opened_at": _utc_timestamp(),
                }
            )
            self._persist("run_opened")
        except Exception:
            if self._lock_handle is not None:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                self._lock_handle.close()
                self._lock_handle = None
            raise

    def _load_state(self) -> None:
        assert self._state_path is not None
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssertionError("budget_ledger_state_unreadable") from exc
        version = int(payload.get("version") or 0)
        if version not in {1, 2, 3}:
            raise AssertionError(
                f"budget_ledger_contract_mismatch:version:{version}"
            )
        expected = {
            "expected_model": EXPECTED_MODEL,
            "expected_protocol": EXPECTED_PROTOCOL,
            "soft_limit": self.soft_limit,
            "pause_limit": self.pause_limit,
            "hard_limit": self.hard_limit,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise AssertionError(
                    f"budget_ledger_contract_mismatch:{key}:"
                    f"{payload.get(key)}!={value}"
                )
        self.observed_physical_attempts = int(
            payload.get("observed_physical_attempts") or 0
        )
        self.reserved_physical_upper_bound = int(
            payload.get("reserved_physical_upper_bound") or 0
        )
        self.warned = bool(payload.get("warned"))
        self._state_version = version
        self._record_keys = {
            (str(item[0]), str(item[1]), int(item[2]))
            for item in payload.get("record_keys", [])
        }
        self._runs = list(payload.get("runs") or [])
        if version in {2, 3}:
            settlement = payload.get("settlement")
            if not isinstance(settlement, dict):
                raise AssertionError("budget_ledger_settlement_missing")
            self.settlement = settlement
            self._validate_loaded_settlement()
        if self.observed_physical_attempts > self.hard_limit:
            raise AssertionError("budget_ledger_observed_total_above_hard_limit")
        if self.reserved_physical_upper_bound > self.hard_limit:
            raise AssertionError("budget_ledger_reserved_total_above_hard_limit")
        if (
            version in {2, 3}
            and self.reserved_physical_upper_bound < self.observed_physical_attempts
        ):
            raise AssertionError("budget_ledger_reserved_below_observed")

    @property
    def effective_committed_physical_upper_bound(self) -> int:
        return self.reserved_physical_upper_bound

    def _validate_loaded_settlement(self) -> None:
        assert self.settlement is not None
        schema = str(self.settlement.get("schema") or "")
        if schema == SETTLEMENT_SCHEMA:
            required = {
                "schema": SETTLEMENT_SCHEMA,
                "source_version": 1,
                "source_reserved_physical_upper_bound": (
                    SETTLEMENT_EXPECTED_HISTORICAL_RESERVED
                ),
                "source_observed_physical_attempts": (
                    SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
                ),
                "effective_initial_reserved_physical_upper_bound": (
                    SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
                ),
                "unresolved_reservation_count": 0,
                "verified_reservation_count": SETTLEMENT_EXPECTED_RESERVATION_COUNT,
                "verified_provider_record_count": SETTLEMENT_EXPECTED_RESERVATION_COUNT,
            }
            initial_reserved = SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
            expected_record_keys = SETTLEMENT_EXPECTED_RESERVATION_COUNT
        elif schema == ABORT_SETTLEMENT_SCHEMA:
            required = {
                "schema": ABORT_SETTLEMENT_SCHEMA,
                "source_version": 2,
                "failure_reason": ABORT_FAILURE_REASON,
                "source_reserved_physical_upper_bound": ABORT_SOURCE_RESERVED,
                "source_observed_physical_attempts": ABORT_SOURCE_OBSERVED,
                "effective_initial_reserved_physical_upper_bound": (
                    ABORT_CONSERVATIVE_CARRY
                ),
                "conservative_carry": ABORT_CONSERVATIVE_CARRY,
                "unresolved_reservation_count": 1,
                "aborted_run_reservation_count": ABORT_SOURCE_NEW_RESERVATIONS,
                "aborted_run_provider_record_count": (
                    ABORT_SOURCE_NEW_PROVIDER_RECORDS
                ),
                "accidental_checkpoint_before": ACCIDENTAL_CHECKPOINT_BEFORE,
                "accidental_checkpoint_after": ACCIDENTAL_CHECKPOINT_AFTER,
            }
            initial_reserved = ABORT_CONSERVATIVE_CARRY
            expected_record_keys = (
                SETTLEMENT_EXPECTED_RESERVATION_COUNT
                + ABORT_SOURCE_NEW_PROVIDER_RECORDS
            )
            if self._state_version != 3:
                raise AssertionError("budget_ledger_abort_settlement_requires_v3")
        else:
            raise AssertionError(f"budget_ledger_settlement_schema_unknown:{schema}")
        for key, value in required.items():
            if self.settlement.get(key) != value:
                raise AssertionError(
                    f"budget_ledger_settlement_mismatch:{key}:"
                    f"{self.settlement.get(key)}!={value}"
                )
        stored_sha = str(self.settlement.get("settlement_sha256") or "")
        calculated_sha = _settlement_sha256(self.settlement)
        if stored_sha != calculated_sha:
            raise AssertionError("budget_ledger_settlement_sha256_invalid")
        if (
            self.expected_settlement_sha256
            and stored_sha != self.expected_settlement_sha256
        ):
            raise AssertionError("budget_ledger_settlement_sha256_mismatch")
        if len(self._record_keys) != expected_record_keys:
            raise AssertionError("budget_ledger_settlement_record_keys_mismatch")
        if self.reserved_physical_upper_bound < initial_reserved:
            raise AssertionError("budget_ledger_settlement_effective_reserved_regressed")
        if (self.reserved_physical_upper_bound - initial_reserved) % 3:
            raise AssertionError("budget_ledger_settlement_reservation_alignment")

    def _state_payload(self) -> dict[str, Any]:
        payload = {
            "version": self._state_version,
            "expected_model": EXPECTED_MODEL,
            "expected_protocol": EXPECTED_PROTOCOL,
            "soft_limit": self.soft_limit,
            "pause_limit": self.pause_limit,
            "hard_limit": self.hard_limit,
            "observed_physical_attempts": self.observed_physical_attempts,
            "reserved_physical_upper_bound": self.reserved_physical_upper_bound,
            "effective_committed_physical_upper_bound": (
                self.effective_committed_physical_upper_bound
            ),
            "warned": self.warned,
            "record_keys": [list(item) for item in sorted(self._record_keys)],
            "runs": self._runs,
            "updated_at": _utc_timestamp(),
        }
        if self.settlement is not None:
            payload["settlement"] = self.settlement
        return payload

    def _persist(self, event: str, **details: Any) -> None:
        if self._state_path is None or self._events_path is None:
            return
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        _write_json(temporary, self._state_payload())
        os.replace(temporary, self._state_path)
        os.chmod(self._state_path, 0o600)
        event_payload = {
            "event": event,
            "at": _utc_timestamp(),
            "run_id": self.run_id,
            "record_namespace": self.record_namespace,
            "observed_physical_attempts": self.observed_physical_attempts,
            "reserved_physical_upper_bound": self.reserved_physical_upper_bound,
            "effective_committed_physical_upper_bound": (
                self.effective_committed_physical_upper_bound
            ),
            **details,
        }
        with self._events_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(event_payload, ensure_ascii=False, sort_keys=True) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(self._events_path, 0o600)

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self._state_version,
            "observed_physical_attempts": self.observed_physical_attempts,
            "reserved_physical_upper_bound": self.reserved_physical_upper_bound,
            "effective_committed_physical_upper_bound": (
                self.effective_committed_physical_upper_bound
            ),
            "soft_limit": self.soft_limit,
            "pause_limit": self.pause_limit,
            "hard_limit": self.hard_limit,
            "pause_override_confirmed": self.allow_after_pause,
            "settlement_sha256": (
                self.settlement.get("settlement_sha256")
                if self.settlement is not None
                else None
            ),
            "ledger_path_sha256": (
                _sha256_bytes(str(self.ledger_dir).encode("utf-8"))
                if self.ledger_dir is not None
                else None
            ),
        }

    def close(self, status: str) -> None:
        if self._lock_handle is None:
            return
        self._persist("run_closed", status=status)
        fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        self._lock_handle.close()
        self._lock_handle = None

    def ensure_run_capacity(self, planned_physical_max: int) -> None:
        if planned_physical_max < 0:
            raise AssertionError("planned_physical_max_must_be_non_negative")
        if self.reserved_physical_upper_bound + planned_physical_max > self.hard_limit:
            self._persist(
                "run_capacity_blocked",
                planned_physical_max=planned_physical_max,
            )
            raise AssertionError(
                "cumulative_budget_cannot_fit_complete_run:"
                f"{self.reserved_physical_upper_bound}+{planned_physical_max}>"
                f"{self.hard_limit}"
            )
        if (
            self.observed_physical_attempts >= self.pause_limit
            and not self.allow_after_pause
        ):
            self._persist("run_pause_blocked")
            raise AssertionError(
                "request_budget_paused_at_600: review the persistent ledger and "
                f"supply --pause-confirmation {PAUSE_CONFIRMATION}"
            )

    def ensure_can_call(self, label: str) -> None:
        if self.reserved_physical_upper_bound + 3 > self.hard_limit:
            self._persist("hard_limit_blocked", label=label)
            raise AssertionError(
                f"hard_request_budget_would_be_exceeded:{label}:"
                f"{self.reserved_physical_upper_bound}+3>{self.hard_limit}"
            )
        if (
            self.observed_physical_attempts >= self.pause_limit
            and not self.allow_after_pause
        ):
            self._persist("pause_blocked", label=label)
            raise AssertionError(
                "request_budget_paused_at_600: rerun only after reviewing the audit "
                f"and supplying --pause-confirmation {PAUSE_CONFIRMATION}"
            )
        # One application-level model request can consume one initial provider
        # attempt, one transport retry and one structured-output repair.  Reserve
        # all three before sending so an ambiguous client disconnect can never
        # make the sealed runner cross the hard cap even when the local database
        # has not yet committed its trace row.
        self.reserved_physical_upper_bound += 3
        self._persist("call_reserved", label=label, reserved_attempts=3)

    def observe(self, records: Iterable[dict[str, Any]]) -> None:
        for record in records:
            table = str(record["record_type"])
            record_id = int(record["id"])
            key = (self.record_namespace, table, record_id)
            if key in self._record_keys or not _record_invoked_provider(record):
                continue
            self._record_keys.add(key)
            attempts = 1 + int(record.get("transport_retry_count") or 0)
            attempts += 1 if bool(record.get("repair_used")) else 0
            self.observed_physical_attempts += attempts
        self._persist("audit_observed")
        if self.observed_physical_attempts > self.hard_limit:
            raise AssertionError(
                f"hard_request_budget_exceeded:{self.observed_physical_attempts}"
            )
        if (
            self.observed_physical_attempts >= self.pause_limit
            and not self.allow_after_pause
        ):
            raise AssertionError(
                f"request_budget_paused_at_600:{self.observed_physical_attempts}"
            )
        if self.observed_physical_attempts >= self.soft_limit and not self.warned:
            print(
                "V6.1 UAT budget warning: observed physical provider attempts "
                f"reached {self.observed_physical_attempts} (soft limit {self.soft_limit}).",
                file=sys.stderr,
            )
            self.warned = True
            self._persist("budget_warning_emitted")


class ResponseSelector:
    """Choose only from the frozen, deterministic participant answer bank."""

    def __init__(self, answers: Sequence[PreparedAnswer]) -> None:
        if not answers:
            raise ValueError("answer bank must not be empty")
        self._answers = tuple(answers)
        self._used: set[str] = set()

    def select(self, interviewer_message: str) -> PreparedAnswer:
        if not self._used:
            answer = self._answers[0]
            self._used.add(answer.answer_id)
            return answer
        intent = classify_question_intent(interviewer_message)
        answer = next(
            (
                item
                for item in self._answers
                if item.answer_id not in self._used and item.intent == intent
            ),
            None,
        )
        if answer is None:
            answer = next(
                (item for item in self._answers if item.answer_id not in self._used),
                None,
            )
        if answer is None:
            raise AssertionError("deterministic_answer_bank_exhausted")
        self._used.add(answer.answer_id)
        return answer


def scenario_specs() -> tuple[ScenarioSpec, ...]:
    """Six independent, non-personal synthetic scenario cards."""

    return (
        ScenarioSpec(
            case_id="UAT-01",
            label="大学生小组项目分工与延期",
            participant={
                "display_name": "合成参与者01",
                "identity_type": "student",
                "occupation": "本科生",
                "experience_level": "二年级",
                "collaboration_role": "课程小组协调者",
            },
            first_answer="我想聊聊小组分工。",
            target_valid_answers=40,
            facts=(
                "项目要在五天后提交，但现在只有一份不完整的任务表",
                "四名成员对谁负责数据检查有两种不同说法",
                "过去十次类似任务中有三次延迟，但没有逐人工时记录",
                "一名成员建议取消交叉检查来赶进度，另一名担心因此返工",
                "老师只给了最终日期和评价标准，没有指定小组内部流程",
                "目前已完成的部分只经过一个人检查，还没有第二份核对记录",
                "开会可以快速确认责任，但两名成员在晚上无法同时参加",
                "我既负责整合也参与写作，如果临时接手更多任务会压缩检查时间",
                "其中一份延迟可能是任务定义不清，不一定代表负责人投入不足",
                "如果分工记录能在会后由每个人确认，后续责任争议会更容易核对",
                "新收到的样本文件存在两处缺失值，这使数据检查不能被完全取消",
                "我倾向先确认任务和最低检查标准，然后再根据进度重分配",
            ),
        ),
        ScenarioSpec(
            case_id="UAT-02",
            label="产品上线速度与质量风险",
            participant={
                "display_name": "合成参与者02",
                "identity_type": "professional",
                "occupation": "初级产品经理",
                "experience_level": "一年",
                "collaboration_role": "跨团队上线协调者",
            },
            first_answer="我在犹豫要不要提前上线。",
            target_valid_answers=40,
            clarifications=(
                Clarification(
                    after_valid_answer=10,
                    text="我没理解你刚才的问题。",
                ),
            ),
            facts=(
                "原定上线日期在两周后，商务团队希望提前五天赶上宣传窗口",
                "自动化测试的通过率是百分之九十八，但高峰并发场景只测了一次",
                "一份安全检查报告提到一个低概率的权限边界问题",
                "工程团队认为可以通过小流量发布降低风险，但回滚脚本还没完整演练",
                "客服团队已收到十二个用户对新功能的问询，但没有紧急需求证据",
                "上一次类似提前发布曾出现两小时服务降级，后来在当天完成回滚",
                "商务预测提前上线会增加试用注册，但该预测未经独立复核",
                "如果先向百分之五用户开放，最多可以在十分钟内停止扩容",
                "项目负责人关心日期，运维负责人更关心可观测性和值班负担",
                "我没有权限单独决定全量上线，但需要提交一份有依据的建议",
                "新的压测结果显示响应时间在高峰上升了百分之二十二",
                "我倾向把回滚演练和高峰压测作为提前发布的前置条件",
            ),
        ),
        ScenarioSpec(
            case_id="UAT-03",
            label="研究生项目与就业机会选择",
            participant={
                "display_name": "合成参与者03",
                "identity_type": "student",
                "occupation": "应届本科生",
                "experience_level": "毕业年级",
                "collaboration_role": "个人决策者",
            },
            first_answer="我在毕业选择上有些摇摆。",
            target_valid_answers=41,
            facts=(
                "一个研究生项目已发来有条件录取，奖学金需要三周后才能确认",
                "一份工作邀请要求在十天内答复，职位与我的实习经历相关",
                "研究生课程包含我想学的方法课，但导师每年指导学生数量比较多",
                "工作的税前薪酬能覆盖生活开支，但培训计划只在招聘介绍中简略提及",
                "家人愿意支持一年学费，但这会减少家庭的应急储备",
                "两名在读学生对课程质量的评价很好，对导师反馈速度的看法不同",
                "我的长期目标是做需要研究能力的产品工作，但不确定学位是否必需",
                "工作邀请允许六个月后申请内部轮岗，具体条件没写进合同",
                "如果奖学金不成立，读书方案需要额外借款才能完成",
                "我可以请求工作方将答复期限延长一周，但对方未必同意",
                "项目办公室新回复说奖学金名额只覆盖约一半录取者",
                "我倾向先核实奖学金和工作培训条款，再根据可逆性做决定",
            ),
        ),
        ScenarioSpec(
            case_id="UAT-04",
            label="校园活动遇到天气与供应商变化",
            participant={
                "display_name": "合成参与者04",
                "identity_type": "student",
                "occupation": "学生社团干部",
                "experience_level": "一年活动经验",
                "collaboration_role": "活动统筹者",
            },
            first_answer="我要决定周末活动是否照常。",
            target_valid_answers=42,
            facts=(
                "活动计划在周末户外举行，已有一百八十名参与者报名",
                "气象预报给出百分之六十降雨可能，但不同平台对雨量预测不一致",
                "原定舞台供应商通知运输车辆存在故障，可能晚到两小时",
                "校内备用场地只能容纳一百人，且需要在明天中午前确认",
                "两名志愿者提醒户外场地对行动不便参与者的通道不够稳定",
                "取消会损失一部分场地定金，改期则需要重新确认大部分志愿者时间",
                "问卷显示多数参与者愿意接受室内缩减版，但回答率只有百分之三十",
                "校方要求任何方案都保留紧急疏散通道，这一条件不能妥协",
                "一名赞助方希望保留现场展示，但合同并没要求必须户外举行",
                "我可以将活动拆成室内主环节和延后的户外体验环节",
                "新的临近预报将强风概率上调到百分之七十，舞台安全风险因此增加",
                "我倾向优先保留室内主环节，并在明确安全条件后再决定户外部分",
            ),
        ),
        ScenarioSpec(
            case_id="UAT-05",
            label="实习项目外部服务商选择",
            participant={
                "display_name": "合成参与者05",
                "identity_type": "professional",
                "occupation": "项目实习生",
                "experience_level": "三个月",
                "collaboration_role": "供应商评估协助者",
            },
            first_answer="我需要比较三家外部服务商。",
            target_valid_answers=44,
            clarifications=(
                Clarification(
                    after_valid_answer=23,
                    text="麻烦再解释一下。",
                ),
            ),
            facts=(
                "三家服务商的报价相差约百分之四十，但交付范围表达方式不统一",
                "最便宜的方案承诺两周交付，只提供了一个可联系的参考客户",
                "中等报价方案有三个相似案例，其中一个案例的上线时间曾延迟",
                "最高报价方案提供完整安全说明，但有两项功能需要后续另行计费",
                "项目的核心要求是保留数据导出能力，这一点在两份方案中没写清楚",
                "财务团队希望今年成本最低，使用团队更关心后续响应速度",
                "我们只有一周做选择，但可以要求三家在同一份清单上补充回复",
                "一份网络评价称某家响应慢，但评价无法核实是否来自真实客户",
                "如果先做一个两周小范围验证，可以在不迁移全部数据的情况下检查服务质量",
                "我没有最终签约权，但需要向项目负责人说明评估依据",
                "新收到的统一回复显示，最便宜方案不支持自助导出原始数据",
                "我倾向先排除不满足数据可迁移底线的方案，再比较成本和服务质量",
            ),
        ),
        ScenarioSpec(
            case_id="UAT-06",
            label="志愿项目有限资源分配",
            participant={
                "display_name": "合成参与者06",
                "identity_type": "informal",
                "occupation": "社区志愿者",
                "experience_level": "两年",
                "collaboration_role": "资源分配讨论参与者",
            },
            first_answer="我想理清有限资源怎么分。",
            target_valid_answers=45,
            facts=(
                "项目本月只能提供一百二十个服务时段，登记需求已超过两百人次",
                "原方案按登记时间排序，但有些行动不便参与者无法在开放时立即登记",
                "一部分志愿者希望优先紧急需求，但当前没有统一的紧急程度定义",
                "去年的投诉中既有等待过久问题，也有人担心需求说明涉及隐私",
                "项目可以预留百分之二十时段给特殊情况，但预留标准需要公开且可复核",
                "两个合作机构对优先顺序有不同建议，一方看紧急度，另一方看服务连续性",
                "已有数据只记录预约和完成情况，没有记录未能成功登记的需求",
                "如果完全由工作人员判断优先级，可能更灵活，但也会增加不一致和难以解释的风险",
                "一个可逆的方案是先试行两周混合规则，同时保留申诉和人工复核",
                "参与者不应为了获得服务被迫披露与需求无关的敏感信息",
                "新的排班显示下月可能增加二十个时段，但尚未最终确认",
                "我倾向将可解释的紧急度规则与部分按时间排序结合，并保留人工复核",
            ),
        ),
    )


def normalized_visible_character_count(value: str) -> int:
    return sum(
        1
        for char in unicodedata.normalize("NFKC", value)
        if unicodedata.category(char)[0] in {"L", "N"}
    )


def prepare_answers(spec: ScenarioSpec) -> tuple[PreparedAnswer, ...]:
    if not spec.facts:
        raise AssertionError(f"scenario_without_facts:{spec.case_id}")
    answers = [
        PreparedAnswer(
            answer_id=f"{spec.case_id}-A01", intent="fact", text=spec.first_answer
        )
    ]
    for offset in range(spec.target_valid_answers - 1):
        intent = INTENT_ORDER[offset % len(INTENT_ORDER)]
        fact = spec.facts[(offset * 5 + len(spec.case_id)) % len(spec.facts)]
        templates = RESPONSE_TEMPLATES[intent]
        template = templates[(offset // len(INTENT_ORDER)) % len(templates)]
        answers.append(
            PreparedAnswer(
                answer_id=f"{spec.case_id}-A{offset + 2:02d}",
                intent=intent,
                text=template.format(fact=fact),
            )
        )
    return tuple(answers)


def classify_question_intent(message: str) -> str:
    normalized = unicodedata.normalize("NFKC", message).casefold()
    scores = {
        intent: sum(normalized.count(keyword.casefold()) for keyword in keywords)
        for intent, keywords in INTENT_KEYWORDS.items()
    }
    best_score = max(scores.values(), default=0)
    if best_score <= 0:
        return "reflection"
    return next(intent for intent in INTENT_ORDER if scores.get(intent) == best_score)


def validate_scenarios(specs: Sequence[ScenarioSpec]) -> dict[str, int]:
    if len(specs) != 6:
        raise AssertionError(f"expected_six_scenarios:{len(specs)}")
    if len({item.case_id for item in specs}) != len(specs):
        raise AssertionError("duplicate_scenario_id")
    clarification_count = 0
    total_answers = 0
    for spec in specs:
        if not MIN_VALID_ANSWERS <= spec.target_valid_answers <= MAX_VALID_ANSWERS:
            raise AssertionError(
                f"scenario_answer_target_out_of_range:{spec.case_id}:"
                f"{spec.target_valid_answers}"
            )
        answers = prepare_answers(spec)
        if len(answers) != spec.target_valid_answers:
            raise AssertionError(f"scenario_answer_bank_size:{spec.case_id}")
        if normalized_visible_character_count(answers[0].text) < 1:
            raise AssertionError(f"first_answer_blank:{spec.case_id}")
        if normalized_visible_character_count(answers[0].text) >= 20:
            raise AssertionError(f"first_answer_does_not_test_short_path:{spec.case_id}")
        for answer in answers[1:]:
            if normalized_visible_character_count(answer.text) < 20:
                raise AssertionError(
                    f"subsequent_answer_too_short:{spec.case_id}:{answer.answer_id}"
                )
        for clarification in spec.clarifications:
            if not 1 <= clarification.after_valid_answer < spec.target_valid_answers:
                raise AssertionError(
                    f"clarification_schedule_invalid:{spec.case_id}:"
                    f"{clarification.after_valid_answer}"
                )
            if normalized_visible_character_count(clarification.text) < 1:
                raise AssertionError(f"blank_clarification:{spec.case_id}")
            if not is_bounded_clarification_request(clarification.text):
                raise AssertionError(
                    f"clarification_contract_mismatch:{spec.case_id}:"
                    f"{clarification.after_valid_answer}"
                )
        total_answers += len(answers)
        clarification_count += len(spec.clarifications)
    if clarification_count < 2:
        raise AssertionError(f"at_least_two_clarifications_required:{clarification_count}")
    logical_calls = len(specs) * 2 + total_answers + clarification_count
    theoretical_physical_max = logical_calls * 3
    if theoretical_physical_max > HARD_REQUEST_LIMIT:
        raise AssertionError(
            f"planned_request_budget_exceeds_hard_limit:{theoretical_physical_max}"
        )
    return {
        "scenario_count": len(specs),
        "planned_valid_answers": total_answers,
        "planned_clarifications": clarification_count,
        "planned_logical_calls": logical_calls,
        "theoretical_physical_max": theoretical_physical_max,
    }


def canonical_scenario_payload(specs: Sequence[ScenarioSpec]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": spec.case_id,
            "label": spec.label,
            "participant": spec.participant,
            "target_valid_answers": spec.target_valid_answers,
            "answers": [asdict(item) for item in prepare_answers(spec)],
            "clarifications": [asdict(item) for item in spec.clarifications],
        }
        for spec in specs
    ]


def scenario_sha256(specs: Sequence[ScenarioSpec]) -> str:
    payload = json.dumps(
        canonical_scenario_payload(specs),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _record_invoked_provider(record: dict[str, Any]) -> bool:
    return bool(record.get("requested_model")) or str(
        record.get("model_provider") or ""
    ).casefold() == "deepseek"


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(method, path, json=payload)
    if response.status_code >= 400:
        raise AssertionError(
            f"{method} {path} failed: {response.status_code}: {response.text[:800]}"
        )
    return response.json()


def parse_events(response: httpx.Response) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    if response.status_code >= 400:
        code = events[-1].get("code") if events else "unknown"
        raise AssertionError(
            f"turn_failed:{response.status_code}:{code}:{response.text[:800]}"
        )
    return events


def _completed_event(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    completed = next(
        (item.get("data") for item in reversed(events) if item.get("event") == "agent_completed"),
        None,
    )
    if not isinstance(completed, dict):
        raise AssertionError(f"agent_completed_missing:{events[-1] if events else 'none'}")
    return completed


def assert_visible_message(message: str) -> None:
    if not message.strip():
        raise AssertionError("empty_interviewer_message")
    lower = message.casefold()
    if any(token.casefold() in lower for token in FORBIDDEN_VISIBLE):
        raise AssertionError(f"forbidden_visible_language:{message}")
    if message.count("？") + message.count("?") > 1:
        raise AssertionError(f"multiple_primary_questions:{message}")


def _ensure_local_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise AssertionError(f"real_uat_requires_local_api:{base_url}")
    return normalized


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _outside_repository(path: Path, *, error: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        return resolved
    raise AssertionError(error)


def _ensure_private_budget_ledger_dir(ledger_dir: Path) -> Path:
    resolved = _outside_repository(
        ledger_dir,
        error="uat_budget_ledger_must_be_outside_repository",
    )
    if resolved.exists() and not resolved.is_dir():
        raise AssertionError(f"uat_budget_ledger_is_not_directory:{resolved}")
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(resolved, 0o700)
    return resolved


def _ensure_private_output_dir(output_dir: Path) -> Path:
    resolved = _outside_repository(
        output_dir,
        error="uat_output_must_be_outside_repository",
    )
    if resolved.exists():
        raise AssertionError(f"uat_output_dir_must_not_exist:{resolved}")
    resolved.mkdir(parents=True, mode=0o700)
    os.chmod(resolved, 0o700)
    return resolved


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with path.open("wb") as handle:
        handle.write(value)
    os.chmod(path, 0o600)


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.chmod(path, 0o600)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_snapshot() -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "status_short": run("status", "--short").splitlines(),
    }


class LocalAuditDB:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        if not self.path.is_file():
            raise AssertionError(f"uat_sqlite_database_missing:{self.path}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def all_provider_records(self) -> list[dict[str, Any]]:
        """Return every provider-backed trace in creation order for settlement.

        This method is deliberately read-only and does not need participant
        transcript content.  The settlement verifier compares only provenance,
        model identity, response identifiers and retry/repair metadata.
        """

        with self._connect() as connection:
            traces = connection.execute(
                """
                SELECT id, session_id, action, model_provider, model_name,
                       requested_model, actual_model, response_id, request_id,
                       transport_retry_count, renderer_status, repair_used,
                       fallback_used, fallback_reason, created_at
                FROM agent_traces
                ORDER BY created_at, id
                """
            ).fetchall()
            scoring = connection.execute(
                """
                SELECT id, session_id, status, model_provider, model_name,
                       requested_model, actual_model, response_id, request_id,
                       transport_retry_count, repair_used, error, created_at
                FROM scoring_runs
                ORDER BY created_at, id
                """
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in traces:
            record = dict(row)
            record["record_type"] = "agent_trace"
            records.append(record)
        for row in scoring:
            record = dict(row)
            record.update(
                record_type="scoring_run",
                renderer_status=record.get("status"),
                fallback_used=False,
                fallback_reason=record.get("error"),
            )
            records.append(record)
        records.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("record_type") or ""),
                int(item.get("id") or 0),
            )
        )
        return [item for item in records if _record_invoked_provider(item)]

    def session_records(self, session_uuids: Sequence[str]) -> list[dict[str, Any]]:
        if not session_uuids:
            return []
        placeholders = ",".join("?" for _ in session_uuids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id, uuid FROM assessment_sessions WHERE uuid IN ({placeholders})",
                tuple(session_uuids),
            ).fetchall()
            session_ids = {str(row["uuid"]): int(row["id"]) for row in rows}
            if set(session_uuids) != set(session_ids):
                missing = sorted(set(session_uuids) - set(session_ids))
                raise AssertionError(f"api_and_audit_db_do_not_match:{missing}")
            id_placeholders = ",".join("?" for _ in session_ids)
            ids = tuple(session_ids.values())
            traces = connection.execute(
                f"""
                SELECT id, session_id, assistant_turn_id, action,
                       model_provider, model_name,
                       requested_model, actual_model, response_id, request_id,
                       prompt_tokens, completion_tokens, total_tokens,
                       transport_retry_count, prompt_template_id, prompt_version,
                       output_contract, renderer_status, repair_used, fallback_used,
                       fallback_reason, latency_ms, created_at
                FROM agent_traces
                WHERE session_id IN ({id_placeholders})
                ORDER BY id
                """,
                ids,
            ).fetchall()
            scoring = connection.execute(
                f"""
                SELECT id, session_id, attempt_number, status, model_provider,
                       model_name, requested_model, actual_model, response_id,
                       request_id, prompt_tokens, completion_tokens, total_tokens,
                       transport_retry_count, prompt_template_id, prompt_version,
                       final_scorer_contract_sha256, repair_used, error,
                       created_at, completed_at
                FROM scoring_runs
                WHERE session_id IN ({id_placeholders})
                ORDER BY id
                """,
                ids,
            ).fetchall()
        uuid_by_id = {value: key for key, value in session_ids.items()}
        records: list[dict[str, Any]] = []
        for row in traces:
            record = dict(row)
            record.update(
                record_type="agent_trace",
                session_uuid=uuid_by_id[int(record["session_id"])],
            )
            records.append(record)
        for row in scoring:
            record = dict(row)
            record.update(
                record_type="scoring_run",
                session_uuid=uuid_by_id[int(record["session_id"])],
                latency_ms=_elapsed_ms(
                    record.get("created_at"), record.get("completed_at")
                ),
                renderer_status=record.get("status"),
                fallback_used=False,
                fallback_reason=record.get("error"),
            )
            records.append(record)
        return records


def _elapsed_ms(started: Any, completed: Any) -> int | None:
    if not started or not completed:
        return None
    try:
        start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def audit_model_identity(records: Sequence[dict[str, Any]]) -> None:
    provider_records = [item for item in records if _record_invoked_provider(item)]
    if not provider_records:
        raise AssertionError("no_real_model_provenance_in_local_database")
    for record in provider_records:
        if record.get("requested_model") != EXPECTED_MODEL:
            raise AssertionError(
                f"requested_model_mismatch:{record['record_type']}:{record['id']}:"
                f"{record.get('requested_model')}"
            )
        if record.get("actual_model") != EXPECTED_MODEL:
            raise AssertionError(
                f"actual_model_mismatch:{record['record_type']}:{record['id']}:"
                f"{record.get('actual_model')}"
            )
        if bool(record.get("fallback_used")):
            raise AssertionError(
                f"fallback_used_in_real_uat:{record['record_type']}:{record['id']}"
            )
        if str(record.get("renderer_status") or "") == "failed":
            raise AssertionError(
                f"failed_model_record:{record['record_type']}:{record['id']}"
            )


def _require_sha256(value: str, *, label: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise AssertionError(f"{label}_must_be_lowercase_sha256")
    return normalized


def _read_settlement_events(value: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"settlement_source_event_json_invalid:{line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise AssertionError(
                f"settlement_source_event_not_object:{line_number}"
            )
        events.append(event)
    if not events:
        raise AssertionError("settlement_source_events_empty")
    return events


def _verify_settlement_source_events(
    state: dict[str, Any], events: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    opened: dict[str, int] = {}
    closed: dict[str, int] = {}
    reservations: list[dict[str, Any]] = []
    seen_reservations: set[tuple[str, str]] = set()
    previous_reserved = 0
    previous_observed = 0

    for index, event in enumerate(events):
        event_name = str(event.get("event") or "")
        run_id = str(event.get("run_id") or "")
        if not run_id:
            raise AssertionError(f"settlement_event_run_id_missing:{index}")
        if event_name == "run_opened":
            opened[run_id] = opened.get(run_id, 0) + 1
        elif event_name == "run_closed":
            closed[run_id] = closed.get(run_id, 0) + 1
        elif event_name == "call_reserved":
            label = str(event.get("label") or "")
            namespace = str(event.get("record_namespace") or "")
            key = (run_id, label)
            if not label or not namespace or key in seen_reservations:
                raise AssertionError(
                    f"settlement_reservation_identity_invalid:{run_id}:{label}"
                )
            seen_reservations.add(key)
            if int(event.get("reserved_attempts") or 0) != 3:
                raise AssertionError("settlement_reservation_not_three_attempts")
            expected_reserved = previous_reserved + 3
            reserved_value = event.get("reserved_physical_upper_bound")
            if reserved_value is None or int(reserved_value) != expected_reserved:
                raise AssertionError("settlement_reservation_cumulative_mismatch")
            observed_value = event.get("observed_physical_attempts")
            if observed_value is None or int(observed_value) != previous_observed:
                raise AssertionError("settlement_reservation_observed_mismatch")
            if index + 1 >= len(events):
                raise AssertionError("settlement_reservation_missing_observation")
            observation = events[index + 1]
            if (
                observation.get("event") != "audit_observed"
                or str(observation.get("run_id") or "") != run_id
                or str(observation.get("record_namespace") or "") != namespace
                or observation.get("reserved_physical_upper_bound") is None
                or int(observation["reserved_physical_upper_bound"])
                != expected_reserved
            ):
                raise AssertionError("settlement_reservation_not_immediately_observed")
            if observation.get("observed_physical_attempts") is None:
                raise AssertionError("settlement_observation_total_missing")
            observed = int(observation["observed_physical_attempts"])
            delta = observed - previous_observed
            if delta not in {1, 2, 3}:
                raise AssertionError("settlement_observed_attempt_delta_invalid")
            reservations.append(
                {
                    "ordinal": len(reservations) + 1,
                    "run_id": run_id,
                    "label": label,
                    "record_namespace": namespace,
                    "audit_delta": delta,
                }
            )
            previous_reserved = expected_reserved
            previous_observed = observed

    if len(reservations) != SETTLEMENT_EXPECTED_RESERVATION_COUNT:
        raise AssertionError(
            f"settlement_reservation_count_mismatch:{len(reservations)}"
        )
    if previous_reserved != SETTLEMENT_EXPECTED_HISTORICAL_RESERVED:
        raise AssertionError("settlement_historical_reserved_mismatch")
    if previous_observed != SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED:
        raise AssertionError("settlement_historical_observed_mismatch")
    state_runs = list(state.get("runs") or [])
    state_run_ids = [str(item.get("run_id") or "") for item in state_runs]
    if set(state_run_ids) != set(opened) or any(value != 1 for value in opened.values()):
        raise AssertionError("settlement_run_open_provenance_mismatch")
    if set(opened) != set(closed) or any(value != 1 for value in closed.values()):
        raise AssertionError("settlement_unclosed_run")
    if str(events[-1].get("event") or "") != "run_closed":
        raise AssertionError("settlement_final_event_not_run_closed")
    return reservations


def _verify_settlement_databases(
    database_paths: Sequence[Path],
    *,
    source_record_keys: set[tuple[str, str, int]],
    reservations: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reservation_namespaces = {
        str(item["record_namespace"]) for item in reservations
    }
    if len(database_paths) != len(reservation_namespaces):
        raise AssertionError("settlement_database_path_count_mismatch")

    records_by_namespace: dict[str, list[dict[str, Any]]] = {}
    provenance: list[dict[str, Any]] = []
    for raw_path in database_paths:
        path = _outside_repository(
            Path(raw_path), error="settlement_database_must_be_outside_repository"
        )
        audit_db = LocalAuditDB(path)
        namespace = _sha256_bytes(str(audit_db.path).encode("utf-8"))
        if namespace in records_by_namespace:
            raise AssertionError("settlement_duplicate_database_namespace")
        records = audit_db.all_provider_records()
        audit_model_identity(records)
        attempts = 0
        response_ids: list[str] = []
        for record in records:
            response_id = str(record.get("response_id") or "").strip()
            if not response_id:
                raise AssertionError("settlement_response_id_missing")
            response_ids.append(response_id)
            attempt_count = 1 + int(record.get("transport_retry_count") or 0)
            attempt_count += 1 if bool(record.get("repair_used")) else 0
            if attempt_count not in {1, 2, 3}:
                raise AssertionError("settlement_provider_attempt_count_invalid")
            attempts += attempt_count
        if len(response_ids) != len(set(response_ids)):
            raise AssertionError("settlement_duplicate_response_id_in_database")
        records_by_namespace[namespace] = records
        provenance.append(
            {
                "record_namespace": namespace,
                "database_path_sha256": _sha256_bytes(
                    str(audit_db.path).encode("utf-8")
                ),
                "provider_record_count": len(records),
                "observed_physical_attempts": attempts,
            }
        )
    if set(records_by_namespace) != reservation_namespaces:
        raise AssertionError("settlement_database_namespace_mismatch")

    all_response_ids: set[str] = set()
    database_record_keys: set[tuple[str, str, int]] = set()
    offsets = {namespace: 0 for namespace in records_by_namespace}
    mapping: list[dict[str, Any]] = []
    for reservation in reservations:
        namespace = str(reservation["record_namespace"])
        records = records_by_namespace[namespace]
        offset = offsets[namespace]
        if offset >= len(records):
            raise AssertionError("settlement_reservation_missing_provider_record")
        record = records[offset]
        offsets[namespace] += 1
        record_key = (
            namespace,
            str(record["record_type"]),
            int(record["id"]),
        )
        if record_key in database_record_keys:
            raise AssertionError("settlement_duplicate_provider_record")
        database_record_keys.add(record_key)
        response_id = str(record.get("response_id") or "").strip()
        if response_id in all_response_ids:
            raise AssertionError("settlement_duplicate_response_id_global")
        all_response_ids.add(response_id)
        attempts = 1 + int(record.get("transport_retry_count") or 0)
        attempts += 1 if bool(record.get("repair_used")) else 0
        if attempts != int(reservation["audit_delta"]):
            raise AssertionError("settlement_reservation_provider_attempt_mismatch")
        mapping.append(
            {
                "ordinal": int(reservation["ordinal"]),
                "run_id": str(reservation["run_id"]),
                "label": str(reservation["label"]),
                "record_namespace": namespace,
                "record_type": str(record["record_type"]),
                "record_id": int(record["id"]),
                "response_id_sha256": _sha256_bytes(response_id.encode("utf-8")),
                "physical_attempts": attempts,
                "audit_delta": int(reservation["audit_delta"]),
            }
        )
    if any(offsets[key] != len(value) for key, value in records_by_namespace.items()):
        raise AssertionError("settlement_database_has_unreserved_provider_record")
    if database_record_keys != source_record_keys:
        raise AssertionError("settlement_database_record_keys_mismatch")
    if len(mapping) != SETTLEMENT_EXPECTED_RESERVATION_COUNT:
        raise AssertionError("settlement_provider_record_count_mismatch")
    if sum(item["physical_attempts"] for item in mapping) != (
        SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
    ):
        raise AssertionError("settlement_provider_attempt_total_mismatch")
    return mapping, sorted(provenance, key=lambda item: item["record_namespace"])


def settle_budget_ledger_v1_to_v2(
    *,
    source_ledger_dir: Path,
    target_ledger_dir: Path,
    database_paths: Sequence[Path],
    expected_source_state_sha256: str,
    expected_source_events_sha256: str,
    confirmation: str,
) -> dict[str, Any]:
    """Create a new immutable v2 ledger without touching the sealed v1 source."""

    if confirmation != SETTLEMENT_CONFIRMATION:
        raise AssertionError("settlement_confirmation_mismatch")
    expected_state_sha = _require_sha256(
        expected_source_state_sha256, label="expected_source_state_sha256"
    )
    expected_events_sha = _require_sha256(
        expected_source_events_sha256, label="expected_source_events_sha256"
    )
    source = _outside_repository(
        source_ledger_dir, error="settlement_source_ledger_must_be_outside_repository"
    )
    target = _outside_repository(
        target_ledger_dir, error="settlement_target_ledger_must_be_outside_repository"
    )
    if not source.is_dir():
        raise AssertionError("settlement_source_ledger_missing")
    if target.exists():
        raise AssertionError("settlement_target_ledger_must_not_exist")
    if source == target or source in target.parents or target in source.parents:
        raise AssertionError("settlement_source_and_target_must_be_disjoint")

    state_path = source / "budget_state.json"
    events_path = source / "budget_events.jsonl"
    lock_path = source / "budget.lock"
    if not state_path.is_file() or not events_path.is_file() or not lock_path.is_file():
        raise AssertionError("settlement_source_ledger_incomplete")
    with lock_path.open("r", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AssertionError("settlement_source_ledger_locked") from exc
        state_bytes = state_path.read_bytes()
        events_bytes = events_path.read_bytes()
        if _sha256_bytes(state_bytes) != expected_state_sha:
            raise AssertionError("settlement_source_state_sha256_mismatch")
        if _sha256_bytes(events_bytes) != expected_events_sha:
            raise AssertionError("settlement_source_events_sha256_mismatch")
        try:
            state = json.loads(state_bytes)
        except json.JSONDecodeError as exc:
            raise AssertionError("settlement_source_state_json_invalid") from exc
        expected_contract = {
            "version": 1,
            "expected_model": EXPECTED_MODEL,
            "expected_protocol": EXPECTED_PROTOCOL,
            "soft_limit": SOFT_REQUEST_WARNING,
            "pause_limit": PAUSE_REQUEST_LIMIT,
            "hard_limit": HARD_REQUEST_LIMIT,
            "reserved_physical_upper_bound": SETTLEMENT_EXPECTED_HISTORICAL_RESERVED,
            "observed_physical_attempts": SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED,
        }
        for key, expected in expected_contract.items():
            if state.get(key) != expected:
                raise AssertionError(
                    f"settlement_source_contract_mismatch:{key}:"
                    f"{state.get(key)}!={expected}"
                )
        source_record_keys = {
            (str(item[0]), str(item[1]), int(item[2]))
            for item in state.get("record_keys", [])
        }
        if len(source_record_keys) != SETTLEMENT_EXPECTED_RESERVATION_COUNT:
            raise AssertionError("settlement_source_record_key_count_mismatch")
        events = _read_settlement_events(events_bytes)
        reservations = _verify_settlement_source_events(state, events)
        mapping, database_provenance = _verify_settlement_databases(
            database_paths,
            source_record_keys=source_record_keys,
            reservations=reservations,
        )
        mapping_sha = _sha256_bytes(
            json.dumps(
                mapping,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        settlement: dict[str, Any] = {
            "schema": SETTLEMENT_SCHEMA,
            "source_version": 1,
            "source_state_sha256": expected_state_sha,
            "source_events_sha256": expected_events_sha,
            "source_reserved_physical_upper_bound": (
                SETTLEMENT_EXPECTED_HISTORICAL_RESERVED
            ),
            "source_observed_physical_attempts": (
                SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
            ),
            "verified_reservation_count": SETTLEMENT_EXPECTED_RESERVATION_COUNT,
            "verified_provider_record_count": SETTLEMENT_EXPECTED_RESERVATION_COUNT,
            "unresolved_reservation_count": 0,
            "settled_unused_envelope": (
                SETTLEMENT_EXPECTED_HISTORICAL_RESERVED
                - SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
            ),
            "effective_initial_reserved_physical_upper_bound": (
                SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
            ),
            "source_database_namespaces": sorted(
                {str(item["record_namespace"]) for item in reservations}
            ),
            "database_provenance": database_provenance,
            "reservation_mapping_sha256": mapping_sha,
            "confirmation_sha256": _sha256_bytes(confirmation.encode("utf-8")),
            "created_at": _utc_timestamp(),
        }
        settlement["settlement_sha256"] = _settlement_sha256(settlement)
        target_state = {
            "version": 2,
            "expected_model": EXPECTED_MODEL,
            "expected_protocol": EXPECTED_PROTOCOL,
            "soft_limit": SOFT_REQUEST_WARNING,
            "pause_limit": PAUSE_REQUEST_LIMIT,
            "hard_limit": HARD_REQUEST_LIMIT,
            "observed_physical_attempts": SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED,
            "reserved_physical_upper_bound": SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED,
            "effective_committed_physical_upper_bound": (
                SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
            ),
            "warned": False,
            "record_keys": [list(item) for item in sorted(source_record_keys)],
            "runs": [],
            "settlement": settlement,
            "updated_at": _utc_timestamp(),
        }
        target_event = {
            "event": "ledger_settled_from_v1",
            "at": _utc_timestamp(),
            "observed_physical_attempts": SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED,
            "reserved_physical_upper_bound": SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED,
            "effective_committed_physical_upper_bound": (
                SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
            ),
            "settlement_sha256": settlement["settlement_sha256"],
            "source_state_sha256": expected_state_sha,
            "source_events_sha256": expected_events_sha,
        }
        target.mkdir(parents=True, mode=0o700)
        os.chmod(target, 0o700)
        _write_json(target / "budget_state.json", target_state)
        _write_bytes(
            target / "budget_events.jsonl",
            (
                json.dumps(target_event, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8"),
        )
        _write_bytes(target / "budget.lock", b"")
        if _sha256_file(state_path) != expected_state_sha:
            raise AssertionError("settlement_source_state_changed_during_settlement")
        if _sha256_file(events_path) != expected_events_sha:
            raise AssertionError("settlement_source_events_changed_during_settlement")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return {
        "target_ledger_dir": str(target),
        "settlement_sha256": settlement["settlement_sha256"],
        "source_state_sha256": expected_state_sha,
        "source_events_sha256": expected_events_sha,
        "verified_reservation_count": SETTLEMENT_EXPECTED_RESERVATION_COUNT,
        "observed_physical_attempts": SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED,
        "effective_reserved_physical_upper_bound": (
            SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
        ),
    }


def _file_evidence(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "name": path.name, "size": 0, "sha256": None}
    if not path.is_file():
        raise AssertionError(f"forensic_artifact_not_file:{path.name}")
    return {
        "exists": True,
        "name": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def _directory_evidence(path: Path) -> dict[str, Any]:
    if not path.is_dir():
        raise AssertionError(f"forensic_directory_missing:{path}")
    files = []
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        evidence = _file_evidence(item)
        evidence["relative_path"] = str(item.relative_to(path))
        files.append(evidence)
    payload = json.dumps(
        files,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {"file_count": len(files), "inventory_sha256": _sha256_bytes(payload), "files": files}


def _verify_aborted_v2_events(
    state: dict[str, Any],
    events: Sequence[dict[str, Any]],
    *,
    expected_run_id: str,
    expected_prior_settlement_sha256: str,
) -> dict[str, Any]:
    if not events or str(events[0].get("event") or "") != "ledger_settled_from_v1":
        raise AssertionError("abort_settlement_prior_event_missing")
    if str(events[0].get("settlement_sha256") or "") != expected_prior_settlement_sha256:
        raise AssertionError("abort_settlement_prior_event_sha_mismatch")
    run_events = [item for item in events[1:] if str(item.get("run_id") or "")]
    if not run_events or any(
        str(item.get("run_id") or "") != expected_run_id for item in run_events
    ):
        raise AssertionError("abort_settlement_run_id_mismatch")
    if str(run_events[0].get("event") or "") != "run_opened":
        raise AssertionError("abort_settlement_run_open_missing")
    if any(str(item.get("event") or "") == "run_closed" for item in run_events):
        raise AssertionError("abort_settlement_run_was_already_closed")
    reservations = [
        item for item in run_events if str(item.get("event") or "") == "call_reserved"
    ]
    observations = [
        item for item in run_events if str(item.get("event") or "") == "audit_observed"
    ]
    if len(reservations) != ABORT_SOURCE_NEW_RESERVATIONS:
        raise AssertionError(
            f"abort_settlement_reservation_count_mismatch:{len(reservations)}"
        )
    if len(observations) != ABORT_SOURCE_NEW_PROVIDER_RECORDS:
        raise AssertionError(
            f"abort_settlement_observation_count_mismatch:{len(observations)}"
        )
    previous_reserved = SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
    previous_observed = SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED
    observation_index = 0
    for index, reservation in enumerate(reservations):
        if int(reservation.get("reserved_attempts") or 0) != 3:
            raise AssertionError("abort_settlement_reservation_not_three_attempts")
        previous_reserved += 3
        if int(reservation.get("reserved_physical_upper_bound") or -1) != previous_reserved:
            raise AssertionError("abort_settlement_reserved_cumulative_mismatch")
        if int(reservation.get("observed_physical_attempts") or -1) != previous_observed:
            raise AssertionError("abort_settlement_reservation_observed_mismatch")
        if index < len(reservations) - 1:
            observation = observations[observation_index]
            observation_index += 1
            if int(observation.get("reserved_physical_upper_bound") or -1) != previous_reserved:
                raise AssertionError("abort_settlement_observation_reserved_mismatch")
            observed = int(observation.get("observed_physical_attempts") or -1)
            delta = observed - previous_observed
            if delta not in {1, 2, 3}:
                raise AssertionError("abort_settlement_observation_delta_invalid")
            previous_observed = observed
    if previous_reserved != ABORT_SOURCE_RESERVED:
        raise AssertionError("abort_settlement_source_reserved_mismatch")
    if previous_observed != ABORT_SOURCE_OBSERVED:
        raise AssertionError("abort_settlement_source_observed_mismatch")
    if str(run_events[-1].get("event") or "") != "call_reserved":
        raise AssertionError("abort_settlement_final_unresolved_reservation_missing")
    state_runs = list(state.get("runs") or [])
    if len(state_runs) != 1 or str(state_runs[0].get("run_id") or "") != expected_run_id:
        raise AssertionError("abort_settlement_state_run_mismatch")
    namespace = str(state_runs[0].get("record_namespace") or "")
    if not namespace or any(
        str(item.get("record_namespace") or "") != namespace for item in run_events
    ):
        raise AssertionError("abort_settlement_namespace_mismatch")
    return {
        "record_namespace": namespace,
        "reservation_count": len(reservations),
        "observation_count": len(observations),
        "unresolved_reservation_count": 1,
        "observed_physical_attempts": previous_observed,
        "reserved_physical_upper_bound": previous_reserved,
    }


def _normalize_triplet_sha_size(
    evidence: Mapping[str, Mapping[str, Any]], *, label: str
) -> dict[str, dict[str, Any]]:
    if set(evidence) != {"database", "wal", "shm"}:
        raise AssertionError(f"{label}_keys_invalid")
    normalized: dict[str, dict[str, Any]] = {}
    for key in ("database", "wal", "shm"):
        item = evidence[key]
        if set(item) != {"sha256", "size"}:
            raise AssertionError(f"{label}_{key}_fields_invalid")
        sha = _require_sha256(str(item.get("sha256") or ""), label=f"{label}_{key}")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise AssertionError(f"{label}_{key}_size_invalid")
        normalized[key] = {"sha256": sha, "size": size}
    return normalized


def _triplet_sha_size(
    evidence: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "sha256": str(evidence[key].get("sha256") or ""),
            "size": int(evidence[key].get("size") or 0),
        }
        for key in ("database", "wal", "shm")
    }


def _create_forensic_sqlite_checkpoint(
    *,
    source_database_path: Path,
    checkpoint_archive_dir: Path,
    expected_current_triplet: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    source_database = _outside_repository(
        source_database_path, error="abort_settlement_database_must_be_outside_repository"
    )
    archive = _outside_repository(
        checkpoint_archive_dir,
        error="abort_settlement_checkpoint_must_be_outside_repository",
    )
    if archive.exists():
        raise AssertionError("abort_settlement_checkpoint_must_not_exist")
    source_paths = {
        "database": source_database,
        "wal": Path(f"{source_database}-wal"),
        "shm": Path(f"{source_database}-shm"),
    }
    if not all(path.is_file() for path in source_paths.values()):
        raise AssertionError("abort_settlement_database_triplet_incomplete")
    source_before = {key: _file_evidence(path) for key, path in source_paths.items()}
    if _triplet_sha_size(source_before) != _normalize_triplet_sha_size(
        expected_current_triplet,
        label="accidental_checkpoint_after",
    ):
        raise AssertionError("abort_settlement_current_triplet_not_frozen_after")
    archive.mkdir(parents=True, mode=0o700)
    os.chmod(archive, 0o700)
    checkpoint_paths = {
        "database": archive / "forensic.sqlite3",
        "wal": archive / "forensic.sqlite3-wal",
        "shm": archive / "forensic.sqlite3-shm",
    }
    for key, source_path in source_paths.items():
        shutil.copyfile(source_path, checkpoint_paths[key])
        os.chmod(checkpoint_paths[key], 0o600)
    checkpoint_before = {
        key: _file_evidence(path) for key, path in checkpoint_paths.items()
    }
    connection = sqlite3.connect(checkpoint_paths["database"], timeout=30)
    try:
        checkpoint_result = list(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        integrity_check = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    finally:
        connection.close()
    for path in checkpoint_paths.values():
        if path.exists():
            os.chmod(path, 0o600)
    checkpoint_after = {
        key: _file_evidence(path) for key, path in checkpoint_paths.items()
    }
    source_after = {key: _file_evidence(path) for key, path in source_paths.items()}
    if source_before != source_after:
        raise AssertionError("abort_settlement_source_database_triplet_changed")
    if quick_check != ["ok"] or integrity_check != ["ok"]:
        raise AssertionError(
            f"abort_settlement_checkpoint_integrity_failed:{quick_check}:{integrity_check}"
        )
    return {
        "archive_dir": str(archive),
        "source_before": source_before,
        "source_after": source_after,
        "checkpoint_before": checkpoint_before,
        "checkpoint_after": checkpoint_after,
        "wal_checkpoint_truncate": checkpoint_result,
        "quick_check": quick_check,
        "integrity_check": integrity_check,
        "checkpoint_database_path": checkpoint_paths["database"],
    }


def settle_aborted_disk_full_v2_to_v3(
    *,
    source_ledger_dir: Path,
    target_ledger_dir: Path,
    source_database_path: Path,
    source_sealed_dir: Path,
    checkpoint_archive_dir: Path,
    prompt_path: Path,
    gateway_path: Path,
    expected_source_state_sha256: str,
    expected_source_events_sha256: str,
    expected_prior_settlement_sha256: str,
    expected_prompt_sha256: str,
    expected_gateway_sha256: str,
    expected_run_id: str,
    confirmation: str,
    expected_accidental_checkpoint_before: Mapping[str, Mapping[str, Any]] | None = None,
    expected_accidental_checkpoint_after: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Archive a disk-full interruption and create a conservative v3 ledger."""

    if confirmation != ABORT_SETTLEMENT_CONFIRMATION:
        raise AssertionError("abort_settlement_confirmation_mismatch")
    state_sha = _require_sha256(
        expected_source_state_sha256, label="expected_source_state_sha256"
    )
    events_sha = _require_sha256(
        expected_source_events_sha256, label="expected_source_events_sha256"
    )
    prior_sha = _require_sha256(
        expected_prior_settlement_sha256,
        label="expected_prior_settlement_sha256",
    )
    prompt_sha = _require_sha256(expected_prompt_sha256, label="expected_prompt_sha256")
    gateway_sha = _require_sha256(
        expected_gateway_sha256, label="expected_gateway_sha256"
    )
    run_id = str(expected_run_id or "").strip()
    if not run_id:
        raise AssertionError("expected_aborted_run_id_required")
    accidental_checkpoint_before = _normalize_triplet_sha_size(
        expected_accidental_checkpoint_before or ACCIDENTAL_CHECKPOINT_BEFORE,
        label="accidental_checkpoint_before",
    )
    accidental_checkpoint_after = _normalize_triplet_sha_size(
        expected_accidental_checkpoint_after or ACCIDENTAL_CHECKPOINT_AFTER,
        label="accidental_checkpoint_after",
    )
    if accidental_checkpoint_before == accidental_checkpoint_after:
        raise AssertionError("accidental_checkpoint_before_after_must_differ")
    source = _outside_repository(
        source_ledger_dir,
        error="abort_settlement_source_ledger_must_be_outside_repository",
    )
    target = _outside_repository(
        target_ledger_dir,
        error="abort_settlement_target_ledger_must_be_outside_repository",
    )
    sealed = _outside_repository(
        source_sealed_dir,
        error="abort_settlement_sealed_dir_must_be_outside_repository",
    )
    archive = _outside_repository(
        checkpoint_archive_dir,
        error="abort_settlement_checkpoint_must_be_outside_repository",
    )
    if not source.is_dir():
        raise AssertionError("abort_settlement_source_ledger_missing")
    if target.exists():
        raise AssertionError("abort_settlement_target_ledger_must_not_exist")
    if archive.exists():
        raise AssertionError("abort_settlement_checkpoint_must_not_exist")
    all_paths = [source, target, sealed, archive]
    if len(set(all_paths)) != len(all_paths):
        raise AssertionError("abort_settlement_paths_must_be_disjoint")
    if any(a in b.parents for a in all_paths for b in all_paths if a != b):
        raise AssertionError("abort_settlement_paths_must_not_be_nested")
    state_path = source / "budget_state.json"
    events_path = source / "budget_events.jsonl"
    lock_path = source / "budget.lock"
    if not all(path.is_file() for path in (state_path, events_path, lock_path)):
        raise AssertionError("abort_settlement_source_ledger_incomplete")
    if not prompt_path.is_file() or _sha256_file(prompt_path) != prompt_sha:
        raise AssertionError("abort_settlement_prompt_sha256_mismatch")
    if not gateway_path.is_file() or _sha256_file(gateway_path) != gateway_sha:
        raise AssertionError("abort_settlement_gateway_sha256_mismatch")
    with lock_path.open("r", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AssertionError("abort_settlement_source_ledger_locked") from exc
        source_state_bytes = state_path.read_bytes()
        source_events_bytes = events_path.read_bytes()
        if _sha256_bytes(source_state_bytes) != state_sha:
            raise AssertionError("abort_settlement_source_state_sha256_mismatch")
        if _sha256_bytes(source_events_bytes) != events_sha:
            raise AssertionError("abort_settlement_source_events_sha256_mismatch")
        try:
            state = json.loads(source_state_bytes)
        except json.JSONDecodeError as exc:
            raise AssertionError("abort_settlement_source_state_json_invalid") from exc
        expected_contract = {
            "version": 2,
            "expected_model": EXPECTED_MODEL,
            "expected_protocol": EXPECTED_PROTOCOL,
            "soft_limit": SOFT_REQUEST_WARNING,
            "pause_limit": PAUSE_REQUEST_LIMIT,
            "hard_limit": HARD_REQUEST_LIMIT,
            "observed_physical_attempts": ABORT_SOURCE_OBSERVED,
            "reserved_physical_upper_bound": ABORT_SOURCE_RESERVED,
        }
        for key, expected in expected_contract.items():
            if state.get(key) != expected:
                raise AssertionError(
                    f"abort_settlement_source_contract_mismatch:{key}:"
                    f"{state.get(key)}!={expected}"
                )
        prior_settlement = state.get("settlement")
        if not isinstance(prior_settlement, dict):
            raise AssertionError("abort_settlement_prior_settlement_missing")
        if str(prior_settlement.get("settlement_sha256") or "") != prior_sha:
            raise AssertionError("abort_settlement_prior_settlement_sha_mismatch")
        if _settlement_sha256(prior_settlement) != prior_sha:
            raise AssertionError("abort_settlement_prior_settlement_invalid")
        events = _read_settlement_events(source_events_bytes)
        abort_audit = _verify_aborted_v2_events(
            state,
            events,
            expected_run_id=run_id,
            expected_prior_settlement_sha256=prior_sha,
        )
        source_record_keys = {
            (str(item[0]), str(item[1]), int(item[2]))
            for item in state.get("record_keys", [])
        }
        if len(source_record_keys) != (
            SETTLEMENT_EXPECTED_RESERVATION_COUNT + ABORT_SOURCE_NEW_PROVIDER_RECORDS
        ):
            raise AssertionError("abort_settlement_source_record_key_count_mismatch")
        sealed_before = _directory_evidence(sealed)
        manifest_path = sealed / "run_manifest.json"
        if not manifest_path.is_file():
            raise AssertionError("abort_settlement_manifest_missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            str(manifest.get("run_id") or "") != run_id
            or str(manifest.get("status") or "") != "RUNNING"
            or str(manifest.get("settlement_sha256") or "") != prior_sha
        ):
            raise AssertionError("abort_settlement_manifest_provenance_mismatch")
        checkpoint = _create_forensic_sqlite_checkpoint(
            source_database_path=source_database_path,
            checkpoint_archive_dir=archive,
            expected_current_triplet=accidental_checkpoint_after,
        )
        checkpoint_db = LocalAuditDB(Path(checkpoint.pop("checkpoint_database_path")))
        provider_records = checkpoint_db.all_provider_records()
        audit_model_identity(provider_records)
        if len(provider_records) != ABORT_SOURCE_NEW_PROVIDER_RECORDS:
            raise AssertionError("abort_settlement_provider_record_count_mismatch")
        attempts = sum(
            1
            + int(item.get("transport_retry_count") or 0)
            + (1 if bool(item.get("repair_used")) else 0)
            for item in provider_records
        )
        if attempts != ABORT_SOURCE_OBSERVED - SETTLEMENT_EXPECTED_HISTORICAL_OBSERVED:
            raise AssertionError("abort_settlement_provider_attempt_total_mismatch")
        namespace = str(abort_audit["record_namespace"])
        if namespace != _sha256_bytes(str(Path(source_database_path).resolve()).encode("utf-8")):
            raise AssertionError("abort_settlement_database_namespace_mismatch")
        new_record_keys = {
            (namespace, str(item["record_type"]), int(item["id"]))
            for item in provider_records
        }
        if not new_record_keys.issubset(source_record_keys):
            raise AssertionError("abort_settlement_database_record_keys_mismatch")
        response_ids = [str(item.get("response_id") or "").strip() for item in provider_records]
        if any(not item for item in response_ids) or len(response_ids) != len(set(response_ids)):
            raise AssertionError("abort_settlement_response_id_invalid")
        forensic_report = {
            "schema": "v6.1-aborted-disk-full-forensic-checkpoint-v2",
            "failure_reason": ABORT_FAILURE_REASON,
            "aborted_run_id": run_id,
            "conservative_carry": ABORT_CONSERVATIVE_CARRY,
            "logical_call_reservations": ABORT_SOURCE_NEW_RESERVATIONS,
            "provider_record_count": ABORT_SOURCE_NEW_PROVIDER_RECORDS,
            "observed_physical_attempts_during_aborted_run": attempts,
            "unresolved_reservation_count": 1,
            "source_v2_state_sha256": state_sha,
            "source_v2_events_sha256": events_sha,
            "source_v1_state_sha256": prior_settlement.get("source_state_sha256"),
            "source_v1_events_sha256": prior_settlement.get("source_events_sha256"),
            "prior_settlement_sha256": prior_sha,
            "prompt_sha256": prompt_sha,
            "gateway_sha256": gateway_sha,
            "accidental_checkpoint_before": accidental_checkpoint_before,
            "accidental_checkpoint_after": accidental_checkpoint_after,
            "source_sealed_evidence": sealed_before,
            "sqlite_checkpoint": checkpoint,
            "created_at": _utc_timestamp(),
        }
        report_path = archive / "forensic_report.json"
        _write_json(report_path, forensic_report)
        report_sha = _sha256_file(report_path)
        settlement: dict[str, Any] = {
            "schema": ABORT_SETTLEMENT_SCHEMA,
            "source_version": 2,
            "source_state_sha256": state_sha,
            "source_events_sha256": events_sha,
            "source_reserved_physical_upper_bound": ABORT_SOURCE_RESERVED,
            "source_observed_physical_attempts": ABORT_SOURCE_OBSERVED,
            "prior_settlement_sha256": prior_sha,
            "source_v1_state_sha256": prior_settlement.get("source_state_sha256"),
            "source_v1_events_sha256": prior_settlement.get("source_events_sha256"),
            "aborted_run_id": run_id,
            "failure_reason": ABORT_FAILURE_REASON,
            "aborted_run_reservation_count": ABORT_SOURCE_NEW_RESERVATIONS,
            "aborted_run_provider_record_count": ABORT_SOURCE_NEW_PROVIDER_RECORDS,
            "unresolved_reservation_count": 1,
            "conservative_carry": ABORT_CONSERVATIVE_CARRY,
            "effective_initial_reserved_physical_upper_bound": ABORT_CONSERVATIVE_CARRY,
            "source_database_namespaces": sorted(
                set(prior_settlement.get("source_database_namespaces") or [])
                | {namespace}
            ),
            "forensic_report_sha256": report_sha,
            "source_sealed_inventory_sha256": sealed_before["inventory_sha256"],
            "prompt_sha256": prompt_sha,
            "gateway_sha256": gateway_sha,
            "accidental_checkpoint_before": accidental_checkpoint_before,
            "accidental_checkpoint_after": accidental_checkpoint_after,
            "confirmation_sha256": _sha256_bytes(confirmation.encode("utf-8")),
            "created_at": _utc_timestamp(),
        }
        settlement["settlement_sha256"] = _settlement_sha256(settlement)
        target_state = {
            "version": 3,
            "expected_model": EXPECTED_MODEL,
            "expected_protocol": EXPECTED_PROTOCOL,
            "soft_limit": SOFT_REQUEST_WARNING,
            "pause_limit": PAUSE_REQUEST_LIMIT,
            "hard_limit": HARD_REQUEST_LIMIT,
            "observed_physical_attempts": ABORT_SOURCE_OBSERVED,
            "reserved_physical_upper_bound": ABORT_CONSERVATIVE_CARRY,
            "effective_committed_physical_upper_bound": ABORT_CONSERVATIVE_CARRY,
            "warned": False,
            "record_keys": [list(item) for item in sorted(source_record_keys)],
            "runs": [],
            "settlement": settlement,
            "updated_at": _utc_timestamp(),
        }
        target_event = {
            "event": "ledger_settled_aborted_disk_full",
            "at": _utc_timestamp(),
            "failure_reason": ABORT_FAILURE_REASON,
            "aborted_run_id": run_id,
            "observed_physical_attempts": ABORT_SOURCE_OBSERVED,
            "reserved_physical_upper_bound": ABORT_CONSERVATIVE_CARRY,
            "effective_committed_physical_upper_bound": ABORT_CONSERVATIVE_CARRY,
            "settlement_sha256": settlement["settlement_sha256"],
            "source_state_sha256": state_sha,
            "source_events_sha256": events_sha,
            "forensic_report_sha256": report_sha,
        }
        target.mkdir(parents=True, mode=0o700)
        os.chmod(target, 0o700)
        _write_json(target / "budget_state.json", target_state)
        _write_bytes(
            target / "budget_events.jsonl",
            (json.dumps(target_event, ensure_ascii=False, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        _write_bytes(target / "budget.lock", b"")
        source_after = {
            "state_sha256": _sha256_file(state_path),
            "events_sha256": _sha256_file(events_path),
            "sealed": _directory_evidence(sealed),
            "prompt_sha256": _sha256_file(prompt_path),
            "gateway_sha256": _sha256_file(gateway_path),
        }
        if source_after != {
            "state_sha256": state_sha,
            "events_sha256": events_sha,
            "sealed": sealed_before,
            "prompt_sha256": prompt_sha,
            "gateway_sha256": gateway_sha,
        }:
            raise AssertionError("abort_settlement_source_artifact_changed")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return {
        "target_ledger_dir": str(target),
        "checkpoint_archive_dir": str(archive),
        "forensic_report_sha256": report_sha,
        "settlement_sha256": settlement["settlement_sha256"],
        "source_state_sha256": state_sha,
        "source_events_sha256": events_sha,
        "aborted_run_id": run_id,
        "failure_reason": ABORT_FAILURE_REASON,
        "observed_physical_attempts": ABORT_SOURCE_OBSERVED,
        "effective_reserved_physical_upper_bound": ABORT_CONSERVATIVE_CARRY,
    }


def classify_close_origin(
    completed: dict[str, Any],
    records: Sequence[dict[str, Any]],
    *,
    session_uuid: str,
) -> tuple[str | None, str | None]:
    """Classify a close from the persisted trace linked to the exact turn.

    ``finish_reason`` is not trusted as provenance. In particular, the 45th
    answer is a deterministic protocol cap and must carry both the protocol
    gate provider and ``technical_maximum_reached``. A model close must link to
    a real DeepSeek AgentTrace for the returned assistant turn.
    """

    if str(completed.get("session_action") or "") != "finish":
        return None, None
    turn = completed.get("turn") or {}
    assistant_turn_id = turn.get("id")
    if assistant_turn_id is None:
        raise AssertionError("closing_assistant_turn_id_missing")
    matching = [
        item
        for item in records
        if item.get("record_type") == "agent_trace"
        and item.get("session_uuid") == session_uuid
        and int(item.get("assistant_turn_id") or -1) == int(assistant_turn_id)
    ]
    if len(matching) != 1:
        raise AssertionError(
            f"closing_turn_trace_count_invalid:{session_uuid}:"
            f"{assistant_turn_id}:{len(matching)}"
        )
    trace = matching[0]
    provider = str(trace.get("model_provider") or "")
    quality_flags = {str(item) for item in turn.get("quality_flags") or []}
    if "technical_maximum_reached" in quality_flags:
        if provider != "protocol_gate" or _record_invoked_provider(trace):
            raise AssertionError(
                f"technical_limit_trace_invalid:{session_uuid}:{provider}"
            )
        return "technical_limit", provider
    if _record_invoked_provider(trace):
        if provider.casefold() != "deepseek":
            raise AssertionError(
                f"model_close_provider_invalid:{session_uuid}:{provider}"
            )
        if trace.get("actual_model") != EXPECTED_MODEL:
            raise AssertionError(
                f"model_close_actual_model_invalid:{session_uuid}:"
                f"{trace.get('actual_model')}"
            )
        return "model", provider
    if provider == "protocol_gate":
        return "protocol_gate", provider
    if provider == "safety_gate":
        return "safety_gate", provider
    raise AssertionError(f"closing_origin_unclassified:{session_uuid}:{provider}")


def assert_exact_target_coverage(
    spec: ScenarioSpec,
    *,
    actual_answers: int,
    close_origin: str,
) -> None:
    if actual_answers != spec.target_valid_answers:
        raise AssertionError(
            f"scenario_target_not_reached_exactly:{spec.case_id}:"
            f"{actual_answers}!={spec.target_valid_answers}"
        )
    if spec.target_valid_answers == MAX_VALID_ANSWERS:
        if close_origin != "technical_limit":
            raise AssertionError(
                f"maximum_target_did_not_use_technical_limit:{spec.case_id}:"
                f"{close_origin}"
            )
    elif close_origin == "technical_limit":
        raise AssertionError(
            f"technical_limit_before_maximum:{spec.case_id}:{actual_answers}"
        )
    elif close_origin not in {"model", "uat_finalize"}:
        raise AssertionError(
            f"unsupported_uat_close_origin:{spec.case_id}:{close_origin}"
        )


def _refresh_audit(
    audit_db: LocalAuditDB,
    session_uuids: Sequence[str],
    budget: BudgetLedger,
) -> list[dict[str, Any]]:
    records = audit_db.session_records(session_uuids)
    audit_model_identity(records)
    budget.observe(records)
    return records


def create_session(
    client: httpx.Client,
    spec: ScenarioSpec,
    budget: BudgetLedger,
) -> tuple[dict[str, Any], str, float]:
    budget.ensure_can_call(f"{spec.case_id}:opening")
    started = time.perf_counter()
    payload = request_json(
        client,
        "POST",
        "/sessions",
        payload={
            "consent_version": CONSENT_VERSION,
            "consent_given": True,
            "participant": spec.participant,
        },
    )
    latency_ms = (time.perf_counter() - started) * 1000
    session = payload["session"]
    if session["phase"] != "interviewing":
        raise AssertionError(f"unexpected_create_phase:{session['phase']}")
    if session.get("interview_protocol_version") != EXPECTED_PROTOCOL:
        raise AssertionError(
            f"unexpected_interview_protocol:{session.get('interview_protocol_version')}"
        )
    if session.get("minimum_valid_answers") != MIN_VALID_ANSWERS:
        raise AssertionError("minimum_valid_answers_contract_mismatch")
    if session.get("maximum_user_answers") != MAX_VALID_ANSWERS:
        raise AssertionError("maximum_valid_answers_contract_mismatch")
    opening = payload["initial_turn"]["content"]
    assert_visible_message(opening)
    return session, opening, latency_ms


def _send_once(
    client: httpx.Client, session_uuid: str, payload: dict[str, Any]
) -> tuple[httpx.Response, float]:
    started = time.perf_counter()
    response = client.post(f"/sessions/{session_uuid}/turns:stream", json=payload)
    return response, (time.perf_counter() - started) * 1000


def submit_interaction(
    client: httpx.Client,
    session_uuid: str,
    *,
    client_turn_id: str,
    content: str,
    interaction_kind: str,
    budget: BudgetLedger,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any], float, int]:
    payload = {
        "content": content,
        "client_turn_id": client_turn_id,
        "interaction_kind": interaction_kind,
        "input_mode": "text",
        "answer_duration_ms": 15_000,
    }
    attempts = 0
    total_latency = 0.0
    while True:
        budget.ensure_can_call(label)
        attempts += 1
        try:
            response, latency_ms = _send_once(client, session_uuid, payload)
        except httpx.TransportError:
            if attempts >= 2:
                raise
            continue
        total_latency += latency_ms
        events = [
            json.loads(line) for line in response.text.splitlines() if line.strip()
        ]
        if response.status_code < 400:
            completed = _completed_event(events)
            return completed["session"], completed, total_latency, attempts
        recoverable = any(
            item.get("code") in {"model_connection_interrupted", "turn_processing_failed"}
            for item in events
        )
        if not recoverable or attempts >= 2:
            parse_events(response)


def _finalize_with_one_safe_retry(
    client: httpx.Client,
    session_uuid: str,
    budget: BudgetLedger,
) -> tuple[dict[str, Any], float, int]:
    attempts = 0
    total_latency = 0.0
    while True:
        budget.ensure_can_call(f"{session_uuid}:finalize")
        attempts += 1
        started = time.perf_counter()
        response = client.post(f"/sessions/{session_uuid}/finalize")
        total_latency += (time.perf_counter() - started) * 1000
        if response.status_code < 400:
            return response.json()["session"], total_latency, attempts
        if response.status_code not in {500, 502, 503, 504} or attempts >= 2:
            raise AssertionError(
                f"POST /sessions/{session_uuid}/finalize failed: "
                f"{response.status_code}: {response.text[:800]}"
            )


def report_audit(
    client: httpx.Client,
    session: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    report_started = time.perf_counter()
    report = request_json(client, "GET", f"/sessions/{session['uuid']}/report")
    report_latency_ms = (time.perf_counter() - report_started) * 1000
    if "total_score" in report or "ranking" in report:
        raise AssertionError("forbidden_report_total_or_ranking")
    if len(report.get("dimensions", [])) != 6:
        raise AssertionError(f"report_dimension_count:{len(report.get('dimensions', []))}")
    if any("confidence" in item for item in report["dimensions"]):
        raise AssertionError("public_report_exposes_confidence")

    turns = {
        turn["turn_index"]: turn["content"]
        for turn in session["turns"]
        if turn["role"] == "user"
    }
    evidence_count = 0
    for dimension in report["dimensions"]:
        if dimension.get("score") is not None:
            if dimension.get("status") != "sufficient" or not dimension.get("evidences"):
                raise AssertionError("numeric_score_without_sufficient_evidence")
        for evidence in dimension.get("evidences", []):
            evidence_count += 1
            source = turns.get(evidence["turn_index"])
            if source is None or evidence["quote"] not in source:
                raise AssertionError("report_quote_is_not_exact_user_text")

    pdf_started = time.perf_counter()
    pdf_response = client.get(f"/sessions/{session['uuid']}/report.pdf")
    pdf_latency_ms = (time.perf_counter() - pdf_started) * 1000
    if pdf_response.status_code >= 400:
        raise AssertionError(
            f"report_pdf_failed:{pdf_response.status_code}:{pdf_response.text[:400]}"
        )
    pdf = pdf_response.content
    if "application/pdf" not in pdf_response.headers.get("content-type", ""):
        raise AssertionError("report_pdf_content_type_invalid")
    if not pdf.startswith(b"%PDF") or b"%%EOF" not in pdf[-2048:] or len(pdf) < 1000:
        raise AssertionError("report_pdf_structure_invalid")
    return report, {
        "report_latency_ms": round(report_latency_ms, 3),
        "pdf_latency_ms": round(pdf_latency_ms, 3),
        "evidence_count": evidence_count,
        "pdf_bytes": len(pdf),
        "pdf_sha256": _sha256_bytes(pdf),
        "pdf_content": pdf,
    }


def run_flow(
    client: httpx.Client,
    spec: ScenarioSpec,
    *,
    run_id: str,
    audit_db: LocalAuditDB,
    budget: BudgetLedger,
    known_session_uuids: list[str],
    turn_metrics: list[dict[str, Any]],
    quality_rows: list[dict[str, Any]],
    output_dir: Path,
) -> tuple[FlowResult, dict[str, Any], list[dict[str, Any]]]:
    answers = prepare_answers(spec)
    selector = ResponseSelector(answers)
    session, interviewer_message, opening_latency = create_session(client, spec, budget)
    session_uuid = str(session["uuid"])
    known_session_uuids.append(session_uuid)
    records = _refresh_audit(audit_db, known_session_uuids, budget)
    turn_metrics.append(
        {
            "case_id": spec.case_id,
            "session_uuid": session_uuid,
            "interaction": "opening",
            "valid_answer_count": 0,
            "answer_id": "",
            "interaction_kind": "opening",
            "http_latency_ms": round(opening_latency, 3),
            "client_attempts": 1,
            "session_action": "continue",
            "finish_reason": "",
            "close_origin": "",
            "close_provider": "",
        }
    )

    clarification_schedule = {
        item.after_valid_answer: item for item in spec.clarifications
    }
    clarifications_used: set[int] = set()
    close_origin: str | None = None
    close_provider: str | None = None
    finish_reason: str | None = None
    while int(session.get("valid_answer_count", session["user_answer_count"])) < spec.target_valid_answers:
        current_count = int(session.get("valid_answer_count", session["user_answer_count"]))
        clarification = clarification_schedule.get(current_count)
        if clarification and current_count not in clarifications_used:
            before = current_count
            session, completed, latency_ms, client_attempts = submit_interaction(
                client,
                session_uuid,
                client_turn_id=f"{run_id}-{spec.case_id}-C{before:02d}",
                content=clarification.text,
                interaction_kind="clarification",
                budget=budget,
                label=f"{spec.case_id}:clarification:{before}",
            )
            after = int(session.get("valid_answer_count", session["user_answer_count"]))
            if after != before:
                raise AssertionError(
                    f"clarification_changed_valid_answer_count:{spec.case_id}:{before}->{after}"
                )
            clarification_turn = completed["turn"]
            interviewer_message = clarification_turn["content"]
            assert_visible_message(interviewer_message)
            clarifications_used.add(current_count)
            turn_metrics.append(
                {
                    "case_id": spec.case_id,
                    "session_uuid": session_uuid,
                    "interaction": f"clarification_after_{before}",
                    "valid_answer_count": after,
                    "answer_id": "",
                    "interaction_kind": "clarification",
                    "http_latency_ms": round(latency_ms, 3),
                    "client_attempts": client_attempts,
                    "session_action": completed.get("session_action"),
                    "finish_reason": completed.get("finish_reason") or "",
                    "close_origin": "",
                    "close_provider": "",
                }
            )
            quality_rows.append(
                _quality_row(spec.case_id, session_uuid, after, clarification_turn)
            )
            records = _refresh_audit(audit_db, known_session_uuids, budget)

        answer = selector.select(interviewer_message)
        before = int(session.get("valid_answer_count", session["user_answer_count"]))
        expected_after = before + 1
        session, completed, latency_ms, client_attempts = submit_interaction(
            client,
            session_uuid,
            client_turn_id=f"{run_id}-{spec.case_id}-{answer.answer_id}",
            content=answer.text,
            interaction_kind="answer",
            budget=budget,
            label=f"{spec.case_id}:answer:{expected_after}",
        )
        after = int(session.get("valid_answer_count", session["user_answer_count"]))
        if after != expected_after:
            raise AssertionError(
                f"valid_answer_count_increment_failed:{spec.case_id}:{before}->{after}"
            )
        assistant_turn = completed["turn"]
        interviewer_message = assistant_turn["content"]
        assert_visible_message(interviewer_message)
        action = str(completed.get("session_action") or "")
        finish_reason = completed.get("finish_reason")
        if action == "finish" and after < MIN_VALID_ANSWERS:
            raise AssertionError(f"model_finished_before_40:{spec.case_id}:{after}")
        if after == MAX_VALID_ANSWERS and action != "finish":
            raise AssertionError(f"technical_cap_did_not_finish:{spec.case_id}")
        records = _refresh_audit(audit_db, known_session_uuids, budget)
        turn_close_origin, turn_close_provider = classify_close_origin(
            completed,
            records,
            session_uuid=session_uuid,
        )
        if action == "finish":
            if after < spec.target_valid_answers:
                raise AssertionError(
                    f"scenario_closed_before_target:{spec.case_id}:"
                    f"{after}<{spec.target_valid_answers}:"
                    f"{turn_close_origin}"
                )
            close_origin = turn_close_origin
            close_provider = turn_close_provider
        turn_metrics.append(
            {
                "case_id": spec.case_id,
                "session_uuid": session_uuid,
                "interaction": f"answer_{after}",
                "valid_answer_count": after,
                "answer_id": answer.answer_id,
                "interaction_kind": "answer",
                "http_latency_ms": round(latency_ms, 3),
                "client_attempts": client_attempts,
                "session_action": action,
                "finish_reason": finish_reason or "",
                "close_origin": turn_close_origin or "",
                "close_provider": turn_close_provider or "",
            }
        )
        quality_rows.append(
            _quality_row(spec.case_id, session_uuid, after, assistant_turn)
        )
        if action == "finish":
            break

    actual_answers = int(session.get("valid_answer_count", session["user_answer_count"]))
    if actual_answers != spec.target_valid_answers:
        raise AssertionError(
            f"scenario_target_not_reached_exactly:{spec.case_id}:"
            f"{actual_answers}!={spec.target_valid_answers}"
        )
    if close_origin is None:
        close_origin = "uat_finalize"
        close_provider = "uat_controller"
    if session["phase"] not in {"interviewing", "finalizing"}:
        raise AssertionError(f"unexpected_pre_finalize_phase:{session['phase']}")
    session, finalize_latency, finalize_attempts = _finalize_with_one_safe_retry(
        client, session_uuid, budget
    )
    if session["phase"] != "completed":
        raise AssertionError(f"unexpected_final_phase:{session['phase']}")
    records = _refresh_audit(audit_db, known_session_uuids, budget)
    if close_origin == "uat_finalize":
        explicit_finalize = [
            item
            for item in records
            if item.get("record_type") == "agent_trace"
            and item.get("session_uuid") == session_uuid
            and item.get("action") == "user_requested_finalize"
            and item.get("model_provider") == "none"
        ]
        if len(explicit_finalize) != 1:
            raise AssertionError(
                f"uat_finalize_trace_count_invalid:{spec.case_id}:"
                f"{len(explicit_finalize)}"
            )
    assert_exact_target_coverage(
        spec,
        actual_answers=actual_answers,
        close_origin=close_origin,
    )

    report, checks = report_audit(client, session)
    report_path = output_dir / "reports" / f"{spec.case_id}.json"
    pdf_path = output_dir / "reports" / f"{spec.case_id}.pdf"
    transcript_path = output_dir / "transcripts" / f"{spec.case_id}.json"
    _write_json(report_path, report)
    _write_bytes(pdf_path, checks.pop("pdf_content"))
    _write_json(
        transcript_path,
        {
            "case_id": spec.case_id,
            "label": spec.label,
            "session_uuid": session_uuid,
            "interview_protocol_version": session.get("interview_protocol_version"),
            "valid_answer_count": actual_answers,
            "target_valid_answers": spec.target_valid_answers,
            "close_origin": close_origin,
            "close_provider": close_provider,
            "turns": [
                {
                    "turn_index": turn.get("turn_index"),
                    "role": turn.get("role"),
                    "content": turn.get("content"),
                    "interaction_kind": turn.get("interaction_kind"),
                    "session_action": turn.get("session_action"),
                    "finish_reason": turn.get("finish_reason"),
                    "quality_flags": turn.get("quality_flags") or [],
                }
                for turn in session.get("turns", [])
            ],
        },
    )
    report_sha = _sha256_file(report_path)
    pdf_sha = _sha256_file(pdf_path)
    transcript_sha = _sha256_file(transcript_path)
    scoring_records = [
        item
        for item in records
        if item["session_uuid"] == session_uuid and item["record_type"] == "scoring_run"
    ]
    if not scoring_records or scoring_records[-1].get("status") != "completed":
        raise AssertionError(f"completed_scoring_run_missing:{spec.case_id}")
    case_records = [item for item in records if item["session_uuid"] == session_uuid]
    total_tokens = sum(int(item.get("total_tokens") or 0) for item in case_records)
    result = FlowResult(
        case_id=spec.case_id,
        label=spec.label,
        session_uuid=session_uuid,
        target_valid_answers=spec.target_valid_answers,
        valid_answer_count=actual_answers,
        clarification_count=len(clarifications_used),
        close_origin=close_origin,
        close_provider=close_provider or "unknown",
        closed_by_model=close_origin == "model",
        finish_reason=finish_reason,
        scored_dimensions=sum(item.get("score") is not None for item in report["dimensions"]),
        null_dimensions=sum(item.get("score") is None for item in report["dimensions"]),
        report_sha256=report_sha,
        pdf_sha256=pdf_sha,
        transcript_sha256=transcript_sha,
        total_tokens=total_tokens,
        observed_physical_attempts=budget.observed_physical_attempts,
    )
    checks.update(
        case_id=spec.case_id,
        session_uuid=session_uuid,
        report_sha256=report_sha,
        pdf_sha256=pdf_sha,
        transcript_sha256=transcript_sha,
        valid_answer_count=actual_answers,
        clarification_count=len(clarifications_used),
        close_origin=close_origin,
        close_provider=close_provider,
        finalize_latency_ms=round(finalize_latency, 3),
        finalize_client_attempts=finalize_attempts,
    )
    return result, checks, records


def _quality_row(
    case_id: str,
    session_uuid: str,
    valid_answer_count: int,
    assistant_turn: dict[str, Any],
) -> dict[str, Any]:
    message = str(assistant_turn.get("content") or "")
    return {
        "case_id": case_id,
        "session_uuid": session_uuid,
        "valid_answer_count": valid_answer_count,
        "turn_index": assistant_turn.get("turn_index"),
        "question_count": message.count("？") + message.count("?"),
        "quality_flags": "|".join(assistant_turn.get("quality_flags") or []),
        "message_sha256": _sha256_bytes(message.encode("utf-8")),
        "interviewer_text": message,
        "manual_relevance": "PENDING",
        "manual_grounding": "PENDING",
        "manual_naturalness_1_to_5": "PENDING",
        "manual_new_information_transition": "PENDING",
        "manual_notes": "",
    }


def _model_usage_rows(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in records:
        if not _record_invoked_provider(item):
            continue
        rows.append(
            {
                "record_type": item["record_type"],
                "record_id": item["id"],
                "session_uuid": item["session_uuid"],
                "action": item.get("action") or "final_scoring",
                "requested_model": item.get("requested_model"),
                "actual_model": item.get("actual_model"),
                "response_id": item.get("response_id"),
                "request_id": item.get("request_id"),
                "prompt_tokens": item.get("prompt_tokens"),
                "completion_tokens": item.get("completion_tokens"),
                "total_tokens": item.get("total_tokens"),
                "transport_retry_count": item.get("transport_retry_count") or 0,
                "repair_used": bool(item.get("repair_used")),
                "estimated_physical_attempts": 1
                + int(item.get("transport_retry_count") or 0)
                + (1 if bool(item.get("repair_used")) else 0),
                "latency_ms": item.get("latency_ms"),
                "prompt_template_id": item.get("prompt_template_id"),
                "prompt_version": item.get("prompt_version"),
                "final_scorer_contract_sha256": item.get(
                    "final_scorer_contract_sha256"
                ),
                "status": item.get("renderer_status"),
            }
        )
    return rows


def _write_audit_bundle(
    output_dir: Path,
    *,
    manifest: dict[str, Any],
    results: Sequence[FlowResult],
    turn_metrics: Sequence[dict[str, Any]],
    quality_rows: Sequence[dict[str, Any]],
    report_checks: Sequence[dict[str, Any]],
    records: Sequence[dict[str, Any]],
) -> None:
    _write_json(output_dir / "run_manifest.json", manifest)
    _write_json(output_dir / "case_results.json", [asdict(item) for item in results])
    _write_json(output_dir / "report_checks.json", list(report_checks))
    _write_csv(
        output_dir / "turn_metrics.csv",
        list(turn_metrics),
        (
            "case_id",
            "session_uuid",
            "interaction",
            "valid_answer_count",
            "answer_id",
            "interaction_kind",
            "http_latency_ms",
            "client_attempts",
            "session_action",
            "finish_reason",
            "close_origin",
            "close_provider",
        ),
    )
    _write_csv(
        output_dir / "quality_review.csv",
        list(quality_rows),
        (
            "case_id",
            "session_uuid",
            "valid_answer_count",
            "turn_index",
            "question_count",
            "quality_flags",
            "message_sha256",
            "interviewer_text",
            "manual_relevance",
            "manual_grounding",
            "manual_naturalness_1_to_5",
            "manual_new_information_transition",
            "manual_notes",
        ),
    )
    usage_rows = _model_usage_rows(records)
    _write_csv(
        output_dir / "model_usage.csv",
        usage_rows,
        (
            "record_type",
            "record_id",
            "session_uuid",
            "action",
            "requested_model",
            "actual_model",
            "response_id",
            "request_id",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "transport_retry_count",
            "repair_used",
            "estimated_physical_attempts",
            "latency_ms",
            "prompt_template_id",
            "prompt_version",
            "final_scorer_contract_sha256",
            "status",
        ),
    )
    _write_json(output_dir / "synthetic_scenarios.json", canonical_scenario_payload(scenario_specs()))
    checksum_lines = []
    for path in sorted(item for item in output_dir.rglob("*") if item.is_file()):
        if path.name == "SHA256SUMS.txt":
            continue
        checksum_lines.append(f"{_sha256_file(path)}  {path.relative_to(output_dir)}")
    _write_bytes(
        output_dir / "SHA256SUMS.txt",
        ("\n".join(checksum_lines) + "\n").encode("utf-8"),
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--settle-budget-ledger-only",
        action="store_true",
        help="verify sealed v1 provenance and create a new v2 ledger without API calls",
    )
    parser.add_argument(
        "--settle-aborted-disk-full-only",
        action="store_true",
        help=(
            "checkpoint a read-only disk-full failure and create a conservative "
            "v3 ledger without API calls"
        ),
    )
    parser.add_argument(
        "--source-budget-ledger-dir",
        default="",
        help="read-only sealed v1 ledger directory used only by settlement mode",
    )
    parser.add_argument(
        "--settlement-db-path",
        action="append",
        default=[],
        help="read-only historical SQLite database; repeat once per namespace",
    )
    parser.add_argument("--expected-source-state-sha256", default="")
    parser.add_argument("--expected-source-events-sha256", default="")
    parser.add_argument("--expected-prior-settlement-sha256", default="")
    parser.add_argument("--expected-prompt-sha256", default="")
    parser.add_argument("--expected-gateway-sha256", default="")
    parser.add_argument("--expected-aborted-run-id", default="")
    parser.add_argument("--settlement-confirmation", default="")
    parser.add_argument("--failed-run-db-path", default="")
    parser.add_argument("--failed-run-sealed-dir", default="")
    parser.add_argument("--checkpoint-archive-dir", default="")
    parser.add_argument(
        "--expected-settlement-sha256",
        default=os.getenv("V6_REAL_UAT_EXPECTED_SETTLEMENT_SHA256", ""),
        help="mandatory v2 settlement asset lock for every billable UAT run",
    )
    parser.add_argument(
        "--confirmation",
        default=os.getenv("V6_REAL_UAT_CONFIRM", ""),
        help=f"must equal {CONFIRMATION}",
    )
    parser.add_argument(
        "--pause-confirmation",
        default=os.getenv("V6_REAL_UAT_PAUSE_CONFIRM", ""),
        help=f"required to continue after {PAUSE_REQUEST_LIMIT} physical attempts",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("V6_API_BASE_URL", "http://127.0.0.1:8060/api/v1"),
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("V6_REAL_UAT_DB_PATH", ""),
        help="absolute path to the disposable local SQLite database used by the API",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("V6_REAL_UAT_OUTPUT_DIR", ""),
        help="new absolute repository-external directory for the sealed audit bundle",
    )
    parser.add_argument(
        "--budget-ledger-dir",
        default=os.getenv("V6_REAL_UAT_BUDGET_LEDGER_DIR", ""),
        help=(
            "persistent repository-external 0700 directory shared by every "
            "new or resumed V6.1 real-UAT run"
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.settle_budget_ledger_only and args.settle_aborted_disk_full_only:
        raise SystemExit("Select exactly one settlement-only mode.")
    if args.settle_aborted_disk_full_only:
        if args.confirmation or args.pause_confirmation or args.db_path or args.output_dir:
            raise SystemExit(
                "Aborted-run settlement rejects billable UAT confirmation, fresh "
                "database, output and pause arguments."
            )
        required = {
            "--source-budget-ledger-dir": args.source_budget_ledger_dir,
            "--budget-ledger-dir": args.budget_ledger_dir,
            "--failed-run-db-path": args.failed_run_db_path,
            "--failed-run-sealed-dir": args.failed_run_sealed_dir,
            "--checkpoint-archive-dir": args.checkpoint_archive_dir,
            "--expected-aborted-run-id": args.expected_aborted_run_id,
        }
        missing = [label for label, value in required.items() if not value]
        if missing:
            raise SystemExit(f"Required aborted-run settlement arguments missing: {missing}")
        result = settle_aborted_disk_full_v2_to_v3(
            source_ledger_dir=Path(args.source_budget_ledger_dir),
            target_ledger_dir=Path(args.budget_ledger_dir),
            source_database_path=Path(args.failed_run_db_path),
            source_sealed_dir=Path(args.failed_run_sealed_dir),
            checkpoint_archive_dir=Path(args.checkpoint_archive_dir),
            prompt_path=BACKEND_ROOT / "app" / "services" / "orchestrator.py",
            gateway_path=BACKEND_ROOT / "app" / "services" / "model_gateway.py",
            expected_source_state_sha256=args.expected_source_state_sha256,
            expected_source_events_sha256=args.expected_source_events_sha256,
            expected_prior_settlement_sha256=args.expected_prior_settlement_sha256,
            expected_prompt_sha256=args.expected_prompt_sha256,
            expected_gateway_sha256=args.expected_gateway_sha256,
            expected_run_id=args.expected_aborted_run_id,
            confirmation=args.settlement_confirmation,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.settle_budget_ledger_only:
        if args.confirmation or args.pause_confirmation or args.db_path or args.output_dir:
            raise SystemExit(
                "Settlement-only mode rejects billable UAT confirmation, database, "
                "output and pause arguments."
            )
        if not args.source_budget_ledger_dir:
            raise SystemExit("--source-budget-ledger-dir is required for settlement")
        if not args.budget_ledger_dir:
            raise SystemExit("--budget-ledger-dir is the new v2 target and is required")
        if not args.settlement_db_path:
            raise SystemExit("at least one --settlement-db-path is required")
        result = settle_budget_ledger_v1_to_v2(
            source_ledger_dir=Path(args.source_budget_ledger_dir),
            target_ledger_dir=Path(args.budget_ledger_dir),
            database_paths=[Path(item) for item in args.settlement_db_path],
            expected_source_state_sha256=args.expected_source_state_sha256,
            expected_source_events_sha256=args.expected_source_events_sha256,
            confirmation=args.settlement_confirmation,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.confirmation != CONFIRMATION:
        raise SystemExit(
            "Refusing billable real-model UAT. Supply --confirmation "
            f"{CONFIRMATION} only after the zero-call gate passes."
        )
    if not args.db_path:
        raise SystemExit("--db-path is required and must be the disposable local UAT SQLite file")
    if not args.output_dir:
        raise SystemExit("--output-dir is required and must be a new repository-external path")
    if not args.budget_ledger_dir:
        raise SystemExit(
            "--budget-ledger-dir is required and must be reused across every rerun"
        )
    expected_settlement_sha256 = _require_sha256(
        args.expected_settlement_sha256,
        label="expected_settlement_sha256",
    )
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")

    specs = scenario_specs()
    plan = validate_scenarios(specs)
    base_url = _ensure_local_base_url(args.base_url)
    requested_ledger = _outside_repository(
        Path(args.budget_ledger_dir),
        error="uat_budget_ledger_must_be_outside_repository",
    )
    if not requested_ledger.is_dir():
        raise AssertionError("budget_ledger_v2_required")
    ledger_dir = _ensure_private_budget_ledger_dir(requested_ledger)
    requested_output = Path(args.output_dir).expanduser().resolve()
    if requested_output == ledger_dir or ledger_dir in requested_output.parents:
        raise AssertionError("uat_output_must_not_be_inside_budget_ledger")
    if requested_output in ledger_dir.parents:
        raise AssertionError("uat_budget_ledger_must_not_be_inside_output")
    audit_db = LocalAuditDB(Path(args.db_path))
    allow_after_pause = args.pause_confirmation == PAUSE_CONFIRMATION
    run_id = uuid.uuid4().hex[:16]
    database_path_sha256 = _sha256_bytes(str(audit_db.path).encode("utf-8"))
    budget = BudgetLedger(
        allow_after_pause=allow_after_pause,
        ledger_dir=ledger_dir,
        run_id=run_id,
        record_namespace=database_path_sha256,
        expected_settlement_sha256=expected_settlement_sha256,
    )
    try:
        budget.ensure_run_capacity(int(plan["theoretical_physical_max"]))
        output_dir = _ensure_private_output_dir(Path(args.output_dir))
    except Exception:
        budget.close("PRECHECK_FAILED")
        raise
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest: dict[str, Any] = {
        "status": "RUNNING",
        "run_id": run_id,
        "started_at": started_at,
        "api_base_url": base_url,
        "database_path_sha256": database_path_sha256,
        "expected_model": EXPECTED_MODEL,
        "expected_protocol": EXPECTED_PROTOCOL,
        "settlement_sha256": expected_settlement_sha256,
        "scenario_sha256": scenario_sha256(specs),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "git": _git_snapshot(),
        "budget": {
            **plan,
            "soft_warning": SOFT_REQUEST_WARNING,
            "pause_limit": PAUSE_REQUEST_LIMIT,
            "hard_limit": HARD_REQUEST_LIMIT,
            "pause_override_confirmed": allow_after_pause,
            "persistent_ledger": budget.snapshot(),
        },
        "participant_generation": "human_authored_facts_plus_deterministic_literal_templates",
        "deepseek_participant_generation": False,
    }
    _write_json(output_dir / "run_manifest.json", manifest)

    results: list[FlowResult] = []
    turn_metrics: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    report_checks: list[dict[str, Any]] = []
    known_session_uuids: list[str] = []
    all_records: list[dict[str, Any]] = []
    try:
        with httpx.Client(base_url=base_url, timeout=args.timeout_seconds) as client:
            health = request_json(client, "GET", "/health")
            db_health = request_json(client, "GET", "/health/db")
            if health.get("version") != "v6.0" or db_health.get("status") != "ok":
                raise AssertionError(
                    f"unexpected_local_backend_health:{health}:{db_health}"
                )
            for spec in specs:
                result, checks, all_records = run_flow(
                    client,
                    spec,
                    run_id=run_id,
                    audit_db=audit_db,
                    budget=budget,
                    known_session_uuids=known_session_uuids,
                    turn_metrics=turn_metrics,
                    quality_rows=quality_rows,
                    output_dir=output_dir,
                )
                results.append(result)
                report_checks.append(checks)
        if len(results) != 6:
            raise AssertionError(f"six_real_uat_flows_required:{len(results)}")
        if sum(item.clarification_count for item in results) < 2:
            raise AssertionError("two_non_counting_clarifications_were_not_completed")
        if [item.valid_answer_count for item in results] != [
            item.target_valid_answers for item in specs
        ]:
            raise AssertionError("six_exact_target_counts_were_not_completed")
        if results[-1].close_origin != "technical_limit":
            raise AssertionError("45_answer_case_missing_technical_limit_close")
        manifest.update(
            status="COMPLETED",
            completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            observed_physical_attempts=budget.observed_physical_attempts,
            reserved_physical_upper_bound=budget.reserved_physical_upper_bound,
            completed_session_uuids=known_session_uuids,
            interpretation=(
                "Local synthetic real-model UAT only; not psychometric validity, "
                "cross-participant comparability, microphone, or deployment evidence."
            ),
        )
        manifest["budget"]["persistent_ledger"] = budget.snapshot()
        _write_audit_bundle(
            output_dir,
            manifest=manifest,
            results=results,
            turn_metrics=turn_metrics,
            quality_rows=quality_rows,
            report_checks=report_checks,
            records=all_records,
        )
    except Exception as exc:
        if known_session_uuids:
            try:
                all_records = audit_db.session_records(known_session_uuids)
                budget.observe(all_records)
            except Exception as audit_exc:
                manifest["failure_audit_refresh_error"] = (
                    f"{type(audit_exc).__name__}: {audit_exc}"
                )[:1000]
        manifest.update(
            status="FAILED",
            failed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            failure_type=type(exc).__name__,
            failure_message=str(exc)[:1000],
            observed_physical_attempts=budget.observed_physical_attempts,
            reserved_physical_upper_bound=budget.reserved_physical_upper_bound,
            completed_session_uuids=known_session_uuids,
        )
        manifest["budget"]["persistent_ledger"] = budget.snapshot()
        _write_audit_bundle(
            output_dir,
            manifest=manifest,
            results=results,
            turn_metrics=turn_metrics,
            quality_rows=quality_rows,
            report_checks=report_checks,
            records=all_records,
        )
        raise
    finally:
        budget.close(str(manifest.get("status") or "FAILED"))

    print(
        json.dumps(
            {
                "status": "REAL_DEEPSEEK_V6_1_UAT_COMPLETED",
                "output_dir": str(output_dir),
                "scenario_sha256": manifest["scenario_sha256"],
                "observed_physical_attempts": budget.observed_physical_attempts,
                "reserved_physical_upper_bound": (
                    budget.reserved_physical_upper_bound
                ),
                "flows": [asdict(item) for item in results],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, httpx.HTTPError, sqlite3.Error) as exc:
        print(f"REAL_DEEPSEEK_V6_1_UAT_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
