"""tool_schemas.json loader and a stdlib validator for the JSON Schema subset of CONTRACT.md section 3.1.

Input schemas use type, properties, required, additionalProperties, enum, const, pattern, minLength, maxLength,
minimum, maximum, exclusiveMinimum, items, minItems, maxItems, uniqueItems and default. Output schemas also use
$ref, anyOf, minProperties and maxProperties, so the service can check its own outputs against the allow-list.
Problems name a JSON path and a problem code; they never echo the offending value.
"""
import copy
import json
import math
import os
import re

SCHEMAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_schemas.json")
ID_FIELDS = ("transaction_id", "product_id", "case_id", "challenge_id")
ID_LIST_FIELDS = ("candidate_transaction_ids",)
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    # NaN and infinities are not JSON numbers (Python's json accepts them): NaN would pass every bound check.
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
    "null": lambda v: v is None,
}
_PATTERNS = {}


def _regex(pattern):
    if pattern not in _PATTERNS:
        _PATTERNS[pattern] = re.compile(pattern)
    return _PATTERNS[pattern]


class ToolSchemas:
    def __init__(self, path=SCHEMAS_PATH):
        with open(path, encoding="utf-8") as fh:
            self.doc = json.load(fh)
        self.tools = {t["name"]: t for t in self.doc["tools"]}
        self.defs = self.doc.get("$defs", {})

    def tool(self, name):
        return self.tools.get(name) if isinstance(name, str) else None

    def model_tools(self, include_runtime=False, parameters_key="input_schema"):
        """Tool definitions to offer the model: {name, description, input_schema} (or 'parameters')."""
        out = []
        for t in self.doc["tools"]:
            if t["exposure"] == "model" or include_runtime:
                out.append({"name": t["name"], "description": t["description"],
                            parameters_key: copy.deepcopy(t["input_schema"])})
        return out

    def validate_input(self, name, args):
        return validate(self.tools[name]["input_schema"], args, defs=self.defs)

    def validate_output(self, name, data):
        return validate(self.tools[name]["output_schema"], data, defs=self.defs)


def validate(schema, value, path="$", defs=None, problems=None):
    """Return a list of {path, problem}; empty when valid."""
    problems = [] if problems is None else problems
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return validate((defs or {})[name], value, path, defs, problems)
    if "anyOf" in schema:
        if not any(not validate(s, value, path, defs, []) for s in schema["anyOf"]):
            problems.append({"path": path, "problem": "any_of"})
        return problems
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_TYPES[k](value) for k in kinds):
            problems.append({"path": path, "problem": "type"})
            return problems
    if "const" in schema and value != schema["const"]:
        problems.append({"path": path, "problem": "const"})
    if "enum" in schema and value not in schema["enum"]:
        problems.append({"path": path, "problem": "enum"})
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append({"path": path, "problem": "min_length"})
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append({"path": path, "problem": "max_length"})
        if "pattern" in schema and not _regex(schema["pattern"]).search(value):
            problems.append({"path": path, "problem": "pattern"})
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append({"path": path, "problem": "minimum"})
        if "maximum" in schema and value > schema["maximum"]:
            problems.append({"path": path, "problem": "maximum"})
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            problems.append({"path": path, "problem": "exclusive_minimum"})
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append({"path": path, "problem": "min_items"})
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append({"path": path, "problem": "max_items"})
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(set(seen)) != len(seen):
                problems.append({"path": path, "problem": "unique_items"})
        if "items" in schema:
            for i, item in enumerate(value):
                validate(schema["items"], item, f"{path}[{i}]", defs, problems)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                problems.append({"path": f"{path}.{key}", "problem": "required"})
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            problems.append({"path": path, "problem": "min_properties"})
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            problems.append({"path": path, "problem": "max_properties"})
        for key, item in value.items():
            if key in props:
                validate(props[key], item, f"{path}.{key}", defs, problems)
            elif schema.get("additionalProperties") is False:
                # The key itself may carry injected text: report a fixed path.
                problems.append({"path": f"{path}.<unknown>", "problem": "unknown_field"})
    return problems


def normalize(schema, args):
    """Section 3.1 normalization: trim strings, upper-case id fields, drop null optional fields, turn integral
    floats into integers where the schema wants an integer, and apply top-level defaults."""
    if not isinstance(args, dict):
        return args
    out = _normalize_obj(schema, args)
    for key, prop in schema.get("properties", {}).items():
        if key not in out and "default" in prop:
            out[key] = copy.deepcopy(prop["default"])
    return out


def _normalize_obj(schema, obj):
    props = schema.get("properties", {})
    out = {}
    for key, value in obj.items():
        if value is None and key in props and key not in schema.get("required", []):
            continue
        out[key] = _normalize_value(props.get(key, {}), value, key)
    return out


def _normalize_value(schema, value, key):
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if isinstance(value, str):
        value = value.strip()
        if key in ID_FIELDS:
            value = value.upper()
        return value
    if isinstance(value, float) and "integer" in kinds and value.is_integer():
        return int(value)
    if isinstance(value, dict) and "object" in kinds:
        return _normalize_obj(schema, value)
    if isinstance(value, list):
        items = schema.get("items", {})
        return [(_normalize_value(items, v, key).upper() if key in ID_LIST_FIELDS and isinstance(v, str)
                 else _normalize_value(items, v, key)) for v in value]
    return value


def truncate_to_limits(schema, obj, prefix):
    """Cut over-long strings and over-long lists of `obj` (in place) to the schema limits.

    Used only for the handoff package (section 3.15): texts above the limits are cut, never stored whole and never
    a reason to refuse the safe fallback. Returns the JSON paths that were cut."""
    cut = []
    if not isinstance(obj, dict):
        return cut
    for key, prop in schema.get("properties", {}).items():
        value = obj.get(key)
        path = f"{prefix}.{key}"
        if isinstance(value, str) and "maxLength" in prop and len(value) > prop["maxLength"]:
            obj[key] = value[:prop["maxLength"]]
            cut.append(path)
        elif isinstance(value, list):
            if "maxItems" in prop and len(value) > prop["maxItems"]:
                obj[key] = value = value[:prop["maxItems"]]
                cut.append(path)
            limit = prop.get("items", {}).get("maxLength")
            if limit:
                for i, item in enumerate(value):
                    if isinstance(item, str) and len(item) > limit:
                        value[i] = item[:limit]
                        cut.append(f"{path}[{i}]")
    return cut
