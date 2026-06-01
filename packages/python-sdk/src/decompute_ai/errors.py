from __future__ import annotations


class DecomputeError(Exception):
    pass


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
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def error_for_status(status_code: int, message: str, body=None) -> DecomputeError:
    """Map an HTTP status to the most specific Decompute error class.

    Lets SDK callers do `except RateLimitError` / `except AuthError` instead of
    pattern-matching on status codes."""
    if status_code in (401, 403):
        return AuthError(message)
    if status_code == 429:
        return RateLimitError(message)
    if status_code in (413, 422):
        return CompressionError(message)
    if status_code >= 500:
        return ProviderError(message)
    return APIError(status_code, message, body)
