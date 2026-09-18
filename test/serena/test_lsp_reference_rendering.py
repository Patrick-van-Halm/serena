from unittest.mock import MagicMock

from serena.repl.api.lsp_api import LspApi, LspReferenceCollectionRenderer


def _reference(path: str, line: int):
    ref = MagicMock()
    ref.line = line
    ref.symbol.to_dict.return_value = {
        "name_path": f"Symbol{line}",
        "kind": "Function",
        "relative_path": path,
        "body_location": {"start_line": line, "end_line": line},
    }
    return ref


def test_reference_context_is_not_loaded_when_metadata_already_exceeds_budget() -> None:
    agent = MagicMock()
    agent.serena_config.default_max_tool_answer_chars = 100
    renderer = LspReferenceCollectionRenderer(agent, -1, LspApi.references_grouper_)
    references = [_reference("src/very_long_path_name.py", i) for i in range(20)]
    loader = MagicMock(return_value="context")

    contents = renderer.collect_context_with_budget(references, loader)

    assert contents == [None] * len(references)
    loader.assert_not_called()


def test_reference_context_loading_stops_when_answer_budget_is_consumed() -> None:
    agent = MagicMock()
    agent.serena_config.default_max_tool_answer_chars = 1200
    renderer = LspReferenceCollectionRenderer(agent, -1, LspApi.references_grouper_)
    references = [_reference("src/a.py", i) for i in range(20)]
    loader = MagicMock(return_value="x" * 180)

    contents = renderer.collect_context_with_budget(references, loader)

    assert 0 < loader.call_count < len(references)
    assert sum(content is not None for content in contents) == loader.call_count
    assert len(contents) == len(references)
