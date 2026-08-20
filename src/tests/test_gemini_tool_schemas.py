"""Regression tests for the tool schemas handed to Gemini.

Gemini validates function-declaration parameters against its own OpenAPI
subset, which is narrower than both plain JSON Schema and the google-genai
Schema model (that model also covers Vertex AI, so Vertex-only keys pass
client-side validation and are then rejected by the REST endpoint). Three
separate 400s came out of that gap:

    oneOf                -> rejected by the SDK's FunctionDeclaration
    additionalProperties -> "Unknown name ... Cannot find field"
    {"type": "array"}    -> "items: missing field"

model_router._sanitize_gemini_schema translates or drops whatever Gemini will
not take, and these tests pin every rule the API enforced on us.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import AI
import model_router

# The Gemini API's Schema type, per its v1beta discovery document. Anything
# outside this set is refused by the endpoint even when the SDK accepts it.
GEMINI_SCHEMA_FIELDS = {
    "anyOf", "default", "description", "enum", "example", "format", "items",
    "maxItems", "maxLength", "maxProperties", "maximum", "minItems",
    "minLength", "minProperties", "minimum", "nullable", "pattern",
    "properties", "propertyOrdering", "required", "title", "type",
}
GEMINI_TYPES = {
    "TYPE_UNSPECIFIED", "STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY",
    "OBJECT", "NULL",
}


def _violations(schema: dict, path: str = "root") -> list[str]:
    """Every rule the API rejected a payload over, applied recursively."""
    found = []
    for key in schema:
        if key not in GEMINI_SCHEMA_FIELDS:
            found.append(f"{path}.{key}: field does not exist on Gemini's Schema")

    declared = str(schema.get("type", "")).upper()
    if "type" in schema and declared not in GEMINI_TYPES:
        found.append(f"{path}.type={schema['type']!r}: not a Gemini type")
    if "type" in schema and "anyOf" in schema:
        found.append(f"{path}: type and anyOf cannot be set together")
    if declared == "ARRAY" and not isinstance(schema.get("items"), dict):
        found.append(f"{path}: ARRAY without items")
    if "enum" in schema and declared != "STRING":
        found.append(f"{path}: enum needs a STRING type")

    for name, sub in (schema.get("properties") or {}).items():
        found += _violations(sub, f"{path}.{name}")
    if isinstance(schema.get("items"), dict):
        found += _violations(schema["items"], f"{path}[]")
    for i, sub in enumerate(schema.get("anyOf") or []):
        found += _violations(sub, f"{path}|{i}")
    return found


def _clean(properties: dict) -> dict:
    return model_router._sanitize_gemini_schema(
        {"type": "object", "properties": properties}
    )["properties"]["v"]


def test_every_shipped_tool_schema_is_accepted():
    for tool in AI._get_all_tools(include_subagent=True, include_telegram=True):
        fn = tool["function"]
        cleaned = model_router._sanitize_gemini_schema(fn.get("parameters") or {})
        assert _violations(cleaned, fn["name"]) == []


def test_source_schemas_declare_array_contents():
    """Gemini needs items on every array, so the schemas should say so
    themselves rather than leaning on the sanitizer's fallback."""
    for tool in AI._get_all_tools(include_subagent=True, include_telegram=True):
        fn = tool["function"]
        stack = [fn.get("parameters") or {}]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            if str(node.get("type", "")).lower() == "array":
                assert isinstance(node.get("items"), dict), f"{fn['name']}: array without items"
            stack.extend((node.get("properties") or {}).values())
            stack.append(node.get("items"))
            stack.extend(node.get("anyOf") or [])


def test_one_of_becomes_any_of():
    out = _clean({"v": {"oneOf": [{"type": "number"}, {"type": "string"}]}})
    assert out == {"anyOf": [{"type": "number"}, {"type": "string"}]}


def test_vertex_only_keys_are_dropped():
    out = _clean({"v": {"type": "object", "additionalProperties": {"type": "string"}}})
    assert out == {"type": "object"}


def test_bare_array_gets_items():
    assert _violations(_clean({"v": {"type": "array"}}), "v") == []
    assert isinstance(_clean({"v": {"type": "array"}})["items"], dict)


def test_bare_array_gets_items_at_every_depth():
    nested = _clean({"v": {"type": "array", "items": {"type": "array"}}})
    assert isinstance(nested["items"]["items"], dict)
    inside_object = _clean({"v": {"type": "object", "properties": {"deep": {"type": "array"}}}})
    assert isinstance(inside_object["properties"]["deep"]["items"], dict)
    inside_union = _clean({"v": {"anyOf": [{"type": "string"}, {"type": "array"}]}})
    assert isinstance(inside_union["anyOf"][1]["items"], dict)


def test_type_lists_collapse_to_a_type_plus_nullable():
    assert _clean({"v": {"type": ["string", "null"]}}) == {"type": "string", "nullable": True}
    assert _clean({"v": {"type": ["string", "number"]}}) == {
        "anyOf": [{"type": "string"}, {"type": "number"}]
    }


def test_enum_gains_a_string_type():
    assert _clean({"v": {"enum": ["a", "b"]}}) == {"enum": ["a", "b"], "type": "string"}


def test_all_of_is_flattened():
    assert _clean({"v": {"allOf": [{"type": "string"}, {"description": "d"}]}}) == {
        "type": "string",
        "description": "d",
    }


def test_unknown_keywords_are_stripped():
    assert _clean({"v": {"type": "string", "readOnly": True, "$comment": "x"}}) == {"type": "string"}


def test_declarations_build_and_no_arg_tools_omit_parameters():
    from google.genai import types

    no_arg = []
    for tool in AI._get_all_tools(include_subagent=True, include_telegram=True):
        fn = tool["function"]
        params = model_router._sanitize_gemini_schema(fn.get("parameters") or {})
        if not params.get("properties"):
            params = None
            no_arg.append(fn["name"])
        types.FunctionDeclaration(
            name=fn["name"], description=fn.get("description", ""), parameters=params
        )
    # Gemini rejects an OBJECT with an empty properties map, so a no-argument
    # tool has to send no parameters at all.
    assert "list_projects" in no_arg
