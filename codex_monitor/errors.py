class IngressError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Retryable(Exception):
    """Known not accepted; retry is safe."""


class Unavailable(Retryable):
    """Pre-submission connectivity or owner availability failure."""


class Uncertain(Exception):
    """Submission may have been accepted. Reconcile, never blindly replay."""


class Permanent(Exception):
    """Unsupported target or rejected operation; operator action required."""
