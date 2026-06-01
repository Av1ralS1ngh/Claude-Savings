import sys


def test_no_legacy_yellowstone_loaded():
    assert not any("yellowstone_legacy" in name for name in sys.modules)
