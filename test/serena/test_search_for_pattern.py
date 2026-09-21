"""Tests for the ``SearchForPatternTool`` overflow shortening chain.

The snippet stage and, in particular, its position in the shortening chain were
previously untested. Test contributed by @AmirF194 in review of PR #1667.
"""

from unittest.mock import MagicMock

from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.tools.file_tools import SearchForPatternTool


def test_search_for_pattern_snippet_stage(tmp_path):
    lines: list[str] = []
    for i in range(60):
        lines += [
            "filler above",
            "filler above",
            f"MATCHME item number {i:04d} " + "payload " * 6,
            "filler below",
            "filler below",
        ]
    (tmp_path / "data.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    project = Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    tool = SearchForPatternTool(agent)

    def run(cap: int) -> str:
        return tool.apply(
            substring_pattern="MATCHME",
            context_lines_before=2,
            context_lines_after=2,
            restrict_search_to_code_files=False,
            max_answer_chars=cap,
        )

    # wide but overflowing cap: the snippet stage (line + matched text) is returned
    snippet = run(7000)
    assert "The answer is too long" in snippet
    assert '"text":' in snippet and "MATCHME item number 0000" in snippet
    assert "Match lines per file" not in snippet  # not the bare-line-numbers stage

    # tighter cap: the chain degrades past the snippet stage to bare line numbers
    bare = run(1000)
    assert "Match lines per file" in bare and '"text":' not in bare



def test_search_for_pattern_many_matches_on_one_huge_line_is_memory_bounded(tmp_path, monkeypatch):
    # This models minified/generated files: the same very long line can contain thousands of
    # regex hits. Rendering that full line once per hit used to create enormous temporary strings.
    huge_line = "MATCHME " * 20_000
    (tmp_path / "minified.txt").write_text(huge_line, encoding="utf-8")

    project = Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    tool = SearchForPatternTool(agent)

    from serena.util.text_utils import MatchedConsecutiveLines

    def fail_full_render(self, *args, **kwargs):
        raise AssertionError("oversized repeated line was materialised once per match")

    monkeypatch.setattr(MatchedConsecutiveLines, "to_display_string", fail_full_render)

    result = tool.apply(
        substring_pattern="MATCHME",
        relative_path="minified.txt",
        restrict_search_to_code_files=False,
        max_answer_chars=2000,
    )

    assert "The answer is too long" in result
    assert "Match counts per file" in result or "Found 20000 matches" in result
    assert len(result) <= 2000
