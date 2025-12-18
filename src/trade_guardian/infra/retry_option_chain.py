from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3
    base_sleep_s: float = 0.5      # first backoff
    max_sleep_s: float = 4.0       # cap
    jitter_s: float = 0.15         # random +/- jitter
    timeout_s: float = 10.0        # if your client supports timeout, pass it through


@dataclass
class FetchAttemptRecord:
    ts_utc: str
    symbol: str
    provider: str
    attempt: int
    max_attempts: int
    ok: bool

    # request parameters you care about
    request_params: Dict[str, Any]

    # response/exception info
    reason: str = ""               # classified reason string
    http_status: Optional[int] = None
    error: str = ""                # exception message if any
    response_meta: Dict[str, Any] = None  # optional: headers, request_id, etc.
    response_snippet: str = ""     # short text snippet (safe length)


class OptionChainFetchError(RuntimeError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_snippet(s: Any, max_len: int = 400) -> str:
    if s is None:
        return ""
    try:
        txt = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    except Exception:
        txt = repr(s)
    txt = txt.replace("\n", " ").replace("\r", " ")
    return txt[:max_len]


def classify_empty_response(
    *,
    payload: Any,
    http_status: Optional[int] = None,
    error: Optional[BaseException] = None,
) -> str:
    """
    Return a stable machine-readable reason.
    Keep this conservative: it's for diagnostics, not logic.
    """
    if error is not None:
        msg = str(error).lower()
        if "timeout" in msg:
            return "timeout"
        if "rate" in msg or "429" in msg:
            return "rate_limited"
        if "403" in msg or "forbidden" in msg:
            return "forbidden"
        if "401" in msg or "unauthorized" in msg:
            return "unauthorized"
        if "not found" in msg or "404" in msg:
            return "not_found"
        if "connection" in msg or "ssl" in msg:
            return "network_error"
        return "exception"

    if http_status is not None:
        if http_status == 204:
            return "no_content"
        if http_status == 404:
            return "not_found"
        if http_status == 401:
            return "unauthorized"
        if http_status == 403:
            return "forbidden"
        if http_status == 429:
            return "rate_limited"
        if 500 <= http_status <= 599:
            return "server_error"
        if 400 <= http_status <= 499:
            return "client_error"

    # payload-level hints
    if payload is None:
        return "payload_none"
    if isinstance(payload, (list, tuple)) and len(payload) == 0:
        return "payload_empty_list"
    if isinstance(payload, dict) and len(payload.keys()) == 0:
        return "payload_empty_dict"

    # common shapes: provider returns {"errors":[...]} or {"message": "..."}
    if isinstance(payload, dict):
        if "error" in payload:
            return "payload_error_field"
        if "errors" in payload and payload.get("errors"):
            return "payload_errors"
        if "message" in payload and payload.get("message"):
            return "payload_message"
        # option-chain typical: chain/expirations missing
        for k in ("callExpDateMap", "putExpDateMap", "options", "data"):
            if k in payload and not payload.get(k):
                return f"payload_missing_{k}"

    return "unknown_empty_or_unexpected"


def _write_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def fetch_with_retry_and_diagnostics(
    *,
    symbol: str,
    provider: str,
    request_params: Dict[str, Any],
    fetch_fn: Callable[[Dict[str, Any]], Tuple[Any, Optional[int], Dict[str, Any]]],
    # fetch_fn contract:
    #   input: request_params
    #   return: (payload, http_status, response_meta)
    #   - http_status may be None if not available
    #   - response_meta may include request_id, headers, url, etc.
    retry: RetryConfig = RetryConfig(),
    diag_jsonl_path: Path = Path("cache") / "option_chain_failures.jsonl",
    # If True: only retry on transient reasons; if False: retry all failures
    retry_only_transient: bool = True,
) -> Any:
    """
    This function is meant to sit in your infra layer.
    It does:
      - retry/backoff
      - classify failures and write JSONL diagnostic records
    """

    transient_reasons = {
        "timeout",
        "network_error",
        "server_error",
        "rate_limited",
        "no_content",
        "unknown_empty_or_unexpected",
        "payload_none",
        "payload_empty_list",
        "payload_empty_dict",
    }

    last_error: Optional[BaseException] = None
    last_reason = "unknown"
    last_status: Optional[int] = None
    last_meta: Dict[str, Any] = {}

    for attempt in range(1, retry.max_attempts + 1):
        try:
            payload, http_status, response_meta = fetch_fn(request_params)
            last_status = http_status
            last_meta = response_meta or {}

            # treat "empty payload" as failure that can be retried (often transient)
            is_empty = payload is None
            if not is_empty and isinstance(payload, (list, tuple, dict)):
                is_empty = (len(payload) == 0)

            if is_empty:
                reason = classify_empty_response(payload=payload, http_status=http_status)
                last_reason = reason

                rec = FetchAttemptRecord(
                    ts_utc=_utc_now_iso(),
                    symbol=symbol,
                    provider=provider,
                    attempt=attempt,
                    max_attempts=retry.max_attempts,
                    ok=False,
                    request_params=request_params,
                    reason=reason,
                    http_status=http_status,
                    error="",
                    response_meta=response_meta or {},
                    response_snippet=_safe_snippet(payload),
                )
                _write_jsonl(diag_jsonl_path, asdict(rec))

                # decide retry
                if attempt < retry.max_attempts and (not retry_only_transient or reason in transient_reasons):
                    _sleep_backoff(attempt, retry)
                    continue

                raise OptionChainFetchError(f"{symbol}: empty option chain payload ({reason})")

            # success
            return payload

        except Exception as e:
            last_error = e
            reason = classify_empty_response(payload=None, http_status=last_status, error=e)
            last_reason = reason

            rec = FetchAttemptRecord(
                ts_utc=_utc_now_iso(),
                symbol=symbol,
                provider=provider,
                attempt=attempt,
                max_attempts=retry.max_attempts,
                ok=False,
                request_params=request_params,
                reason=reason,
                http_status=last_status,
                error=str(e),
                response_meta=last_meta or {},
                response_snippet="",
            )
            _write_jsonl(diag_jsonl_path, asdict(rec))

            if attempt < retry.max_attempts and (not retry_only_transient or reason in transient_reasons):
                _sleep_backoff(attempt, retry)
                continue

            raise

    # should never reach
    if last_error is not None:
        raise last_error
    raise OptionChainFetchError(f"{symbol}: failed to fetch option chain ({last_reason})")


def _sleep_backoff(attempt: int, retry: RetryConfig) -> None:
    # exponential backoff with jitter
    base = retry.base_sleep_s * (2 ** (attempt - 1))
    sleep_s = min(retry.max_sleep_s, base)
    sleep_s += random.uniform(-retry.jitter_s, retry.jitter_s)
    sleep_s = max(0.0, sleep_s)
    time.sleep(sleep_s)
