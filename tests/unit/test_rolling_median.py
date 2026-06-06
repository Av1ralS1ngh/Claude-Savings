"""Headline savings median is a ROLLING median over the latest ROLLING_MEDIAN_WINDOW (50)
requests, and the dashboard defaults to the 1h window."""
import pytest
from cryptography.fernet import Fernet

from decompute_gateway.config import Settings
from decompute_gateway.db import Store, ROLLING_MEDIAN_WINDOW


@pytest.fixture()
def store(tmp_path):
    s = Store(Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path}/rm.db",
        ccr_encryption_key=Fernet.generate_key().decode(),
        ccr_enabled=True,
    ))
    s.create_schema_for_local_dev()
    return s


def _record(store, pid, *, raw, final, n):
    for _ in range(n):
        store.record_usage(
            {"endpoint": "/v1/messages", "model": "claude-haiku-4-5",
             "raw_input_tokens": raw, "yscompress_input_tokens": final,
             "yellowstone_final_input_tokens": final, "output_tokens": 10,
             "tokens_saved": max(0, raw - final)},
            project_id=pid, key_prefix="dc_test",
        )


def test_window_constant_is_50():
    assert ROLLING_MEDIAN_WINDOW == 50


def test_median_is_rolling_over_latest_50_not_whole_history(store):
    pid = store.create_project("RM")["id"]
    _record(store, pid, raw=1000, final=1000, n=50)  # 50 OLD requests @ 0% savings
    _record(store, pid, raw=1000, final=100, n=50)   # 50 NEW requests @ 90% savings (most recent)

    u = store.usage_summary(project_id=pid, since=None)
    # The count is over ALL requests...
    assert u["requests"] == 100
    # ...but the headline median is over only the LATEST 50 (all 90%), NOT diluted to ~45%
    # by the older 0% requests.
    assert u["median_savings_percent"] >= 85, u["median_savings_percent"]
    assert u["latest_savings_percent"] >= 85


def test_default_dashboard_window_is_1h():
    import decompute_gateway.main as M
    assert M._DASHBOARD_DEFAULT_RANGE == "1h"
