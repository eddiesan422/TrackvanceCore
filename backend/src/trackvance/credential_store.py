"""Credential port and authenticated encrypted local implementation.

Metadata contains opaque references only. Keys live in a separate Docker volume;
an object-storage or artifact backup never contains the decryption key. Future
Vault/Key Vault/Secrets Manager adapters implement this port, not DatasetSource.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from .config import STORAGE_DIR


class SecretStoreError(Exception):
    """Safe error: never contains a path, ciphertext or plaintext credential."""


@runtime_checkable
class SecretStore(Protocol):
    def put(self, organization_id: str, secret: str) -> str: ...

    def get(self, organization_id: str, reference: str) -> str: ...

    def delete(self, organization_id: str, reference: str) -> None: ...


class EncryptedFileSecretStore:
    def __init__(self, root: Path, key_path: Path):
        self.root = root.resolve()
        self.key_path = key_path.resolve()

    @staticmethod
    def _write_private(path: Path, content: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())

    def _cipher(self, *, create: bool = False) -> Fernet:
        try:
            if not self.key_path.exists() and create:
                if any(self.root.glob("*/*.secret")) and not self.key_path.exists():
                    raise SecretStoreError("Falta la clave del almacén existente. Restaura su copia de seguridad.")
                # Atomic publication avoids a second process reading a partial key.
                self.key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = self.key_path.with_name(f".{uuid4().hex}.key")
                try:
                    self._write_private(temporary, Fernet.generate_key())
                    try:
                        os.link(temporary, self.key_path)
                    except FileExistsError:
                        pass
                finally:
                    temporary.unlink(missing_ok=True)
            return Fernet(self.key_path.read_bytes())
        except (OSError, ValueError):
            raise SecretStoreError("El almacén de credenciales no está disponible.") from None

    def _path(self, organization_id: str, reference: str) -> Path:
        if not organization_id or not re.fullmatch(r"local:[a-f0-9]{32}", reference):
            raise SecretStoreError("Referencia de credencial inválida.")
        scope = hashlib.sha256(organization_id.encode()).hexdigest()
        return self.root / scope / f"{reference[6:]}.secret"

    def put(self, organization_id: str, secret: str) -> str:
        if not isinstance(secret, str) or not secret or len(secret.encode()) > 8192:
            raise SecretStoreError("La credencial no es válida.")
        reference = f"local:{uuid4().hex}"
        target = self._path(organization_id, reference)
        payload = json.dumps({"organization_id": organization_id, "reference": reference,
                              "secret": secret}).encode()
        encrypted = self._cipher(create=True).encrypt(payload)
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._write_private(target, encrypted)
        except OSError:
            raise SecretStoreError("No fue posible guardar la credencial.") from None
        return reference

    def get(self, organization_id: str, reference: str) -> str:
        try:
            encrypted = self._path(organization_id, reference).read_bytes()
            payload = json.loads(self._cipher().decrypt(encrypted))
            if payload["organization_id"] != organization_id or payload["reference"] != reference:
                raise ValueError("scope")
            return str(payload["secret"])
        except (OSError, ValueError, KeyError, InvalidToken):
            raise SecretStoreError("No fue posible recuperar la credencial de esta organización.") from None

    def delete(self, organization_id: str, reference: str) -> None:
        try:
            self._path(organization_id, reference).unlink(missing_ok=True)
        except OSError:
            raise SecretStoreError("No fue posible retirar la credencial.") from None


secret_store: SecretStore = EncryptedFileSecretStore(
    Path(os.getenv("TRACKVANCE_SECRETS_DIR", str(STORAGE_DIR.parent / "credentials"))),
    Path(os.getenv("TRACKVANCE_SECRET_KEY_FILE", str(STORAGE_DIR.parent / "keys" / "master.key"))),
)
