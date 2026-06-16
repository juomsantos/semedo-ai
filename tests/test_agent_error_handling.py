"""
Tests that lock in the consistent error-handling patterns across agents.

These are *static* tests — they parse each agent's source with ``ast`` and
verify the standardized patterns are present. They are intentionally NOT
behavioral tests, because:

  - Running an agent's ``main()`` requires Ollama (or a heavy mock of it).
  - The pre-flight check exits the process via ``sys.exit(1)``; pytest
    would need to fork to test it.

What we lock in (the error-handling standardization):

  1. Every Ollama-using agent has a startup ``is_available()`` check that
     exits non-zero if the LLM server is unreachable. This is the
     "pre-flight" pattern documented in CLAUDE.md.
  2. The per-task LLM call is wrapped in ``except OllamaError`` so a
     transient Ollama error doesn't crash the cycle — the task is either
     marked failed (workers + orchestrator decompose) or left alone for
     retry next cycle (orchestrator validation).
  3. The task-loop in ``main()`` has an outer ``except Exception`` so one
     corrupt task doesn't kill the whole run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"

# Agents that talk to Ollama directly. claude-code is excluded — it uses the
# Claude CLI via subprocess, not the Ollama client.
OLLAMA_AGENTS = ("agent_coder.py", "agent_research.py", "agent_qa.py", "agent_orchestrator.py")

# All five agent scripts (Ollama agents + claude-code).
ALL_AGENTS = OLLAMA_AGENTS + ("agent_claude_code.py",)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _walk_in(scope: ast.AST, types: tuple[type, ...]) -> list[ast.AST]:
    return [n for n in ast.walk(scope) if isinstance(n, types)]


# ---------------------------------------------------------------------------
# Pattern 1 — startup pre-flight check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_file", OLLAMA_AGENTS)
def test_ollama_agent_calls_is_available_in_main(agent_file):
    """Every Ollama-using agent must check ``client.is_available()`` in main()
    so a quick "Ollama is down" check happens before any task files are touched.
    """
    tree = _parse(SCRIPTS_DIR / agent_file)
    main_fn = _find_function(tree, "main")
    assert main_fn is not None, f"{agent_file} has no main() function"

    is_available_calls = [
        n for n in _walk_in(main_fn, (ast.Call,))
        if isinstance(n.func, ast.Attribute) and n.func.attr == "is_available"
    ]
    assert is_available_calls, (
        f"{agent_file}::main() must call client.is_available() before "
        f"processing tasks. This is the startup pre-flight pattern; "
        f"without it, an unreachable Ollama server only surfaces as confusing "
        f"per-task errors."
    )


@pytest.mark.parametrize("agent_file", OLLAMA_AGENTS)
def test_ollama_agent_exits_when_ollama_unreachable(agent_file):
    """The pre-flight check must call ``sys.exit(1)`` when Ollama is
    unreachable, not silently continue."""
    src = (SCRIPTS_DIR / agent_file).read_text(encoding="utf-8")
    # Look for the canonical pattern: an `is_available()` check followed by
    # sys.exit(1) within a few lines. We don't enforce exact spacing — just
    # that both appear and the exit follows the check.
    is_avail_idx = src.find("is_available()")
    assert is_avail_idx >= 0, f"{agent_file} missing is_available() call entirely"
    # Look for sys.exit(1) AFTER the is_available call (within the next
    # ~500 chars — generous so error logging fits in between).
    region = src[is_avail_idx : is_avail_idx + 500]
    assert "sys.exit(1)" in region, (
        f"{agent_file}: expected sys.exit(1) within ~500 chars after "
        f"is_available() call so the scheduler sees a clear failure code"
    )


def test_claude_code_agent_verifies_cli_in_main():
    """The claude-code agent doesn't use Ollama; its analogous pre-flight is a
    ``claude --version`` subprocess check that exits non-zero on failure."""
    src = (SCRIPTS_DIR / "agent_claude_code.py").read_text(encoding="utf-8")
    assert "claude" in src and "--version" in src, (
        "agent_claude_code.py should verify the claude CLI is available before "
        "processing tasks (the pre-flight pattern for non-Ollama agents)"
    )
    assert "sys.exit(1)" in src, (
        "agent_claude_code.py must sys.exit(1) when the claude CLI is missing"
    )


# ---------------------------------------------------------------------------
# Pattern 2 — per-task OllamaError handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_file", OLLAMA_AGENTS)
def test_ollama_agent_catches_ollama_error(agent_file):
    """Every Ollama-using agent must have at least one ``except OllamaError``
    block so a transient API error becomes a logged failure, not a crash."""
    tree = _parse(SCRIPTS_DIR / agent_file)
    handlers = _walk_in(tree, (ast.ExceptHandler,))
    catches_ollama = [
        h for h in handlers
        if h.type is not None and _exception_name(h.type) == "OllamaError"
    ]
    assert catches_ollama, (
        f"{agent_file}: missing `except OllamaError` block. The per-task LLM "
        f"call must catch OllamaError so a transient API error becomes a "
        f"logged failure rather than a process crash."
    )


def _exception_name(node: ast.expr) -> str | None:
    """Return the simple name of an exception (e.g. ``OllamaError``) from an
    except-handler type expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Tuple):
        # `except (OllamaError, ValueError)` — return first name only
        for elt in node.elts:
            name = _exception_name(elt)
            if name:
                return name
    return None


# ---------------------------------------------------------------------------
# Pattern 3 — outer task-loop guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_file", ALL_AGENTS)
def test_main_loop_has_outer_exception_guard(agent_file):
    """Every agent's ``main()`` task loop wraps ``process_task`` in
    ``try / except Exception`` so one bad task doesn't kill the whole cycle."""
    tree = _parse(SCRIPTS_DIR / agent_file)
    main_fn = _find_function(tree, "main")
    assert main_fn is not None, f"{agent_file} has no main()"

    # Find every `for task_path in tasks:` loop inside main()
    task_loops = [
        n for n in _walk_in(main_fn, (ast.For,))
        if isinstance(n.target, ast.Name) and "task" in n.target.id.lower()
    ]
    assert task_loops, f"{agent_file}::main() has no task iteration loop"

    for loop in task_loops:
        # Each task loop body must contain a Try whose handlers include
        # `except Exception` (bare-Exception catch is the *intended* pattern
        # at this outer boundary — it's the safety net of last resort).
        tries = _walk_in(loop, (ast.Try,))
        has_exception_guard = any(
            any(_exception_name(h.type) == "Exception"
                for h in t.handlers if h.type is not None)
            for t in tries
        )
        assert has_exception_guard, (
            f"{agent_file}::main() task loop is missing the outer "
            f"`try / except Exception` guard. Without it, one corrupt task "
            f"file can crash the agent for the whole cycle, blocking every "
            f"other queued task."
        )


# ---------------------------------------------------------------------------
# Pattern detail — QA retries empty responses (the one intentional asymmetry)
# ---------------------------------------------------------------------------


def test_qa_retries_empty_llm_response():
    """QA is the only agent that retries on empty LLM responses (intentional —
    its verdict is the gate decision and a missing one defaults to FAIL). This
    test locks in that the retry block is still present."""
    src = (SCRIPTS_DIR / "agent_qa.py").read_text(encoding="utf-8")
    assert "empty_response_retries" in src, (
        "agent_qa.py should still have the empty-response retry loop. If "
        "you intentionally removed it, update CLAUDE.md's "
        "'Agent error-handling patterns' bullet to reflect the change."
    )
