"""
JSON parsers for the orchestrator's LLM responses.

- ``parse_routing_decision`` — decomposition output (subtask list, plus the
  ``redecompose_after_research`` wrapper format).
- ``parse_validation_decision`` — validation output (decision + reasoning +
  optional follow-ups).
- ``_sanitize_json_literals`` — pre-parse cleanup that escapes literal
  newlines/tabs inside JSON string values without double-escaping.
"""

import json
import logging
import re

_module_log = logging.getLogger(__name__)


def parse_routing_decision(response: str) -> tuple[list[dict], bool]:
    """
    Parse the LLM's routing decision from its response.

    Accepts two formats:

    Plain array (standard decomposition):
    [
      {"worker": "coder", "type": "code", "description": "...", "expected_output": "..."},
      ...
    ]

    Wrapper object (research-first, re-decompose after results):
    {
      "redecompose_after_research": true,
      "subtasks": [
        {"worker": "research", "type": "research", "description": "...", "expected_output": "..."}
      ]
    }

    Returns (subtasks, redecompose_after_research).
    When redecompose_after_research is True all subtasks must target the research worker;
    if non-research subtasks are found the flag is cleared and a warning is logged.
    """
    # Try to extract JSON from markdown code fences (```json ... ```)
    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response, re.DOTALL)
    json_str = json_match.group(1) if json_match else response.strip()

    # Escape literal newlines/tabs inside string values before parsing — the
    # decomposition LLM often embeds raw newlines in a description, which
    # json.loads rejects as control characters. Mirrors parse_validation_decision.
    json_str = _sanitize_json_literals(json_str)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from response: {e}")

    # Handle wrapper object format
    redecompose_flag = False
    if isinstance(data, dict):
        redecompose_flag = bool(data.get("redecompose_after_research", False))
        subtask_fields = {"worker", "type", "description", "expected_output"}
        if not redecompose_flag and subtask_fields.issubset(data.keys()):
            # LLM returned a bare single-subtask object instead of a one-element
            # array — recover gracefully by wrapping it.
            data = [data]
        else:
            data = data.get("subtasks", [])

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    if not data:
        raise ValueError("Routing decision array is empty")

    # Validate each subtask
    valid_workers = {"coder", "research", "claude-code", "pending_approval"}
    for i, subtask in enumerate(data):
        if not isinstance(subtask, dict):
            raise ValueError(f"Subtask {i} is not a dict: {type(subtask).__name__}")

        required_fields = {"worker", "type", "description", "expected_output"}
        missing = required_fields - set(subtask.keys())
        if missing:
            raise ValueError(f"Subtask {i} missing required fields: {missing}")

        if subtask["worker"] not in valid_workers:
            raise ValueError(f"Subtask {i} has invalid worker '{subtask['worker']}' (valid: {valid_workers})")

    # redecompose_after_research is only valid when ALL subtasks target research.
    # If non-research subtasks slipped in, the LLM misused the format — ignore the flag
    # and dispatch normally rather than blocking on a re-decompose that will never resolve.
    if redecompose_flag:
        non_research = [s for s in data if s.get("worker") != "research"]
        if non_research:
            redecompose_flag = False
            bad_workers = sorted({s.get("worker") for s in non_research})
            _module_log.warning(
                "redecompose_after_research flag cleared: %d non-research subtask(s) "
                "present (workers: %s) — dispatching normally instead.",
                len(non_research), bad_workers,
            )

    return data, redecompose_flag


def _sanitize_json_literals(raw: str) -> str:
    """
    Replace literal newlines / carriage returns / tabs that appear inside JSON
    string values with their escape sequences.  The regex matches JSON string
    literals (including already-escaped sequences via the \\. alternative) so
    it never double-escapes a '\n' that is already escaped.
    """
    def _fix(m: re.Match) -> str:
        return (
            m.group(0)
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )

    # "(?:[^"\\]|\\.)*" matches a JSON string including escape sequences.
    # re.DOTALL makes . match newlines so the unescaped-newline case is caught.
    return re.sub(r'"(?:[^"\\]|\\.)*"', _fix, raw, flags=re.DOTALL)


def parse_validation_decision(response: str) -> dict:
    """
    Parse the orchestrator's validation decision.
    Expected format is a JSON object with:
    {
      "decision": "complete|refine|additional_work|redo",
      "reasoning": "...",
      "follow_ups": [...]  # Only if decision != "complete"
    }

    Robustness measures applied before json.loads:
      1. Strip any prose before/after by extracting a ```json ... ``` fence if present.
      2. Sanitize literal newlines inside string values that would make the JSON invalid.
    """
    # Extract from code fence if present, otherwise use whole response.
    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response, re.DOTALL)
    json_str = json_match.group(1) if json_match else response.strip()

    # Sanitize literal control characters inside string values.
    json_str = _sanitize_json_literals(json_str)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse validation JSON: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    decision = data.get("decision")
    valid_decisions = {"complete", "refine", "additional_work", "redo"}
    if decision not in valid_decisions:
        raise ValueError(f"Invalid decision '{decision}' (valid: {valid_decisions})")

    return data
