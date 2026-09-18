"""
Tests for the file system facade API.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.repl.api.fs_api import FsApi
from serena.repl.facade import ApiScope, Facade
from serena.util.file_proxy import LocalProjectFileProxy


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = foo(1)\ny = foo(2)\nz = 3\n", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("foo in text\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    return Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))


@pytest.fixture
def api(project: Project) -> FsApi:
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    agent.serena_config.default_max_tool_answer_chars = 10000
    return FsApi(agent)


def test_facade_exposes_file_operations(api: FsApi) -> None:
    facade = Facade.from_api(api, ApiScope())
    assert facade.name == "fs"
    assert set(facade.enabled_method_names) == {"read_file", "create_text_file", "list_dir", "find_file", "search_for_pattern"}
    assert {name for name in facade.enabled_method_names if facade.get_method(name).info.can_edit} == {"create_text_file"}


def test_read_file(api: FsApi) -> None:
    content = api.read_file("src/a.py")
    assert content.lines == ["x = foo(1)", "y = foo(2)", "z = 3", ""]
    assert content.represent() == content.text

    assert api.read_file("src/a.py", start_line=1, end_line=1).text == "y = foo(2)"
    assert api.read_file("src/a.py", start_line=-2).lines == ["z = 3", ""]


def test_bounded_read_uses_streaming_local_path(api: FsApi, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_full_read(self: LocalProjectFileProxy) -> str:
        pytest.fail("bounded local read unexpectedly materialised the whole file")

    monkeypatch.setattr(LocalProjectFileProxy, "get_contents", fail_full_read)
    assert api.read_file("src/a.py", start_line=1, end_line=1).lines == ["y = foo(2)"]


def test_bounded_read_preserves_lsp_line_break_semantics(api: FsApi, project: Project) -> None:
    path = Path(project.project_root) / "mixed.txt"
    path.write_text("zero\r\none\rtwo\n\x0cthree\r\n", encoding="utf-8", newline="")

    assert api.read_file("mixed.txt", start_line=0, end_line=4).lines == ["zero", "one", "two", "\x0cthree", ""]
    assert api.read_file("mixed.txt", start_line=1, end_line=2).lines == ["one", "two"]


def test_create_text_file(api: FsApi, project: Project) -> None:
    result = api.create_text_file("sub/new.txt", "hello\n")
    assert "new.txt" in result
    assert (Path(project.project_root) / "sub" / "new.txt").read_text(encoding="utf-8") == "hello\n"

    result = api.create_text_file("sub/new.txt", "changed\n")
    assert "Overwrote" in result

    with pytest.raises(ValueError, match="full_access_mode"):
        api.create_text_file("../outside.txt", "nope")


def test_outside_file_operations_require_full_access(api: FsApi, project: Project, tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "outside.txt"
    outside_file.write_text("outside foo\n", encoding="utf-8")

    with pytest.raises(ValueError, match="full_access_mode"):
        api.read_file(str(outside_file))
    with pytest.raises(ValueError, match="full_access_mode"):
        api.list_dir(str(outside_dir), recursive=False)
    with pytest.raises(ValueError, match="full_access_mode"):
        api.find_file("*.txt", str(outside_dir))
    with pytest.raises(ValueError, match="full_access_mode"):
        api.search_for_pattern("outside", relative_path=str(outside_dir))


def test_full_access_mode_allows_outside_file_operations(api: FsApi, project: Project, tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-enabled"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "outside.txt"
    outside_file.write_text("outside foo\n", encoding="utf-8")
    project.serena_config.full_access_mode = True

    assert api.read_file(str(outside_file)).text == "outside foo\n"
    api.create_text_file(str(outside_dir / "created.txt"), "created\n")
    assert (outside_dir / "created.txt").read_text(encoding="utf-8") == "created\n"

    listing = api.list_dir(str(outside_dir), recursive=False)
    assert {Path(p).name for p in listing.files} >= {"outside.txt", "created.txt"}

    found = api.find_file("*.txt", str(outside_dir))
    assert {Path(p).name for p in found} >= {"outside.txt", "created.txt"}

    matches = api.search_for_pattern("outside", relative_path=str(outside_dir))
    assert len(matches) == 1
    assert Path(matches.matches[0].source_file_path or "").name == "outside.txt"


def test_full_access_mode_allows_relative_parent_escape(api: FsApi, project: Project, tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-relative"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "relative.txt"
    outside_file.write_text("relative access\n", encoding="utf-8")
    project.serena_config.full_access_mode = True

    escaped = str(Path("..") / outside_dir.name / "relative.txt")
    assert api.read_file(escaped).text == "relative access\n"


def test_list_dir_and_find_file(api: FsApi) -> None:
    listing = api.list_dir(".", recursive=True)
    assert "src" in listing.dirs
    assert {"src/a.py", "src/b.txt", "README.md"} <= {f.replace("\\", "/") for f in listing.files}
    assert '"dirs"' in listing.represent() and '"files"' in listing.represent()

    with pytest.raises(FileNotFoundError):
        api.list_dir("missing", recursive=False)

    assert [f.replace("\\", "/") for f in api.find_file("*.py", ".")] == ["src/a.py"]


def test_search_for_pattern(api: FsApi) -> None:
    matches = api.search_for_pattern("foo", relative_path="src")
    assert len(matches) == 3
    assert {m.source_file_path.replace("\\", "/") for m in matches.matches} == {"src/a.py", "src/b.txt"}

    # restricting to code files excludes the text file; the rendering maps files to matched lines
    code_matches = api.search_for_pattern("foo", restrict_search_to_code_files=True)
    assert all(m.source_file_path.endswith("a.py") for m in code_matches.matches)
    assert "foo(1)" in code_matches.represent()
