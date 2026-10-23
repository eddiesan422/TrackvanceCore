"""Optional deployment endpoint leaves the seven-volume/one-network contract intact."""
import json

import codex_environment as codex
import pytest


def test_idp_overlay_contains_only_fixed_endpoint_on_api_and_document_lane():
    overlay = codex.idp_overlay(codex.IDP_ORIGIN)
    assert set(overlay) == {"services"} and set(overlay["services"]) == {"api", "document-worker"}
    assert all(value == {"environment": {"TRACKVANCE_IDP_LOCAL_URL": codex.IDP_ORIGIN}}
        for value in overlay["services"].values())
    for origin in ("https://aws.example.com", "http://postgres:5432", "http://host.docker.internal:9999", "http://idp-gateway:8088"):
        with pytest.raises(ValueError, match="RESERVED_LOCAL_ORIGIN"):
            codex.idp_overlay(origin)


def test_default_compose_omits_endpoint_overlay_and_optional_overlay_is_exact(tmp_path):
    base, _environment = codex.compose(tmp_path, {})
    assert "idp-local.compose.json" not in " ".join(base)
    values = {"CODEX_IDP_LOCAL_URL": codex.IDP_ORIGIN}
    with pytest.raises(ValueError, match="OVERLAY_REQUIRED"):
        codex.compose(tmp_path, values)
    path = tmp_path / "idp-local.compose.json"
    path.write_text(json.dumps(codex.idp_overlay(codex.IDP_ORIGIN)))
    enabled, environment = codex.compose(tmp_path, values)
    assert enabled[-2:] == ["-f", str(path)]
    assert environment["CODEX_IDP_LOCAL_URL"] == codex.IDP_ORIGIN
    mutated = codex.idp_overlay(codex.IDP_ORIGIN)
    mutated["services"]["worker"] = mutated["services"]["api"]
    path.write_text(json.dumps(mutated))
    with pytest.raises(ValueError, match="OVERLAY_DRIFT"):
        codex.compose(tmp_path, values)


def test_endpoint_pins_are_private_hashed_and_disabled_default_does_not_add_configuration(tmp_path):
    default = tmp_path / "default"
    default.mkdir()
    value = {}
    codex.update_pins(default, value, {})
    assert not (default / "idp-local.compose.json").exists()
    enabled = tmp_path / "enabled"
    enabled.mkdir()
    codex.update_pins(enabled, value, {"CODEX_IDP_LOCAL_URL": codex.IDP_ORIGIN})
    assert json.loads((enabled / "idp-local.compose.json").read_text()) == codex.idp_overlay(codex.IDP_ORIGIN)
    assert value["environment_file"] == codex.reference(enabled / "codex.env")
    assert "CODEX_IDP_LOCAL_URL=" + codex.IDP_ORIGIN in (enabled / "codex.env").read_text()
