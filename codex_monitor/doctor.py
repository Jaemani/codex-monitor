"""Bounded, read-only compatibility diagnostics for the doctor command."""

from __future__ import annotations

import re

from .session import RpcError


# This is the oldest Codex build exercised for the official shared queue
# contract in this repository.  It is evidence for a tested baseline, not a
# claim that every later build is compatible.
SUPPORTED_QUEUE_BASELINE = "0.153.4"
QUEUE_PROBE_METHOD = "thread/queue/list"


def consumer_readiness_diagnostic(
    consumer_ready: bool | str,
    *,
    endpoint: str,
    requested_surface: str,
) -> dict | None:
    """Return bounded guidance when a caller requires a verified consumer.

    Queue support and native consumption are separate facts.  In particular,
    ``shared-local`` can prove that a queue target is readable while it cannot
    prove that the owning client has the target loaded.  The diagnostic is
    intentionally read-only: it tells the operator what to verify and does
    not attempt to start, resume, or submit anything.
    """

    if consumer_ready is True:
        return None
    if consumer_ready == "unknown":
        reason = (
            "consumer readiness is unknown; shared-local queue support does not "
            "verify an owning client"
        )
        next_step = (
            "Shared-local cannot verify the consumer even after opening the task. "
            "If a supported explicit App Server endpoint is available, probe that owner with "
            "--thread and --require-consumer; otherwise verify consumption in "
            "the client separately and keep unattended readiness unverified."
        )
    else:
        reason = "consumer is not ready; no exact loaded owner was verified"
        next_step = (
            "Provide --thread for the exact loaded conversation on the selected "
            "App Server, then rerun doctor --require-consumer."
        )
    return {
        "ready": False,
        "endpoint": endpoint,
        "requested_surface": requested_surface,
        "reason": reason,
        "consumer_ready": consumer_ready,
        "consumer_required": True,
        "delivery_guarantee": "consumer readiness is required but not verified",
        "next_step": next_step,
        "read_only": True,
    }


def _is_unsupported_method(error: RpcError, method: str) -> bool:
    if error.code == -32601:
        return True
    if error.code != -32600:
        return False
    message = str(error)
    prefix = f"Invalid request: unknown variant `{method}`"
    return bool(
        message.startswith(prefix)
        and re.match(r", expected one of(?:\s|$)", message[len(prefix) :])
    )


def unsupported_method_result(
    error: RpcError,
    method: str,
    *,
    endpoint: str,
    requested_surface: str,
    thread: str | None = None,
) -> dict | None:
    """Return structured guidance for a server that lacks one read-only API.

    Other RPC errors return ``None`` so the caller can retain its existing
    transport or target failure handling.  The diagnostic deliberately omits
    the peer's free-form message because it may contain environment details.
    """

    if not _is_unsupported_method(error, method):
        return None
    result = {
        "ready": False,
        "endpoint": endpoint,
        "requested_surface": requested_surface,
        "reason": f"connected Codex App Server does not support {method}",
        "error_code": error.code,
        "missing_method": method,
        "fallback_used": False,
        "read_only": True,
        "queue_api": {"ready": False, "method": method},
        "consumer_ready": "unknown",
        "delivery_guarantee": "unavailable until the connected client supports this queue method",
        "supported_baseline": {
            "product": "Codex",
            "version": SUPPORTED_QUEUE_BASELINE,
            "scope": "official thread queue API used by this monitor path",
            "claim": "tested baseline; later versions require validation",
        },
        "next_step": (
            "Upgrade the Codex client to the tested 0.153.4 baseline or a later "
            "validated build, then rerun doctor."
        ),
    }
    if thread is not None:
        result["thread"] = thread
    return result


def probe_queue_target(
    rpc,
    thread: str,
    *,
    endpoint: str,
    requested_surface: str,
) -> dict | None:
    """Probe queue support once, without starting, resuming, or retrying.

    ``None`` means the method exists.  A structured result means the server
    explicitly reported that the method is unavailable.  All other errors are
    re-raised for the caller's existing bounded transport handling.
    """

    try:
        rpc.call(QUEUE_PROBE_METHOD, {"threadId": thread, "limit": 1})
    except RpcError as error:
        diagnostic = unsupported_method_result(
            error,
            QUEUE_PROBE_METHOD,
            endpoint=endpoint,
            requested_surface=requested_surface,
            thread=thread,
        )
        if diagnostic is not None:
            return diagnostic
        raise
    return None
