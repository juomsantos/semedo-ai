"""
Unit tests for orchestration/parsing.py — the orchestrator's LLM-response parsers.

These are pure functions but sit on the most failure-prone surface in the
system (model-emitted JSON), so they get a thorough table of valid, invalid,
and edge-case inputs:

  * ``parse_routing_decision``    — decomposition output (array + wrapper forms)
  * ``parse_validation_decision`` — validation output (object form)
  * ``_sanitize_json_literals``   — pre-parse newline/tab escaping

No fixtures needed — nothing here touches the filesystem.
"""

import json

import pytest

from orchestration.parsing import (
    parse_routing_decision,
    parse_validation_decision,
    _sanitize_json_literals,
)


def _subtask(worker="coder", type_="code", desc="do a thing", out="a result"):
    return {
        "worker": worker,
        "type": type_,
        "description": desc,
        "expected_output": out,
    }


# ---------------------------------------------------------------------------
# parse_routing_decision — happy paths
# ---------------------------------------------------------------------------

def test_routing_plain_array_single_subtask():
    response = json.dumps([_subtask()])
    subtasks, redecompose = parse_routing_decision(response)
    assert redecompose is False
    assert len(subtasks) == 1
    assert subtasks[0]["worker"] == "coder"


def test_routing_plain_array_multiple_subtasks():
    response = json.dumps([_subtask(worker="research", type_="research"), _subtask()])
    subtasks, redecompose = parse_routing_decision(response)
    assert redecompose is False
    assert [s["worker"] for s in subtasks] == ["research", "coder"]


def test_routing_extracts_from_json_fence():
    response = "Sure, here is the plan:\n```json\n" + json.dumps([_subtask()]) + "\n```\nDone."
    subtasks, redecompose = parse_routing_decision(response)
    assert len(subtasks) == 1
    assert redecompose is False


def test_routing_extracts_from_unlabelled_fence():
    response = "```\n" + json.dumps([_subtask(worker="claude-code", type_="reasoning")]) + "\n```"
    subtasks, _ = parse_routing_decision(response)
    assert subtasks[0]["worker"] == "claude-code"


def test_routing_bare_single_object_is_wrapped():
    """A lone subtask object (not in an array) is recovered into a 1-element list."""
    response = json.dumps(_subtask())
    subtasks, redecompose = parse_routing_decision(response)
    assert isinstance(subtasks, list)
    assert len(subtasks) == 1
    assert redecompose is False


def test_routing_all_valid_workers_accepted():
    workers = ["coder", "research", "claude-code", "pending_approval"]
    response = json.dumps([_subtask(worker=w) for w in workers])
    subtasks, _ = parse_routing_decision(response)
    assert [s["worker"] for s in subtasks] == workers


# ---------------------------------------------------------------------------
# parse_routing_decision — redecompose_after_research wrapper
# ---------------------------------------------------------------------------

def test_routing_redecompose_wrapper_all_research():
    response = json.dumps({
        "redecompose_after_research": True,
        "subtasks": [_subtask(worker="research", type_="research")],
    })
    subtasks, redecompose = parse_routing_decision(response)
    assert redecompose is True
    assert len(subtasks) == 1
    assert subtasks[0]["worker"] == "research"


def test_routing_redecompose_flag_cleared_when_non_research_present():
    """The flag is only valid when every subtask targets research."""
    response = json.dumps({
        "redecompose_after_research": True,
        "subtasks": [_subtask(worker="research", type_="research"), _subtask()],
    })
    subtasks, redecompose = parse_routing_decision(response)
    assert redecompose is False  # cleared because a coder subtask slipped in
    assert len(subtasks) == 2


def test_routing_wrapper_without_flag_uses_subtasks_key():
    response = json.dumps({"subtasks": [_subtask()]})
    subtasks, redecompose = parse_routing_decision(response)
    assert redecompose is False
    assert len(subtasks) == 1


def test_routing_tolerates_literal_newline_in_description():
    """A raw newline inside a description value would break json.loads; the
    routing parser sanitizes it first (parity with parse_validation_decision)."""
    # Real newline char inside the description — invalid JSON as-is.
    raw = (
        '[{"worker": "coder", "type": "code", '
        '"description": "step one\nstep two", "expected_output": "code"}]'
    )
    subtasks, redecompose = parse_routing_decision(raw)
    assert len(subtasks) == 1
    assert "step one" in subtasks[0]["description"]
    assert "step two" in subtasks[0]["description"]


# ---------------------------------------------------------------------------
# parse_routing_decision — error cases
# ---------------------------------------------------------------------------

def test_routing_invalid_json_raises():
    with pytest.raises(ValueError, match="Failed to parse JSON"):
        parse_routing_decision("not json at all {[")


def test_routing_empty_array_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_routing_decision("[]")


def test_routing_dict_without_subtasks_is_empty_and_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_routing_decision(json.dumps({"foo": "bar"}))


def test_routing_subtask_not_a_dict_raises():
    with pytest.raises(ValueError, match="not a dict"):
        parse_routing_decision(json.dumps(["just a string"]))


def test_routing_subtask_missing_fields_raises():
    bad = {"worker": "coder", "type": "code"}  # missing description, expected_output
    with pytest.raises(ValueError, match="missing required fields"):
        parse_routing_decision(json.dumps([bad]))


def test_routing_invalid_worker_raises():
    with pytest.raises(ValueError, match="invalid worker"):
        parse_routing_decision(json.dumps([_subtask(worker="wizard")]))


# ---------------------------------------------------------------------------
# parse_validation_decision — happy paths
# ---------------------------------------------------------------------------

def test_validation_complete_decision():
    response = json.dumps({"decision": "complete", "reasoning": "all good"})
    data = parse_validation_decision(response)
    assert data["decision"] == "complete"
    assert data["reasoning"] == "all good"


@pytest.mark.parametrize("decision", ["complete", "refine", "additional_work", "redo"])
def test_validation_all_valid_decisions(decision):
    response = json.dumps({"decision": decision, "reasoning": "x", "follow_ups": []})
    assert parse_validation_decision(response)["decision"] == decision


def test_validation_extracts_from_fence():
    response = "Verdict:\n```json\n" + json.dumps({"decision": "redo", "reasoning": "fix it"}) + "\n```"
    assert parse_validation_decision(response)["decision"] == "redo"


def test_validation_preserves_follow_ups():
    response = json.dumps({
        "decision": "refine",
        "reasoning": "needs polish",
        "follow_ups": [_subtask()],
    })
    data = parse_validation_decision(response)
    assert len(data["follow_ups"]) == 1


# ---------------------------------------------------------------------------
# parse_validation_decision — the literal-newline sanitizer path
# ---------------------------------------------------------------------------

def test_validation_tolerates_literal_newline_in_string():
    """A raw (unescaped) newline inside a value would normally break json.loads;
    parse_validation_decision sanitizes it first."""
    # Real newline char inside the reasoning value — invalid JSON as-is.
    raw = '{"decision": "complete", "reasoning": "line one\nline two"}'
    data = parse_validation_decision(raw)
    assert data["decision"] == "complete"
    assert "line one" in data["reasoning"] and "line two" in data["reasoning"]


def test_validation_tolerates_literal_tab_in_string():
    raw = '{"decision": "redo", "reasoning": "a\tb"}'
    data = parse_validation_decision(raw)
    assert data["decision"] == "redo"


# ---------------------------------------------------------------------------
# parse_validation_decision — error cases
# ---------------------------------------------------------------------------

def test_validation_invalid_decision_value_raises():
    with pytest.raises(ValueError, match="Invalid decision"):
        parse_validation_decision(json.dumps({"decision": "ship_it", "reasoning": "x"}))


def test_validation_missing_decision_raises():
    with pytest.raises(ValueError, match="Invalid decision"):
        parse_validation_decision(json.dumps({"reasoning": "no decision key"}))


def test_validation_non_object_raises():
    with pytest.raises(ValueError, match="Expected JSON object"):
        parse_validation_decision(json.dumps(["complete"]))


def test_validation_unparseable_json_raises():
    with pytest.raises(ValueError, match="Failed to parse validation JSON"):
        parse_validation_decision("{decision: complete")


# ---------------------------------------------------------------------------
# _sanitize_json_literals
# ---------------------------------------------------------------------------

def test_sanitize_escapes_literal_newline():
    raw = '{"k": "a\nb"}'
    fixed = _sanitize_json_literals(raw)
    # Result must be valid JSON now, and the value round-trips with a real newline.
    assert json.loads(fixed)["k"] == "a\nb"


def test_sanitize_does_not_double_escape_existing_escapes():
    """An already-escaped \\n (backslash + n) must stay a single escape."""
    raw = r'{"k": "a\nb"}'  # literal backslash-n in the source text
    fixed = _sanitize_json_literals(raw)
    assert fixed == raw  # nothing to change — no real control chars present
    assert json.loads(fixed)["k"] == "a\nb"


def test_sanitize_handles_tabs_and_carriage_returns():
    raw = '{"k": "a\tb\rc"}'
    fixed = _sanitize_json_literals(raw)
    loaded = json.loads(fixed)["k"]
    assert "\t" in loaded and "\r" in loaded


def test_sanitize_leaves_clean_json_unchanged():
    raw = '{"decision": "complete", "reasoning": "nothing special"}'
    assert _sanitize_json_literals(raw) == raw
