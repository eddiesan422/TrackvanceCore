"""Literal witness roles change opt-in input, preserving free observations and v2."""
import base64
import copy
import hashlib
import json

import test_document_recognition_stream_098 as stream_tests
from test_document_source_association_098 import config, mapped, observation, page
from test_document_visual_request_098 import gateway

from trackvance.document_normalization import canonical_hash
from trackvance.document_schemas import DocumentDefinition

service = stream_tests.service
LEGACY_WIRE_SHA256 = "d936f6bafb550a661db4bcf05c7499297166300fa7f1025cc2fc92ae1ce1fc38"
LEGACY_PROMPT_SHA256 = "1a87522de46279f74ff68e2214bb212e30756e7ff17992e1a3cba45db232565b"


def test_opt_in_schema_describes_three_roles_but_keeps_literal_witnesses_free():
    api = gateway()
    schema = api.visual_contract(config())
    cell = schema["properties"]["fields"]["properties"]["contract"]["properties"]
    assert "VALUE" in cell["observed"]["description"]
    context = cell["source_context"]["properties"]
    assert "CAPTION" in context["anchor_label"]["description"]
    assert "SECTION HEADING" in context["section_label"]["description"]
    assert "nonempty section_label" in context["section_label"]["description"]
    assert "Otherwise null" in context["section_label"]["description"]
    assert all(context[name]["type"] == ["string", "null"] and context[name]["maxLength"] == 160
        for name in ("anchor_label", "section_label"))
    def no_forced_observation(value):
        if isinstance(value, dict):
            assert not {"const", "enum"} & value.keys()
            for item in value.values():
                no_forced_observation(item)
        elif isinstance(value, list):
            for item in value:
                no_forced_observation(item)
    no_forced_observation(schema)


def test_inverted_value_caption_roles_are_retained_and_rejected_without_repair():
    definition = config()
    source = page(1, observation("SYNTHETIC-17", "SYNTHETIC-17", "Service agreement"))
    original = copy.deepcopy(source)
    # A wrong literal witness remains expressible; association checks decide it.
    gateway().validate_selection(source["selection"], definition)
    result = mapped(definition, source)
    cell = result["fields"]["contract"]
    assert cell["value"] is None and cell["validity"] == "MISSING"
    rejected = cell["candidate_alternatives"][0]
    assert rejected["observed"] == "SYNTHETIC-17" and rejected["eligible_for_field"] is False
    assert rejected["association"]["reason"] == "OBSERVED_ANCHOR_NOT_DECLARED_FOR_FIELD"
    assert rejected["association"]["source_context"] == original["selection"]["fields"]["contract"]["source_context"]
    assert result["document_representation"]["pages"][0]["selection"] == original["selection"]
    assert source == original


def test_literal_caption_and_value_equality_does_not_add_an_inequality_rule():
    source = page(1, observation("Service agreement", "Service agreement"))
    definition = config()
    gateway().validate_selection(source["selection"], definition)
    cell = mapped(definition, source)["fields"]["contract"]
    assert cell["observed"] == cell["value"] == "Service agreement"
    assert cell["validity"] == "VALID"


def test_legacy_effective_wire_and_prompt_remain_byte_identical_to_v2(service, monkeypatch):
    # Frozen from the same synthetic wire fixture before the v4 input change.
    # It includes the actual prompt, schema, semantics, image and generation options.
    runtime, source = service
    calls = stream_tests.install_visual(runtime, monkeypatch)
    result = stream_tests.invoke(source, "QWEN_VISUAL", lambda _: None)
    assert len(calls) == 1 and result["terminal"]["status"] == "COMPLETE"
    assert canonical_hash(calls[0]) == LEGACY_WIRE_SHA256
    assert hashlib.sha256(calls[0]["messages"][0]["content"].encode()).hexdigest() == LEGACY_PROMPT_SHA256
    engine = result["pages"][0]["payload"]["engine"]
    assert engine["prompt_version"] == "trackvance-idp-098-visual-v2"
    assert engine["semantic_definition_version"] == "trackvance-visual-semantic-098-v2"


def test_v4_roles_reach_and_bind_the_effective_opt_in_request(service, monkeypatch):
    runtime, source = service
    definition = copy.deepcopy(stream_tests.lossless_definition())
    definition["fields"][0].update(source_association="DECLARED_LABEL_CONTEXT_V1",
        visual={"label": "Identifier", "page": 1, "region": [0, 0, 1, 1], "direction": "RIGHT"})
    definition = DocumentDefinition.model_validate(definition).model_dump(mode="json")
    monkeypatch.setattr(stream_tests, "DEFINITION", definition)
    calls = stream_tests.install_visual(runtime, monkeypatch)
    original_chat = runtime.stream_chat
    def witnessed_chat(identity, body, timeout, raw_capture=None):
        reply = original_chat(identity, body, timeout, raw_capture=None)
        selection = json.loads(reply["message"]["content"])
        selection["fields"]["identifier"]["source_context"] = {"anchor_label": "Identifier", "section_label": None}
        reply["message"]["content"] = json.dumps(selection)
        if raw_capture:
            raw_capture((json.dumps(reply) + "\n").encode())
        return reply
    monkeypatch.setattr(runtime, "stream_chat", witnessed_chat)
    saved = []
    result = stream_tests.invoke(source, "QWEN_VISUAL", saved.append)
    assert len(calls) == 1 and result["terminal"]["status"] == "COMPLETE"
    body = calls[0]
    prompt = body["messages"][0]["content"]
    assert "observed contains the requested printed VALUE" in prompt
    assert "source_context.section_label=null" in prompt
    assert "logical grouping, not a printed heading" in prompt
    content = json.loads(body["messages"][1]["content"])
    assert content["json_schema"] == body["format"]
    assert "description" in body["format"]["properties"]["fields"]["properties"]["identifier"]["properties"]["observed"]
    assert content["definition"]["semantic_definition_version"] == "trackvance-visual-semantic-098-v4-source-association-roles"
    engine = result["pages"][0]["payload"]["engine"]
    assert engine["prompt_version"] == "trackvance-idp-098-visual-v4-source-association-roles"
    assert engine["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert engine["request_sha256"] == canonical_hash(body)
    record = next(raw for event in saved for raw in event["payload"].get("artifacts", []) if raw["kind"] == "QWEN_REQUEST_JSON")
    metadata = json.loads(base64.b64decode(record["content_b64"]))
    assert metadata["request_sha256"] == canonical_hash(body)
    assert metadata["request_without_image_bytes"]["messages"][0]["content"] == prompt
    assert metadata["request_without_image_bytes"]["format"] == body["format"]
