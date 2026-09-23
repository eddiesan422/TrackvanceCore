"""Physically separate credential store for write-capable Delivery destinations."""

import os
from pathlib import Path

from .config import STORAGE_DIR
from .credential_store import EncryptedFileSecretStore, SecretStore

destination_secret_store: SecretStore = EncryptedFileSecretStore(
    Path(
        os.getenv(
            "TRACKVANCE_DESTINATION_SECRETS_DIR",
            str(STORAGE_DIR.parent / "delivery_credentials"),
        )
    ),
    Path(
        os.getenv(
            "TRACKVANCE_DESTINATION_SECRET_KEY_FILE",
            str(STORAGE_DIR.parent / "delivery_keys" / "master.key"),
        )
    ),
)
