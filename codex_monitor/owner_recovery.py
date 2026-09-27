"""Explicit, one-shot authentication recovery for an existing owner.

This module never logs in, changes accounts, restarts processes, or replays work.
The caller must revalidate the selected route before invoking it.
"""

from __future__ import annotations

import time

from .owner_health import OwnerProbeError, _OwnerRpc, _RpcError, _auth_error, _supported_endpoint, _unsupported


def retry_owner_auth(endpoint: str, *, token: str | None = None, rpc_factory=None) -> str:
    """Request one managed-token refresh, then check account service access."""
    if not _supported_endpoint(endpoint):
        return "An explicit owner endpoint is required. Open the original Codex client to sign in."
    deadline = time.monotonic() + 12.0
    rpc = None

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise OwnerProbeError("unavailable")
        return value

    def call(method, params):
        return rpc.call(method, params, timeout=remaining())

    try:
        rpc = (rpc_factory or _OwnerRpc)(endpoint, timeout=min(3.0, remaining()), token=token)
        call("initialize", {"clientInfo": {"name": "codex-monitor-auth-retry", "version": "0.1.0"},
                            "capabilities": {"experimentalApi": True}})
        rpc.notify("initialized", {})
        current = call("account/read", {"refreshToken": False})
        account = current.get("account") if isinstance(current, dict) else None
        if not isinstance(account, dict):
            return "No managed login found. Sign in through the owning Codex client, then retry."
        if account.get("type") != "chatgpt":
            return "This owner does not report a managed ChatGPT login. Reauthenticate in its owning client."
        refreshed = call("account/read", {"refreshToken": True})
        if not isinstance(refreshed, dict) or not isinstance(refreshed.get("account"), dict):
            return "Authentication is still unavailable. Sign in through the owning Codex client, then retry."
        # Some servers can return account presence even after a refresh failure.
        # A separate authenticated request is evidence of access, not model work.
        limits = call("account/rateLimits/read", {})
        if not isinstance(limits, dict) or not any(isinstance(limits.get(k), dict)
                                                  for k in ("rateLimits", "rateLimitsByLimitId")):
            return "Refresh requested; account access remains unverified. Open in Codex to inspect."
        return "Account access verified. Open in Codex to retry the failed work; no work was replayed."
    except _RpcError as exc:
        if _unsupported(exc):
            return "This owner does not support authentication retry or verification. Use its Codex client."
        if _auth_error(exc):
            return "Authentication still failed. Sign in through the owning Codex client, then retry."
        return "Authentication retry failed. Inspect the owning Codex client; no work was replayed."
    except OwnerProbeError as exc:
        if exc.kind == "auth-required":
            return "Owner access was rejected. Check the owner's connection credentials and login."
        return "Owner unavailable or retry timed out; result unknown. No automatic retry was scheduled."
    except Exception:
        return "Authentication retry could not complete. No automatic retry was scheduled."
    finally:
        if rpc is not None:
            rpc.close()
