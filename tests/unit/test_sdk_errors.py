from decompute_ai.errors import AuthError, RateLimitError, ProviderError, CompressionError


def test_error_classes_are_importable():
    assert issubclass(AuthError, Exception)
    assert issubclass(RateLimitError, Exception)
    assert issubclass(ProviderError, Exception)
    assert issubclass(CompressionError, Exception)
