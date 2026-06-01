from decompute_gateway.secret_scanner import SecretScanner


def test_secret_scanner_detects_common_tokens():
    text = "ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz123456"
    findings = SecretScanner().scan(text)
    assert findings
    assert any(f.kind in {"anthropic_key", "env_secret_assignment"} for f in findings)


def test_secret_redaction():
    scanner = SecretScanner()
    text = "password=supersecret123"
    redacted = scanner.redact(text)
    assert "supersecret" not in redacted
    assert "REDACTED" in redacted


def test_finding_never_leaks_secret_material():
    """The masked preview must not contain any reconstructable secret characters."""
    key = "sk-ant-api03-SECRETKEY1234567890abcdefghij"
    findings = SecretScanner().scan(f"ANTHROPIC_API_KEY={key}")
    assert findings
    for f in findings:
        # no run of >=8 of the actual secret should appear in the stored masked form
        assert "SECRETKEY" not in f.masked
        assert "abcdefghij" not in f.masked
        assert f.masked.startswith("<") and "chars>" in f.masked


def test_detects_user_pii():
    scanner = SecretScanner()
    findings = scanner.scan("Customer alice@customer.com called from 415-555-1234, SSN 123-45-6789")
    kinds = {f.kind for f in findings}
    assert "email" in kinds
    assert "phone" in kinds
    assert "ssn" in kinds
    assert scanner.has_pii(findings) is True


def test_pii_redaction_removes_user_details():
    scanner = SecretScanner()
    redacted = scanner.redact("Contact alice@customer.com about order")
    assert "alice@customer.com" not in redacted
    assert "[REDACTED:email]" in redacted


# ── scrub(): outbound privacy filter (secrets hard-redacted, PII per mode) ──
def test_scrub_secrets_always_hard_redacted_even_in_tokenize_mode():
    scanner = SecretScanner(mode="tokenize")
    out = scanner.scrub("deploy key sk-ant-abcdefghijklmnopqrstuvwxyz012345 here")
    assert "sk-ant-" not in out
    assert "[REDACTED:anthropic_key]" in out  # secret removed, not tokenized


def test_scrub_tokenizes_pii_with_stable_tokens():
    scanner = SecretScanner(mode="tokenize")
    out = scanner.scrub("from alice@corp.com and bob@corp.com and alice@corp.com again")
    assert "alice@corp.com" not in out and "bob@corp.com" not in out
    assert out.count("<email_1>") == 2  # same value -> same token
    assert "<email_2>" in out           # distinct value -> distinct token


def test_scrub_redact_mode_removes_pii():
    scanner = SecretScanner(mode="redact")
    out = scanner.scrub("contact alice@corp.com")
    assert "alice@corp.com" not in out
    assert "[REDACTED:email]" in out


def test_scrub_keeps_pii_when_not_scrubbing_but_still_kills_secrets():
    """The user's own latest instruction: keep their PII, still remove secrets."""
    scanner = SecretScanner(mode="tokenize")
    out = scanner.scrub("email alice@corp.com using key sk-ant-abcdefghijklmnopqrstuvwxyz012345",
                        scrub_pii=False)
    assert "alice@corp.com" in out                 # deliberate user PII kept
    assert "sk-ant-" not in out                     # secret still removed
    assert "[REDACTED:anthropic_key]" in out
