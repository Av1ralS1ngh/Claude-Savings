#!/usr/bin/env python
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from decompute_gateway.config import get_settings
from decompute_gateway.db import Store


def main() -> None:
    settings = get_settings()
    store = Store(settings)
    if settings.uses_sqlite:
        store.create_schema_for_local_dev()
    email = os.getenv("DECOMPUTE_BOOTSTRAP_ADMIN_EMAIL", "admin@local")
    result = store.bootstrap_admin(email=email)
    print("Created bootstrap admin API key. Store it securely; it is shown once.\n")
    print(f"project_id={result['project_id']}")
    print(f"api_key_id={result['api_key_id']}")
    print(f"api_key={result['api_key']}")


if __name__ == "__main__":
    main()
