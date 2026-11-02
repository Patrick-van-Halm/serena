import json
from unittest.mock import MagicMock

from serena.repl.api.lsp_api import LspApi, LspReferenceCollection, LspReferenceCollectionRenderer
from serena.symbol import LanguageServerSymbol, ReferenceInLanguageServerSymbol
from solidlsp.ls_types import SymbolKind


def _reference(path: str, line: int) -> ReferenceInLanguageServerSymbol:
    symbol = LanguageServerSymbol(
        {
            "name": f"Symbol{line}",
            "kind": SymbolKind.Function,
            "children": [],
            "location": {
                "uri": f"file:///project/{path}",
                "absolutePath": f"/project/{path}",
                "relativePath": path,
                "range": {
                    "start": {"line": line, "character": 0},
                    "end": {"line": line, "character": 10},
                },
            },
        }
    )
    return ReferenceInLanguageServerSymbol(symbol=symbol, line=line, character=0)


def test_reference_context_is_not_loaded_when_metadata_already_exceeds_budget() -> None:
    agent = MagicMock()
    agent.serena_config.default_max_tool_answer_chars = 1200
    renderer = LspReferenceCollectionRenderer(agent, -1, LspApi.references_grouper_)
    references = [_reference("src/very_long_path_name.py", i) for i in range(20)]
    loader = MagicMock(return_value="context")

    contents = renderer.collect_context_with_budget(references, loader)

    assert contents == [None] * len(references)
    loader.assert_not_called()


def test_reference_context_loading_stops_when_answer_budget_is_consumed() -> None:
    agent = MagicMock()
    agent.serena_config.default_max_tool_answer_chars = 750
    renderer = LspReferenceCollectionRenderer(agent, -1, LspApi.references_grouper_)
    references = [_reference("src/a.py", i) for i in range(3)]
    snippet = '"\\\t' * 60
    loader = MagicMock(return_value=snippet)

    contents = renderer.collect_context_with_budget(references, loader)
    result = LspReferenceCollection(references, contents, renderer).represent()

    # a rejected probe may require one read, but later references must not load source
    assert loader.call_count < len(references)
    assert contents == [snippet, None, None]
    assert len(result) <= 750
    rendered_references = json.loads(result)["src/a.py"]["Function"]
    assert [ref["name_path"] for ref in rendered_references] == ["Symbol0", "Symbol1", "Symbol2"]
    assert [ref.get("content_around_reference") for ref in rendered_references] == [snippet, None, None]
