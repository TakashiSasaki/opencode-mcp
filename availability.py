"""Project provider-result evidence without inferring a remaining quota."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Literal, NotRequired, TypedDict


class ModelObservation(TypedDict):
    model: str
    state: Literal["last_request_succeeded", "last_request_failed", "quota_exhausted", "rate_limited", "retry_due"]
    occurred_at: str
    checked_at: str
    session_id: str
    message_id: str
    error_type: str | None
    http_status: int | None
    retry_at: str | None
    retry_after_seconds: int | None
    retry_estimated: bool
    quota_remaining: None


class ObservationScan(TypedDict):
    checked_at: str
    source: Literal["recent_session_messages"]
    directory: str
    session_limit: int
    sessions_examined: int
    sessions_truncated: bool
    messages_per_session: int
    partial: bool
    issues: list[str]


class ModelSummary(TypedDict):
    id: NotRequired[str]
    providerID: NotRequired[str]
    name: NotRequired[str]
    status: NotRequired[str]
    enabled: NotRequired[bool]
    cost: NotRequired[list[dict[str, Any]]]
    limit: NotRequired[dict[str, Any]]
    capabilities: NotRequired[dict[str, Any]]
    model: str
    reasoning_efforts: list[str]
    supports_reasoning_effort_selection: bool
    pricing_status: str
    cost_unit: str
    free_by_catalog: bool | None
    quota_available: Literal["unknown"]
    last_observation: ModelObservation | None


class ModelListResult(TypedDict):
    models: list[ModelSummary]
    count: int
    free_only: bool
    pricing_note: str
    availability_note: str
    observation_scan: ObservationScan | None


class UsageResult(TypedDict):
    supported: bool
    limits: Any
    limits_available: bool
    reason: NotRequired[str]
    free_promotional_model_quota: Literal["unknown"]
    model_observations: list[ModelObservation]
    observation_scan: ObservationScan | None
    availability_note: str


OBSERVATION_NOTE = (
    "Recent provider responses, not a live inference probe or remaining-quota balance. "
    "Unknown or expired retry evidence does not mean available. Success does not guarantee the next request. "
    "Only the latest five messages of each sampled session are inspected. Sessions are scoped to their current "
    "workspace; moved sessions may include results from their previous location/configuration. "
    "A model's limit does not establish whether another model shares its quota."
)


def iso_time(milliseconds: int | float) -> str | None:
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)):
        return None
    if not math.isfinite(milliseconds):
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def message_observation(message: dict[str, Any], session_id: str, now_ms: int) -> ModelObservation | None:
    """Use the response's own model and timestamp, never the session's selected model."""
    model = message.get("model") or {}
    if message.get("type") != "assistant" or not isinstance(model, dict) or not model.get("providerID") or not model.get("id"):
        return None
    timing = message.get("time") or {}
    occurred_ms = timing.get("completed", timing.get("created"))
    occurred = iso_time(occurred_ms)
    if occurred is None:
        return None
    error = message.get("error")
    if error is not None and not isinstance(error, dict):
        return None
    if not error and (not timing.get("completed") or message.get("finish") not in ("stop", "length", "tool-calls")):
        return None
    error_type = None
    http_status = None
    retry_at = None
    retry_seconds = None
    state = "last_request_succeeded"
    if error:
        # No response bodies, headers, prompts, reasoning or arbitrary error text.
        raw_type = error.get("type")
        error_type = raw_type if isinstance(raw_type, str) and raw_type.startswith("provider.") and len(raw_type) <= 100 else "provider_error"
        raw_status = error.get("status")
        http_status = raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None
        state = "quota_exhausted" if error_type == "provider.free-limit" else "rate_limited" if http_status == 429 else "last_request_failed"
        delay = error.get("retryAfterMs")
        if isinstance(delay, (int, float)) and not isinstance(delay, bool) and math.isfinite(delay) and delay >= 0:
            retry_ms = occurred_ms + delay
            retry_at = iso_time(retry_ms)
            if retry_at is not None:
                retry_seconds = max(0, math.ceil((retry_ms - now_ms) / 1000))
                if retry_ms <= now_ms:
                    state = "retry_due"
    return {"model": f"{model['providerID']}/{model['id']}", "state": state,
            "occurred_at": occurred, "checked_at": iso_time(now_ms),
            "session_id": session_id, "message_id": message.get("id", ""),
            "error_type": error_type, "http_status": http_status, "retry_at": retry_at,
            "retry_after_seconds": retry_seconds, "retry_estimated": retry_at is not None,
            "quota_remaining": None}


def merge_observations(target: dict[str, ModelObservation], messages: list[dict[str, Any]], session_id: str, now_ms: int) -> None:
    for message in messages:
        item = message_observation(message, session_id, now_ms)
        if item is None:
            continue
        previous = target.get(item["model"])
        if previous is None or item["occurred_at"] > previous["occurred_at"]:
            target[item["model"]] = item
