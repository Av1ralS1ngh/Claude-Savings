from pathlib import Path
from decompute_gateway.secret_scanner import SecretScanner
from decompute_gateway.yscompress import summarize_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "compression"


def test_latest_error_preserved_in_stack_trace():
    text = (FIXTURES / "stack_trace_python.txt").read_text() * 200
    compressed = summarize_text(text, max_chars=1000)
    assert "PermissionError" in compressed
    assert "Traceback" in compressed


def test_fixture_secret_detected():
    text = (FIXTURES / "secrets_should_redact.env").read_text()
    findings = SecretScanner().scan(text)
    assert findings
