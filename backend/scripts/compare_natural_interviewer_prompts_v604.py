#!/usr/bin/env python3
"""Private, synthetic-only blind comparison for interviewer v6.0.3/v6.0.4.

The normal plan is eight synthetic scenario types, two opaque prompt arms and
three scripted user turns per arm (48 logical calls).  A model-requested
``finish`` closes that arm immediately.  Every physical ``httpx.post`` is
counted *before* it can reach the network and the process fails closed at 80.

Nothing in this runner reads the application database, review examples or real
participant data.  It does not score candidates.  The review packet and the
case/arm key are written separately to a new repository-external directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent.resolve()
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402
from app.schemas import NaturalInterviewerOutput  # noqa: E402
from app.services import model_gateway as gateway_module  # noqa: E402
from app.services.model_gateway import (  # noqa: E402
    ModelGatewayService,
    resolve_natural_interviewer_prompt,
)


EXPECTED_MODEL = "deepseek-v4-flash"
CONFIRMATION = "RUN_V604_SYNTHETIC_BLIND_AB"
PROMPT_VERSIONS = ("v6.0.3", "v6.0.4")
ROUNDS_PER_ARM = 3
NORMAL_LOGICAL_CALLS = 8 * len(PROMPT_VERSIONS) * ROUNDS_PER_ARM
MAX_PHYSICAL_REQUESTS = 80
RUNNER_PROTOCOL = "natural_interviewer_blind_ab_v1"


class RunnerHardStop(BaseException):
    """Bypass gateway repair/retry logic for protocol-level hard stops."""


@dataclass(frozen=True)
class SyntheticScenario:
    scenario_type: str
    opening: str
    user_turns: tuple[str, str, str]


SCENARIOS: tuple[SyntheticScenario, ...] = (
    SyntheticScenario(
        "fact_and_decision",
        "你好。最近有什么让你想多说一点？",
        (
            "我在考虑要不要把一个课程项目提前一周交付，现在功能基本齐了，但测试还不够完整。",
            "我更在意的是别让组员因为赶进度反复返工，所以想先核实关键流程是否稳定。",
            "如果核心流程仍有高风险问题，我会推迟；只有非关键细节没完成，我才会按原计划交付。",
        ),
    ),
    SyntheticScenario(
        "emotion_and_pressure",
        "你好。最近有什么让你想多说一点？",
        (
            "最近团队里很多决定都要我来拍板，我有点累，也担心自己一着急就忽略别人的顾虑。",
            "最有压力的是大家意见冲突时都等我表态，我怕太快决定会让没被听见的人更失望。",
            "我想先让每个人说清最担心的后果，再一起看哪些风险能验证，而不是只靠我安慰大家。",
        ),
    ),
    SyntheticScenario(
        "explicit_correction",
        "你好。最近有什么让你想多说一点？",
        (
            "我最近在重新安排学习计划，想减少同时推进的任务。",
            "如果把这个理解成缺乏自律，那不太准确；之前把太多互相依赖的事情挤在同一天，任何一项延迟都会连锁影响。",
            "我真正想调整的是任务之间的依赖关系，先把最容易阻塞其他人的部分单独排出来。",
        ),
    ),
    SyntheticScenario(
        "request_for_explanation",
        "你好。最近有什么让你想多说一点？",
        (
            "我正在比较两个实习机会，一个成长空间更大，另一个工作方式更适合我现在的状态。",
            "我刚才说得有点抽象。你现在最想弄清楚的那个问题是什么意思？我不太确定该从哪个角度回答。",
            "如果是说什么情况会让我改变选择，那我会看实际导师投入和日常工作内容是否符合承诺。",
        ),
    ),
    SyntheticScenario(
        "explicit_uncertainty",
        "你好。最近有什么让你想多说一点？",
        (
            "我在考虑毕业后的方向，但现在还没有形成明确结论。",
            "我还没想好，几种选择看起来都有道理。",
            "我至少知道自己不想只因为别人觉得稳定就决定，我想先弄清每天实际会做什么。",
        ),
    ),
    SyntheticScenario(
        "long_multi_view_answer",
        "你好。最近有什么让你想多说一点？",
        (
            "我们小组在讨论是否减少检查环节来赶进度。支持的人担心错过窗口，反对的人担心返工；我负责协调，也要考虑使用者受到的影响。",
            "我会先区分哪些检查关系到安全和数据完整性，哪些只是格式统一，再用过去几次发布的缺陷记录核实，而不是把所有检查一概保留。",
            "如果数据表明主要风险集中在两个关键环节，我会保留它们，同时把低风险检查改成抽样，并提前约定发现异常后的回退条件。",
        ),
    ),
    SyntheticScenario(
        "mature_plan_missing_condition",
        "你好。最近有什么让你想多说一点？",
        (
            "我准备先做一个小范围试点，记录完成时间、错误率和参与者反馈，再决定是否扩大。",
            "我会在两周后复盘，如果错误率下降并且参与者没有明显增加负担，就进入下一阶段。",
            "还没确定的是样本太少时怎么判断，我可能需要预先写清最低样本量和延长观察的条件。",
        ),
    ),
    SyntheticScenario(
        "refusal_and_topic_shift",
        "你好。最近有什么让你想多说一点？",
        (
            "有些家庭细节我不想展开。我更愿意谈最近一次项目合作里，我是怎么处理意见分歧的。",
            "我先分别问了大家最担心什么，发现一方担心延期，另一方担心质量问题。",
            "我没有替他们选答案，而是让双方一起确定一个可回退的小实验，并约好第二天复查结果。",
        ),
    ),
)


REVIEW_DIMENSIONS = (
    "evidence_grounded_empathy",
    "naturalness_and_human_likeness",
    "relevance_to_current_focus",
    "correction_or_clarification_repair",
    "question_novelty_and_information_value",
    "willingness_to_continue",
    "single_primary_question_and_safety",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _git_text(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=REPOSITORY_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def scenario_plan_payload() -> list[dict[str, Any]]:
    return [
        {
            "scenario_type": scenario.scenario_type,
            "opening": scenario.opening,
            "user_turns": list(scenario.user_turns),
        }
        for scenario in SCENARIOS
    ]


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_output_target(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise SystemExit("output_dir_must_be_absolute")
    if candidate.exists():
        raise SystemExit("output_dir_must_not_exist")
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SystemExit("output_dir_parent_must_exist") from exc
    resolved = resolved_parent / candidate.name
    if _path_is_within(resolved, REPOSITORY_ROOT):
        raise SystemExit("output_dir_must_be_outside_repository")
    return resolved


def create_private_directory(path: Path) -> None:
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)
    if path.stat().st_mode & 0o077:
        raise RunnerHardStop("output_directory_permissions_not_private")


def _write_private_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_private_json(path: Path, payload: Any) -> None:
    _write_private_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


class PrivateEventLedger:
    def __init__(self, path: Path) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._handle = os.fdopen(descriptor, "w", encoding="utf-8")
        self.path = path

    def append(self, event: dict[str, Any]) -> None:
        row = {"recorded_at_unix_ms": int(time.time() * 1000), **event}
        self._handle.write(_canonical_json(row) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
            os.chmod(self.path, 0o600)


def _safe_response_metadata(body: dict[str, Any]) -> dict[str, Any]:
    content: Any = None
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        pass
    usage = body.get("usage")
    safe_usage = usage if isinstance(usage, dict) else None
    return {
        "api_response_id": body.get("id") if isinstance(body.get("id"), str) else None,
        "api_raw_model": body.get("model"),
        "raw_content_sha256": _sha256_text(content) if isinstance(content, str) else None,
        "usage": safe_usage,
    }


class PhysicalCallAudit:
    """Count and gate each physical request before invoking ``httpx.post``."""

    def __init__(
        self,
        original_post: Callable[..., Any],
        ledger: PrivateEventLedger,
        *,
        max_requests: int = MAX_PHYSICAL_REQUESTS,
        expected_model: str = EXPECTED_MODEL,
    ) -> None:
        self.original_post = original_post
        self.ledger = ledger
        self.max_requests = max_requests
        self.expected_model = expected_model
        self.count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.count >= self.max_requests:
            self.ledger.append(
                {
                    "event": "physical_call_blocked",
                    "reason": "physical_request_budget_exhausted",
                    "physical_request_count": self.count,
                    "physical_request_cap": self.max_requests,
                }
            )
            raise RunnerHardStop("physical_request_budget_exhausted")

        # Count before the original callable can reach the network.
        self.count += 1
        call_index = self.count
        request_json = kwargs.get("json")
        request_fingerprint = (
            _sha256_text(_canonical_json(request_json))
            if isinstance(request_json, dict)
            else None
        )
        self.ledger.append(
            {
                "event": "physical_call_started",
                "physical_request_index": call_index,
                "physical_request_cap": self.max_requests,
                "request_payload_sha256": request_fingerprint,
                "requested_model": (
                    request_json.get("model") if isinstance(request_json, dict) else None
                ),
            }
        )
        try:
            response = self.original_post(*args, **kwargs)
        except BaseException as exc:
            self.ledger.append(
                {
                    "event": "physical_call_failed",
                    "physical_request_index": call_index,
                    "exception_type": type(exc).__name__,
                }
            )
            raise

        status_code = getattr(response, "status_code", None)
        event: dict[str, Any] = {
            "event": "physical_call_completed",
            "physical_request_index": call_index,
            "http_status_code": status_code,
        }
        if isinstance(status_code, int) and 200 <= status_code < 300:
            try:
                body = response.json()
            except BaseException as exc:
                event["response_json_error"] = type(exc).__name__
                self.ledger.append(event)
                raise RunnerHardStop("provider_response_not_json") from exc
            if not isinstance(body, dict):
                event["response_json_type"] = type(body).__name__
                self.ledger.append(event)
                raise RunnerHardStop("provider_response_not_object")
            event.update(_safe_response_metadata(body))
            self.ledger.append(event)
            if body.get("model") != self.expected_model:
                self.ledger.append(
                    {
                        "event": "run_hard_stop",
                        "reason": "api_raw_model_mismatch",
                        "physical_request_index": call_index,
                        "expected_model": self.expected_model,
                        "api_raw_model": body.get("model"),
                    }
                )
                raise RunnerHardStop("api_raw_model_mismatch")
        else:
            self.ledger.append(event)
        return response


def _question_count(message: str) -> int:
    return message.count("?") + message.count("？")


def audit_only_flags(message: str, latest_user: str, prior_messages: Iterable[str]) -> list[str]:
    flags: list[str] = []
    compact = re.sub(r"\s+", "", message)
    latest_compact = re.sub(r"\s+", "", latest_user)
    if _question_count(message) > 1:
        flags.append("multiple_primary_questions")
    if re.search(r"(?:A[、.]|B[、.]|二选一|选择[AB])", message, re.IGNORECASE):
        flags.append("option_format")
    if re.search(r"(?:我听到|你提到|听起来|我理解)[^，，]{0,60}[，,]", message):
        flags.append("formulaic_acknowledgement_opening")
    if latest_compact and len(latest_compact) >= 12:
        if any(
            latest_compact[index:index + 12] in compact
            for index in range(len(latest_compact) - 11)
        ):
            flags.append("repeated_user_wording")
    if re.search(r"(?:还想说什么|最想理清什么|具体指哪一部分)[？?]", message):
        flags.append("generic_low_information_question")
    normalized_opening = re.split(r"[。！？?!]", compact, maxsplit=1)[0]
    for prior in prior_messages:
        prior_opening = re.split(
            r"[。！？?!]", re.sub(r"\s+", "", prior), maxsplit=1
        )[0]
        if normalized_opening and normalized_opening == prior_opening:
            flags.append("repeated_opening_within_arm")
            break
    return sorted(set(flags))


def _source_hashes() -> dict[str, str]:
    sources = (
        Path("backend/app/core/config.py"),
        Path("backend/app/schemas.py"),
        Path("backend/app/services/model_gateway.py"),
        Path("backend/scripts/compare_natural_interviewer_prompts_v604.py"),
    )
    return {
        str(path): _sha256_file(REPOSITORY_ROOT / path)
        for path in sources
    }


def _base_manifest(output_dir: Path) -> dict[str, Any]:
    prompt_assets: dict[str, Any] = {}
    for version in PROMPT_VERSIONS:
        prompt_id, resolved_version, prompt = resolve_natural_interviewer_prompt(version)
        prompt_assets[version] = {
            "prompt_id": prompt_id,
            "prompt_version": resolved_version,
            "prompt_sha256": _sha256_text(prompt),
        }
    return {
        "protocol": RUNNER_PROTOCOL,
        "status": "running",
        "stop_reason": None,
        "synthetic_only": True,
        "reads_application_database": False,
        "automated_scoring_performed": False,
        "expected_api_raw_model": EXPECTED_MODEL,
        "normal_logical_call_plan": NORMAL_LOGICAL_CALLS,
        "logical_calls_completed": 0,
        "physical_request_cap": MAX_PHYSICAL_REQUESTS,
        "physical_requests_observed": 0,
        "repair_calls_observed": 0,
        "arms_finished_early": [],
        "output_directory": str(output_dir),
        "git_commit_sha": _git_text("rev-parse", "HEAD"),
        "git_status_sha256": _sha256_text(_git_text("status", "--porcelain=v1")),
        "source_sha256": _source_hashes(),
        "prompt_assets": prompt_assets,
        "scenario_plan_sha256": _sha256_text(_canonical_json(scenario_plan_payload())),
    }


def _persist_outputs(
    *,
    output_dir: Path,
    manifest: dict[str, Any],
    blind_cases: list[dict[str, Any]],
    sealed_mapping: dict[str, Any],
) -> None:
    _write_private_json(output_dir / "run_manifest.json", manifest)
    _write_private_json(
        output_dir / "blind_review_packet.json",
        {
            "protocol": RUNNER_PROTOCOL,
            "synthetic_only": True,
            "automated_scoring_performed": False,
            "review_dimensions": list(REVIEW_DIMENSIONS),
            "instructions": (
                "Review each opaque candidate independently. Do not infer prompt identity; "
                "record human judgments outside this packet."
            ),
            "cases": blind_cases,
        },
    )
    _write_private_json(output_dir / "sealed" / "opaque_mapping.json", sealed_mapping)


def run_plan(
    *,
    output_dir: Path,
    gateway: ModelGatewayService,
    ledger: PrivateEventLedger,
    physical_audit: PhysicalCallAudit,
) -> dict[str, Any]:
    manifest = _base_manifest(output_dir)
    sealed_mapping: dict[str, Any] = {
        "protocol": RUNNER_PROTOCOL,
        "case_key": [],
        "candidate_arm_key": [],
    }
    blind_cases: list[dict[str, Any]] = []
    logical_records: list[dict[str, Any]] = []

    for scenario_index, scenario in enumerate(SCENARIOS):
        case_id = _opaque_id("case")
        sealed_mapping["case_key"].append(
            {
                "case_id": case_id,
                "scenario_type": scenario.scenario_type,
                "scenario_sha256": _sha256_text(
                    _canonical_json(
                        {
                            "opening": scenario.opening,
                            "user_turns": list(scenario.user_turns),
                        }
                    )
                ),
            }
        )
        candidate_entries: list[dict[str, Any]] = []
        versions = PROMPT_VERSIONS if scenario_index % 2 == 0 else tuple(reversed(PROMPT_VERSIONS))
        for version in versions:
            prompt_id, resolved_version, prompt = resolve_natural_interviewer_prompt(version)
            candidate_id = _opaque_id("cand")
            sealed_mapping["candidate_arm_key"].append(
                {
                    "case_id": case_id,
                    "candidate_id": candidate_id,
                    "prompt_id": prompt_id,
                    "prompt_version": resolved_version,
                    "prompt_sha256": _sha256_text(prompt),
                }
            )
            transcript: list[dict[str, str]] = [
                {"role": "assistant", "content": scenario.opening}
            ]
            visible_rounds: list[dict[str, Any]] = []
            prior_outputs: list[str] = []
            for round_index, user_text in enumerate(scenario.user_turns, start=1):
                transcript.append({"role": "user", "content": user_text})
                payload = {
                    "participant": {"display_name": "合成参与者"},
                    "transcript": list(transcript),
                }
                before_physical = physical_audit.count
                result = gateway._typed_call(
                    system_prompt=prompt,
                    payload=payload,
                    schema=NaturalInterviewerOutput,
                )
                after_physical = physical_audit.count
                message = result.output.interviewer_message.strip()
                flags = audit_only_flags(message, user_text, prior_outputs)
                record = {
                    "case_id": case_id,
                    "candidate_id": candidate_id,
                    "round_index": round_index,
                    "input_payload_sha256": _sha256_text(_canonical_json(payload)),
                    "output_text_sha256": _sha256_text(message),
                    "session_action": result.output.session_action,
                    "finish_reason": result.output.finish_reason,
                    "repair_used": result.repair_used,
                    "latency_ms": result.latency_ms,
                    "physical_requests_for_logical_call": after_physical - before_physical,
                    "audit_only_quality_flags": flags,
                }
                logical_records.append(record)
                visible_rounds.append(
                    {
                        "round_index": round_index,
                        "user_text": user_text,
                        "assistant_text": message,
                        "session_action": result.output.session_action,
                        "finish_reason": result.output.finish_reason,
                    }
                )
                transcript.append({"role": "assistant", "content": message})
                prior_outputs.append(message)
                manifest["logical_calls_completed"] = len(logical_records)
                manifest["physical_requests_observed"] = physical_audit.count
                manifest["repair_calls_observed"] = sum(
                    1 for item in logical_records if item["repair_used"]
                )
                if result.output.session_action == "finish":
                    manifest["arms_finished_early"].append(
                        {
                            "case_id": case_id,
                            "candidate_id": candidate_id,
                            "after_round": round_index,
                            "finish_reason": result.output.finish_reason,
                        }
                    )
                    break
            candidate_entries.append(
                {
                    "candidate_id": candidate_id,
                    "opening": scenario.opening,
                    "rounds": visible_rounds,
                }
            )
        # Candidate order is opaque and independent of arm execution order.
        candidate_entries.sort(key=lambda item: item["candidate_id"])
        blind_cases.append({"case_id": case_id, "candidates": candidate_entries})

    manifest["status"] = "completed"
    manifest["stop_reason"] = "normal_completion"
    manifest["logical_call_records"] = logical_records
    manifest["physical_requests_observed"] = physical_audit.count
    manifest["repair_calls_observed"] = sum(
        1 for item in logical_records if item["repair_used"]
    )
    _persist_outputs(
        output_dir=output_dir,
        manifest=manifest,
        blind_cases=blind_cases,
        sealed_mapping=sealed_mapping,
    )
    return manifest


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-real", action="store_true")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--max-physical-requests",
        type=int,
        default=MAX_PHYSICAL_REQUESTS,
        help="Frozen protocol value; values other than 80 are rejected.",
    )
    return parser.parse_args(argv)


def execute(argv: list[str] | None = None) -> dict[str, Any]:
    args = _parse_args(argv)
    # All refusal checks happen before directory creation or monkeypatching.
    if not args.execute_real:
        raise SystemExit("missing_execute_real")
    if args.confirmation != CONFIRMATION:
        raise SystemExit("missing_or_invalid_confirmation")
    if args.max_physical_requests != MAX_PHYSICAL_REQUESTS:
        raise SystemExit("physical_request_cap_must_equal_80")
    output_dir = validate_output_target(args.output_dir)
    if settings.model_gateway_mode != "real":
        raise SystemExit("model_gateway_mode_must_be_real")
    if settings.deepseek_model != EXPECTED_MODEL:
        raise SystemExit("configured_model_must_equal_deepseek_v4_flash")
    if not settings.deepseek_api_key.strip():
        raise SystemExit("missing_deepseek_api_key")

    previous_umask = os.umask(0o077)
    ledger: PrivateEventLedger | None = None
    original_post = gateway_module.httpx.post
    output_created = False
    manifest: dict[str, Any] | None = None
    try:
        create_private_directory(output_dir)
        output_created = True
        sealed_dir = output_dir / "sealed"
        create_private_directory(sealed_dir)
        ledger = PrivateEventLedger(output_dir / "events.jsonl")
        physical_audit = PhysicalCallAudit(original_post, ledger)
        gateway_module.httpx.post = physical_audit
        gateway = ModelGatewayService()
        manifest = run_plan(
            output_dir=output_dir,
            gateway=gateway,
            ledger=ledger,
            physical_audit=physical_audit,
        )
        return manifest
    except RunnerHardStop as exc:
        if ledger is not None:
            ledger.append({"event": "run_stopped", "reason": str(exc)})
        if output_created:
            stopped = manifest or _base_manifest(output_dir)
            stopped.update(
                {
                    "status": "stopped",
                    "stop_reason": str(exc),
                    "physical_requests_observed": (
                        physical_audit.count if "physical_audit" in locals() else 0
                    ),
                }
            )
            _write_private_json(output_dir / "run_manifest.json", stopped)
        raise SystemExit(f"run_hard_stopped:{exc}") from exc
    except BaseException as exc:
        if ledger is not None:
            ledger.append(
                {
                    "event": "run_stopped",
                    "reason": "unhandled_runner_error",
                    "exception_type": type(exc).__name__,
                }
            )
        if output_created:
            stopped = manifest or _base_manifest(output_dir)
            stopped.update(
                {
                    "status": "stopped",
                    "stop_reason": "unhandled_runner_error",
                    "exception_type": type(exc).__name__,
                    "physical_requests_observed": (
                        physical_audit.count if "physical_audit" in locals() else 0
                    ),
                }
            )
            _write_private_json(output_dir / "run_manifest.json", stopped)
        raise
    finally:
        gateway_module.httpx.post = original_post
        if ledger is not None:
            ledger.close()
        os.umask(previous_umask)


def main() -> None:
    manifest = execute()
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "logical_calls_completed": manifest["logical_calls_completed"],
                "physical_requests_observed": manifest["physical_requests_observed"],
                "output_directory": manifest["output_directory"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
