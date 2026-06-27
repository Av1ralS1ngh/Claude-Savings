from __future__ import annotations


class DecomputeError(Exception):
    """Base error. Carries the HTTP status_code and parsed body when raised from a response, so callers
    (and CLI error explainers) can branch on them — not just the most-specific subclass."""

    def __init__(self, message: str = "", *, status_code: int | None = None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AuthError(DecomputeError):
    pass


class RateLimitError(DecomputeError):
    pass


class ProviderError(DecomputeError):
    pass


class CompressionError(DecomputeError):
    pass


class APIError(DecomputeError):
    def __init__(self, status_code: int, message: str, body=None):
        super().__init__(message, status_code=status_code, body=body)


def error_for_status(status_code: int, message: str, body=None) -> DecomputeError:
    """Map an HTTP status to the most specific Decompute error class.

    Lets SDK callers do `except RateLimitError` / `except AuthError` instead of
    pattern-matching on status codes. Every returned error carries `status_code` and `body`."""
    if status_code in (401, 403):
        return AuthError(message, status_code=status_code, body=body)
    if status_code == 429:
        return RateLimitError(message, status_code=status_code, body=body)
    if status_code in (413, 422):
        return CompressionError(message, status_code=status_code, body=body)
    if status_code >= 500:
        return ProviderError(message, status_code=status_code, body=body)
    return APIError(status_code, message, body)
