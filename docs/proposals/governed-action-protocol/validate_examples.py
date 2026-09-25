#!/usr/bin/env python3
"""Validate draft fixtures, not protocol security or production behavior."""
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_pairs)


def main():
    schema = read(ROOT / "schemas/gap-v0.1.schema.json")
    Draft202012Validator.check_schema(schema)

    def validator(kind):
        return Draft202012Validator(
            {"$schema": schema["$schema"], "$defs": schema["$defs"],
             "$ref": f"#/$defs/{kind}"}, format_checker=FormatChecker()
        )

    fixture_types = {
        "action": "action",
        "permit-payload": "permit",
        "review-decision": "decision",
        "unknown-receipt-payload": "receipt",
    }
    fixtures = {}
    for filename, kind in fixture_types.items():
        value = read(ROOT / f"examples/{filename}.json")
        validator(kind).validate(value)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
        fixtures[filename] = value

    # This fixture has ASCII strings and safe integers only. json.dumps below
    # is intentionally NOT offered as a general-purpose JCS implementation.
    action = fixtures["action"]

    def fixture_subset(value):
        if isinstance(value, dict):
            return all(k.isascii() and fixture_subset(v) for k, v in value.items())
        if isinstance(value, str):
            return value.isascii()
        return type(value) is int and abs(value) <= 9007199254740991

    assert fixture_subset(action), "Use a real JCS library for expanded fixtures"
    canonical = json.dumps(action, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert canonical == (ROOT / "examples/action.canonical.json").read_bytes()
    digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
    for name in ("permit-payload", "review-decision", "unknown-receipt-payload"):
        assert fixtures[name]["action_digest"] == digest
        assert fixtures[name]["operation_id"] == action["operation_id"]
    permit = fixtures["permit-payload"]
    assert permit["iat"] == permit["nbf"]
    assert 1 <= permit["exp"] - permit["iat"] <= 300
    assert permit["context_digest"] == action["context_digest"]
    assert permit["aud"] == action["audience"]

    negatives = []

    def mutation(name, kind, source, edit):
        value = copy.deepcopy(source)
        edit(value)
        negatives.append((name, kind, value))

    mutation("unexpected action field", "action", action, lambda x: x.update(approved=True))
    mutation("fractional money", "action", action, lambda x: x["parameters"].update(amount_minor=40.5))
    mutation("negative amount", "action", action, lambda x: x["parameters"].update(amount_minor=-1))
    mutation("unsafe integer", "action", action, lambda x: x["parameters"].update(amount_minor=2**53))
    mutation("wrong currency", "action", action, lambda x: x["parameters"].update(currency="EUR"))
    mutation("new destination", "action", action, lambda x: x["parameters"].update(destination="elsewhere"))
    mutation("unsupported profile", "action", action, lambda x: x.update(profile="gap/9"))
    mutation("missing tenant", "action", action, lambda x: x.pop("tenant_id"))
    mutation("delegation enabled", "permit", permit, lambda x: x.update(delegation=True))
    mutation("multiple uses", "permit", permit, lambda x: x.update(max_uses=2))
    mutation("invalid digest", "permit", permit, lambda x: x.update(action_digest="sha256:bad"))
    mutation("unsigned PERMIT", "decision", fixtures["review-decision"], lambda x: x.update(outcome="PERMIT"))
    mutation("REVIEW with permit", "decision", fixtures["review-decision"], lambda x: x.update(permit="a.b.c"))
    mutation("success without result", "receipt", fixtures["unknown-receipt-payload"], lambda x: x.update(state="SUCCEEDED"))
    mutation("unknown claims result", "receipt", fixtures["unknown-receipt-payload"], lambda x: x.update(result_digest=digest))
    for name, kind, value in negatives:
        assert not validator(kind).is_valid(value), f"Schema accepted: {name}"
    try:
        json.loads('{"tenant_id":"a","tenant_id":"b"}', object_pairs_hook=unique_pairs)
    except ValueError:
        pass
    else:
        raise AssertionError("Duplicate JSON key was accepted")
    print(f"PASS: {len(fixtures)} illustrative payloads; canonical action digest; linked fields;")
    print(f"      {len(negatives)} invalid schema cases and duplicate-key rejection.")
    print("NOT TESTED: signatures, mTLS, authorization, storage, concurrency, revocation, or execution.")


if __name__ == "__main__":
    main()
