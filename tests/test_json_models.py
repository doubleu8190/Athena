"""Regression tests for typed JSON persistence boundaries."""

from athena.infrastructure.sqlite.repositories import _json_dumps, _json_loads_model
from athena.models import (
    FileLocator,
    CommandPayload,
    JsonSchema,
    ToolCall,
)


def test_json_models_preserve_known_and_extension_fields() -> None:
    locator = FileLocator.model_validate(
        {"path": "report.pdf", "page": 3, "custom": "adapter-value"}
    )
    assert locator.path == "report.pdf"
    assert locator.page == 3
    assert locator.model_dump()["custom"] == "adapter-value"

    payload = CommandPayload.model_validate(
        {"message": "analyze", "attachment_ids": ["file-1"]}
    )
    assert payload.message == "analyze"
    assert payload.attachment_ids == ["file-1"]


def test_json_round_trip_uses_models_without_changing_storage_shape() -> None:
    call = ToolCall(
        id="call-1", name="read_file", args={"path": "README.md"}
    )
    raw = _json_dumps(call)
    restored = _json_loads_model(raw, ToolCall, ToolCall())

    assert raw == '{"id": "call-1", "name": "read_file", "args": {"path": "README.md"}}'
    assert isinstance(restored, ToolCall)
    assert restored.args["path"] == "README.md"


def test_json_schema_is_nested_and_attribute_accessible() -> None:
    schema = JsonSchema.model_validate(
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    )
    assert schema.properties["path"].type == "string"
    schema_with_alias = JsonSchema.model_validate(
        {"additionalProperties": {"type": "string"}}
    )
    assert (
        schema_with_alias.model_dump(mode="json", exclude_none=True)[
            "additionalProperties"
        ]["type"]
        == "string"
    )
