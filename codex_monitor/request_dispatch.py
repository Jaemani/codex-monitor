"""Bounded delivery of durable request notifications through normal intake."""

from .errors import IngressError


class RequestDispatcher:
    """Keep recorded request state separate from notification delivery."""

    def __init__(self, monitor, requests, *, batch_size=32):
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("request batch size must be between 1 and 100")
        self.monitor = monitor
        self.requests = requests
        self.batch_size = batch_size

    def pump(self):
        expired = self.requests.expire_due(limit=self.batch_size)
        notifications = self.requests.pending_notifications(limit=self.batch_size)
        if not notifications:
            return bool(expired)
        bindings = {row["name"]: row for row in self.monitor.bindings()}
        processed = bool(expired)
        for item in notifications:
            notification_id = item["notification_id"]
            binding = bindings.get(item["original_binding"])
            if binding is not None and not binding["enabled"]:
                # Defer without consuming failure attempts. This also moves a
                # paused batch out of the way of later runnable notifications.
                self.requests.notification_deferred(
                    notification_id, "original binding is paused", retry_after=1,
                )
                continue
            try:
                if (binding is None or item["source"] not in binding["sources"]
                        or binding["thread"] != item["envelope"]["data"]["conversation_id"]):
                    raise IngressError("original request binding or source is unavailable", 403)
                receipt = self.monitor.ingest(item["original_binding"], item["envelope"])
            except Exception as exc:
                message = str(exc).strip() or type(exc).__name__
                self.requests.notification_error(
                    notification_id, message.encode("utf-8")[:1000].decode("utf-8", errors="ignore"),
                )
                continue
            # If this local acknowledgement fails, the next pump retries the
            # same event ID. Monitor.ingest deduplicates its durable receipt.
            self.requests.notification_accepted(notification_id, receipt["delivery_id"])
            processed = True
        return processed
