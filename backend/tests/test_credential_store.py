from concurrent.futures import ThreadPoolExecutor

import pytest

from trackvance.credential_store import EncryptedFileSecretStore, SecretStore, SecretStoreError


def test_encrypted_credentials_survive_restart_and_stay_outside_artifacts(tmp_path):
    store = EncryptedFileSecretStore(tmp_path / "credentials", tmp_path / "keys/master.key")
    assert isinstance(store, SecretStore)
    secret = "sensitive-password-ñ"
    reference = store.put("org-a", secret)
    assert secret not in reference
    assert all(secret.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    restarted = EncryptedFileSecretStore(store.root, store.key_path)
    assert restarted.get("org-a", reference) == secret
    with pytest.raises(SecretStoreError):
        restarted.get("org-b", reference)
    store.delete("org-b", reference)
    assert restarted.get("org-a", reference) == secret
    store.delete("org-a", reference)
    with pytest.raises(SecretStoreError):
        store.get("org-a", reference)


def test_tampering_and_cross_organization_relocation_fail_closed(tmp_path):
    store = EncryptedFileSecretStore(tmp_path / "credentials", tmp_path / "keys/master.key")
    reference = store.put("org-a", "private-value")
    encrypted = store._path("org-a", reference).read_bytes()
    other = store._path("org-b", reference)
    other.parent.mkdir()
    other.write_bytes(encrypted)
    with pytest.raises(SecretStoreError):
        store.get("org-b", reference)
    store._path("org-a", reference).write_bytes(encrypted[:-10] + b"tampered")
    with pytest.raises(SecretStoreError) as error:
        store.get("org-a", reference)
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize("reference", ["../master.key", "local:../../secret", "local:abc", ""])
def test_invalid_references_cannot_escape_scope(tmp_path, reference):
    store = EncryptedFileSecretStore(tmp_path / "credentials", tmp_path / "keys/master.key")
    with pytest.raises(SecretStoreError):
        store.get("org-a", reference)
    with pytest.raises(SecretStoreError):
        store.delete("org-a", reference)


def test_lost_master_key_is_not_silently_replaced(tmp_path):
    store = EncryptedFileSecretStore(tmp_path / "credentials", tmp_path / "keys/master.key")
    store.put("org-a", "private-value")
    store.key_path.unlink()
    with pytest.raises(SecretStoreError, match="Falta la clave"):
        store.put("org-a", "new-value")
    assert not store.key_path.exists()


def test_concurrent_initialization_uses_one_complete_key(tmp_path):
    def save(index):
        store = EncryptedFileSecretStore(tmp_path / "credentials", tmp_path / "keys/master.key")
        return store.put("org-a", f"secret-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        references = list(pool.map(save, range(24)))
    store = EncryptedFileSecretStore(tmp_path / "credentials", tmp_path / "keys/master.key")
    assert [store.get("org-a", reference) for reference in references] == [f"secret-{i}" for i in range(24)]
