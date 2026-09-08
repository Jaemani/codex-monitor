"""Strict, bounded JSON predicates for managed file monitors."""

from __future__ import annotations

import json
import math


MAX_POINTER_BYTES = 1024
MAX_POINTER_DEPTH = 32
MAX_EXPECTED_BYTES = 4 * 1024
OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
ORDER_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})


class PredicateError(ValueError):
    """A predicate is malformed or cannot be evaluated safely."""


def _json_number(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if _json_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def _strict_equal(left, right):
    left_type = _json_type(left)
    if left_type != _json_type(right):
        return False
    if left_type in {"null", "boolean", "number", "string"}:
        return left == right
    if left_type == "array":
        return len(left) == len(right) and all(_strict_equal(a, b) for a, b in zip(left, right))
    if left_type == "object":
        return (left.keys() == right.keys() and
                all(_strict_equal(left[key], right[key]) for key in left))
    return False


def _pointer_tokens(pointer):
    if not isinstance(pointer, str):
        raise PredicateError("JSON pointer must be a string")
    try:
        pointer_bytes = pointer.encode("utf-8")
    except UnicodeError as exc:
        raise PredicateError("JSON pointer must be valid UTF-8") from exc
    if len(pointer_bytes) > MAX_POINTER_BYTES:
        raise PredicateError("JSON pointer exceeds 1024 bytes")
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise PredicateError("JSON pointer must be empty or start with '/'")
    parts = pointer[1:].split("/")
    if len(parts) > MAX_POINTER_DEPTH:
        raise PredicateError("JSON pointer exceeds depth 32")
    tokens = []
    for part in parts:
        decoded = []
        index = 0
        while index < len(part):
            if part[index] != "~":
                decoded.append(part[index])
                index += 1
                continue
            if index + 1 >= len(part) or part[index + 1] not in "01":
                raise PredicateError("JSON pointer contains an invalid escape")
            decoded.append("~" if part[index + 1] == "0" else "/")
            index += 2
        tokens.append("".join(decoded))
    return tuple(tokens)


def _finite_json(value):
    def valid(item):
        item_type = _json_type(item)
        if item_type is None:
            return False
        if item_type == "array":
            return all(valid(child) for child in item)
        if item_type == "object":
            return all(isinstance(key, str) and valid(child) for key, child in item.items())
        return True

    try:
        valid_value = valid(value)
    except RecursionError as exc:
        raise PredicateError("predicate value must be finite JSON") from exc
    if not valid_value:
        raise PredicateError("predicate value must be finite JSON")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise PredicateError("predicate value must be finite JSON") from exc
    return encoded


def normalize_condition(condition):
    """Validate a condition and return ``(normalized, canonical_json)``."""
    if not isinstance(condition, dict):
        raise PredicateError("condition must be an object")
    if set(condition) != {"pointer", "operator", "value"}:
        raise PredicateError("condition requires pointer, operator and value")
    pointer = condition["pointer"]
    _pointer_tokens(pointer)
    operator = condition["operator"]
    if not isinstance(operator, str) or operator not in OPERATORS:
        raise PredicateError("condition operator must be eq, ne, gt, gte, lt or lte")
    value = condition["value"]
    if _json_type(value) is None:
        raise PredicateError("predicate value must be finite JSON")
    encoded_value = _finite_json(value)
    try:
        encoded_bytes = encoded_value.encode("utf-8")
    except UnicodeError as exc:
        raise PredicateError("predicate value must be finite JSON") from exc
    if len(encoded_bytes) > MAX_EXPECTED_BYTES:
        raise PredicateError("predicate value exceeds 4096 bytes")
    if operator in ORDER_OPERATORS and not _json_number(value):
        raise PredicateError("ordering predicates require a finite number")
    normalized = {"pointer": pointer, "operator": operator, "value": value}
    return normalized, json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False)


def load_condition(encoded):
    try:
        condition = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PredicateError("stored condition is invalid") from exc
    normalized, _ = normalize_condition(condition)
    return normalized


def public_condition(condition):
    if condition is None:
        return None
    if isinstance(condition, str):
        condition = load_condition(condition)
    normalized, _ = normalize_condition(condition)
    return {"pointer": normalized["pointer"], "operator": normalized["operator"]}


def parse_document(raw):
    def reject_constant(value):
        raise ValueError("non-finite JSON constant")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    try:
        return json.loads(
            raw.decode("utf-8"), parse_constant=reject_constant, parse_float=finite_float,
        )
    except (TypeError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise PredicateError("invalid JSON document") from exc


def _resolve(document, tokens):
    value = document
    for token in tokens:
        if isinstance(value, dict):
            if token not in value:
                return None, False
            value = value[token]
        elif isinstance(value, list):
            if (token == "-" or not token or
                    any(char < "0" or char > "9" for char in token) or
                    (token != "0" and token.startswith("0"))):
                return None, False
            index = int(token)
            if index >= len(value):
                return None, False
            value = value[index]
        else:
            return None, False
    return value, True


def evaluate(document, condition):
    """Return only a redacted predicate state, never the selected value."""
    normalized, _ = normalize_condition(condition)
    value, found = _resolve(document, _pointer_tokens(normalized["pointer"]))
    if not found:
        return {"state": "invalid", "error": "missing_pointer"}
    operator = normalized["operator"]
    expected = normalized["value"]
    if operator in ORDER_OPERATORS and not _json_number(value):
        return {"state": "invalid", "error": "ordering_requires_number"}
    if operator == "eq":
        matched = _strict_equal(value, expected)
    elif operator == "ne":
        matched = not _strict_equal(value, expected)
    elif operator == "gt":
        matched = value > expected
    elif operator == "gte":
        matched = value >= expected
    elif operator == "lt":
        matched = value < expected
    else:
        matched = value <= expected
    return {"state": "matched" if matched else "not_matched"}
