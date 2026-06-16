"""
Unit tests for the pure helper functions in orchestration/validate.py.

These back the orchestrator's file-extraction step (writing named coder-output
files to outputs/<parent>/...). They are security- and correctness-sensitive
but were previously untested:

  * ``_extract_named_files``  — parse ``**path**`` + fenced-code blocks
  * ``_is_safe_output_path``  — reject absolute paths and ``..`` traversal
  * ``_normalise_path``       — canonicalize a relative path for comparison

All three are pure string functions — no fixtures, no filesystem.

Note on portability: ``_is_safe_output_path`` cases here use only the
cross-platform-stable inputs (relative paths, leading ``/``, leading ``\\``,
and ``..`` segments). Windows drive-letter behavior (``C:\\...``) differs
between POSIX and Windows ``pathlib`` and is intentionally not asserted.
"""

import pytest

from orchestration.validate import (
    _extract_named_files,
    _is_safe_output_path,
    _normalise_path,
)


# ---------------------------------------------------------------------------
# _extract_named_files
# ---------------------------------------------------------------------------

def test_extract_single_named_file():
    body = (
        "Here is the code:\n\n"
        "**src/app.py**\n"
        "```python\n"
        "print('hello')\n"
        "```\n"
    )
    files = _extract_named_files(body)
    assert len(files) == 1
    path, content = files[0]
    assert path == "src/app.py"
    assert "print('hello')" in content


def test_extract_multiple_named_files_in_order():
    body = (
        "**a.py**\n```python\nA = 1\n```\n\n"
        "**pkg/b.py**\n```python\nB = 2\n```\n"
    )
    files = _extract_named_files(body)
    assert [p for p, _ in files] == ["a.py", "pkg/b.py"]
    assert "A = 1" in files[0][1]
    assert "B = 2" in files[1][1]


def test_extract_strips_whitespace_around_path():
    body = "**  src/foo.py  **\n```\nx=1\n```\n"
    files = _extract_named_files(body)
    assert files[0][0] == "src/foo.py"


def test_extract_plain_code_block_without_header_yields_nothing():
    body = "```python\nprint('no path header')\n```\n"
    assert _extract_named_files(body) == []


def test_extract_no_code_at_all_yields_nothing():
    assert _extract_named_files("Just prose, no code blocks here.") == []


def test_extract_preserves_multiline_content():
    body = "**m.py**\n```python\nline1\nline2\nline3\n```\n"
    _, content = _extract_named_files(body)[0]
    assert content.count("\n") >= 2
    assert "line1" in content and "line3" in content


def test_extract_handles_language_hint_after_fence():
    body = "**t.ts**\n```typescript\nconst x: number = 1;\n```\n"
    files = _extract_named_files(body)
    assert files[0][0] == "t.ts"
    assert "const x" in files[0][1]
    # The ```typescript fence line itself must not be part of the content.
    assert "typescript" not in files[0][1].splitlines()[0]


# ---------------------------------------------------------------------------
# _is_safe_output_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "foo.py",
    "src/foo.py",
    "a/b/c/deep.txt",
    "src/sub_dir/file-name.py",
])
def test_safe_paths_accepted(path):
    assert _is_safe_output_path(path) is True


@pytest.mark.parametrize("path", [
    "/etc/passwd",          # POSIX absolute
    "/foo.py",              # leading slash
    "\\windows\\system32",  # leading backslash
    "../secret",            # parent traversal
    "src/../../etc/x",      # traversal mid-path
    "a/../../b",            # traversal mid-path
])
def test_unsafe_paths_rejected(path):
    assert _is_safe_output_path(path) is False


def test_safe_path_with_dot_segment_is_allowed():
    # A single "." (current dir) is not "..", so it is not a traversal.
    assert _is_safe_output_path("./foo.py") is True


# ---------------------------------------------------------------------------
# _normalise_path
# ---------------------------------------------------------------------------

def test_normalise_strips_leading_dot_slash():
    assert _normalise_path("./src/foo.py") == "src/foo.py"


def test_normalise_converts_backslashes():
    assert _normalise_path("src\\sub\\foo.py") == "src/sub/foo.py"


def test_normalise_strips_surrounding_whitespace():
    assert _normalise_path("  src/foo.py  ") == "src/foo.py"


def test_normalise_strips_repeated_dot_slash():
    assert _normalise_path("././a") == "a"


def test_normalise_leaves_clean_path_unchanged():
    assert _normalise_path("src/foo.py") == "src/foo.py"


def test_normalise_equates_windows_and_posix_separators():
    """The whole point: a coder bold-header path and an LLM-manifest path that
    differ only in separator/prefix must normalise to the same string."""
    assert _normalise_path("src\\foo.py") == _normalise_path("./src/foo.py")
