"""
Tests for the editing facade API (backend-independent parts, without a language server).
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.repl.api.edit_api import EditApi, ReplacementPreview
from serena.repl.facade import ApiScope, Facade


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "a.py").write_text("x = foo(1)\ny = foo(2)\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("z = foo(3)\n", encoding="utf-8")
    return Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))


@pytest.fixture
def api(project: Project) -> EditApi:
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    agent.serena_config.default_max_tool_answer_chars = 10000
    return EditApi(agent)


def test_facade_exposes_editing_operations(api: EditApi) -> None:
    default_methods = {
        "replace_content",
        "replace_in_files",
        "replace_symbol_body",
        "insert_after_symbol",
        "insert_before_symbol",
    }
    optional_methods = {"delete_lines", "replace_lines", "insert_at_line"}  # the line-level operations are optional (as are the tools)

    facade = Facade.from_api(api, ApiScope())
    assert facade.name == "edit"
    assert set(facade.enabled_method_names) == default_methods
    for name in optional_methods:
        assert facade.get_method(name).info.optional
    assert all(facade.get_method(name).info.can_edit for name in default_methods | optional_methods)


def test_replace_in_files_dry_run_returns_inspectable_preview(api: EditApi, project: Project) -> None:
    preview = api.replace_in_files("foo", "bar", mode="literal", dry_run=True)
    assert isinstance(preview, ReplacementPreview)

    # the occurrences are accessible from code
    assert [o.relative_path for o in preview.occurrences] == ["a.py", "a.py", "b.py"]
    assert preview.affected_files == ["a.py", "b.py"]
    assert all(o.replacement == "bar" for o in preview.occurrences)

    # the rendering lists the occurrence ids and diffs; nothing was modified
    rendered = preview.represent()
    assert "DRY RUN" in rendered
    assert all(o.occurrence_id in rendered for o in preview.occurrences)
    assert (Path(project.project_root) / "a.py").read_text(encoding="utf-8") == "x = foo(1)\ny = foo(2)\n"


def test_replace_in_files_guard_failure_includes_preview(api: EditApi) -> None:
    with pytest.raises(ValueError, match="expected_count=1") as exc_info:
        api.replace_in_files("foo", "bar", mode="literal", expected_count=1)
    assert "b.py" in str(exc_info.value)  # the listing of prospective changes is included


def _make_fake_code_editor(content: str, fail_on_insert: bool = False):
    """A string-backed stand-in for CodeEditor that tracks how many times it saves."""
    import contextlib

    from solidlsp.ls_utils import TextUtils

    state = {"contents": content, "opened": 0, "saved": 0}

    class FakeEditedFile:
        def __init__(self, contents: str):
            self.contents = contents

        def delete_text_between_positions(self, start_pos, end_pos):
            new, _ = TextUtils.delete_text_between_positions(self.contents, start_pos.line, start_pos.col, end_pos.line, end_pos.col)
            self.contents = new

        def insert_text_at_position(self, pos, text):
            if fail_on_insert:
                raise ValueError("insert failed")
            new, _, _ = TextUtils.insert_text_at_position(self.contents, pos.line, pos.col, text)
            self.contents = new

    class FakeCodeEditor:
        def edited_file_context(self, relative_path):
            @contextlib.contextmanager
            def _ctx():
                state["opened"] += 1
                edited = FakeEditedFile(state["contents"])
                yield edited
                state["contents"] = edited.contents
                state["saved"] += 1

            return _ctx()

    return FakeCodeEditor(), state


def test_replace_lines_uses_a_single_write(api: EditApi, project: Project, monkeypatch) -> None:
    fake, state = _make_fake_code_editor("a\nb\nc\n")
    monkeypatch.setattr(api, "_create_code_editor", lambda: fake)
    assert api.replace_lines("a.py", 1, 1, "inserted\n") == "OK"
    assert state["opened"] == 1
    assert state["saved"] == 1
    assert state["contents"] == "a\ninserted\nc\n"


def test_replace_lines_does_not_save_when_insertion_fails(api: EditApi, project: Project, monkeypatch) -> None:
    fake, state = _make_fake_code_editor("a\nb\nc\n", fail_on_insert=True)
    monkeypatch.setattr(api, "_create_code_editor", lambda: fake)
    with pytest.raises(ValueError, match="insert failed"):
        api.replace_lines("a.py", 1, 1, "inserted\n")
    assert state["opened"] == 1
    assert state["saved"] == 0
    assert state["contents"] == "a\nb\nc\n"
