from trackvance.api import app


def test_connection_openapi_has_public_response_contract_and_permissions():
    schema = app.openapi()
    paths = schema["paths"]
    listing = paths["/api/v1/connections"]["get"]
    assert listing["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ConnectionListResponse",
    }
    properties = schema["components"]["schemas"]["ConnectionResponse"]["properties"]
    assert {"connection_version_id", "config_hash", "last_test_status"} <= properties.keys()
    assert not {"password", "secret_reference"} & properties.keys()
    assert listing["x-required-permission"] == "connections:read"
    assert paths["/api/v1/connections/test"]["post"]["x-required-permission"] == "connections:manage"
    preview = paths["/api/v1/connections/{connection_id}/preview"]["get"]
    assert preview["x-required-permission"] == "connections:use"
    assert preview["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SourcePreviewResponse",
    }


def test_readiness_alias_is_public_in_openapi():
    schema = app.openapi()
    for path in ("/health/ready", "/api/v1/health/ready", "/api/v1/health"):
        assert "security" not in schema["paths"][path]["get"]
