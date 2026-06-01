from cryptography.fernet import Fernet
from decompute_gateway.config import Settings
from decompute_gateway.crypto import CCRCrypto
from decompute_gateway.db import Store
from decompute_gateway.secret_scanner import SecretScanner


def test_ccr_does_not_store_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMPUTE_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("DECOMPUTE_ENV", "local")
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/test.db", ccr_encryption_key=Fernet.generate_key().decode(), ccr_enabled=True)
    store = Store(settings)
    store.create_schema_for_local_dev()
    crypto = CCRCrypto(settings.ccr_encryption_key, settings.ccr_key_id)
    text = "normal log line\n" * 200
    item = store.put_ccr(project_id="proj_default", original_text=text, metadata={}, crypto=crypto, scanner=SecretScanner())
    db_bytes = (tmp_path / "test.db").read_bytes()
    assert b"normal log line" not in db_bytes
    restored = store.get_ccr(project_id="proj_default", ccr_id=item["id"], crypto=crypto)
    assert restored["original_text"] == text
