"""The performance chart plots one point per request with the NEWEST at the right edge, so
its end-dot equals the headline "Latest request" % exactly (no time-bucket averaging that
diluted a burst of requests into one slice). These guard that contract."""
from datetime import datetime, timezone, timedelta

from decompute_gateway.main import _dashboard_chart

NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _series(pcts):
    # oldest -> newest, one per minute ending "now"
    pts = [{"saved_pct": float(p),
            "created_at": NOW - timedelta(minutes=(len(pcts) - 1 - i))}
           for i, p in enumerate(pcts)]
    return {"points": pts, "t_start": pts[0]["created_at"], "t_end": pts[-1]["created_at"]}


def _dot_pct(ch):
    return round((220.0 - ch["dotY"]) / 2.0, 1)


def test_end_dot_is_the_latest_request_not_the_mean():
    # A burst: 4 round-trips of one turn. The chart must end at the LATEST (34), not mean (18.3).
    ch = _dashboard_chart(_series([6.2, 4.0, 29.0, 34.0]), NOW)
    assert ch["hasData"] is True
    assert _dot_pct(ch) == 34.0
    assert abs(ch["dotX"] - 760.0) < 0.1  # newest anchors the right edge


def test_single_point_renders_at_right_edge_with_visible_paths():
    ch = _dashboard_chart(_series([50.0]), NOW)
    assert ch["hasData"] is True
    assert _dot_pct(ch) == 50.0
    assert abs(ch["dotX"] - 760.0) < 0.1
    assert ch["savedLineD"] and ch["savedAreaD"]  # not empty for a lone point


def test_empty_series_has_no_data():
    ch = _dashboard_chart({"points": [], "t_start": None, "t_end": None}, NOW)
    assert ch["hasData"] is False
    assert ch["dotX"] == -10 and ch["dotY"] == -10


def test_axis_labels_track_point_times_rightmost_is_now():
    ch = _dashboard_chart(_series([10.0, 20.0, 30.0, 40.0, 50.0]), NOW)
    assert ch["xLabels"][-1] == "now"          # newest point
    assert ch["xLabels"][0] == "4m"            # oldest of 5 one-per-minute points
