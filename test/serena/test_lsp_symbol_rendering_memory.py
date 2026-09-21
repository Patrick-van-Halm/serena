from unittest.mock import MagicMock

from serena.repl.api.lsp_api import LspSymbolCollection, LspSymbolCollectionRenderer, SymbolOutputParams


def test_symbol_renderer_skips_full_dict_tree_when_mandatory_values_exceed_budget() -> None:
    agent = MagicMock()
    agent.serena_config.default_max_tool_answer_chars = 200

    symbols = []
    for i in range(20):
        symbol = MagicMock()
        symbol.get_name_path.return_value = f"VeryLongContainerName{i:04d}/VeryLongMethodName{i:04d}"
        symbol.location.relative_path = "src/big.py"
        symbol.to_dict.side_effect = AssertionError("full symbol dictionary should not be materialised")
        symbols.append(symbol)

    renderer = LspSymbolCollectionRenderer(
        agent,
        -1,
        SymbolOutputParams(name_path=True, relative_path=True, depth=3, include_body=True),
    )
    collection = LspSymbolCollection(symbols, renderer)

    result = collection.represent()

    assert "answer is too long" in result.lower() or "Shortened result" in result
    for symbol in symbols:
        symbol.to_dict.assert_not_called()



def test_symbol_renderer_preflights_bodies_before_materialising_dict_collection() -> None:
    agent = MagicMock()
    agent.serena_config.default_max_tool_answer_chars = 200

    symbols = []
    for i in range(4):
        symbol = MagicMock()
        symbol.get_name_path.return_value = f"symbol{i}"
        symbol.location.relative_path = "src/big.py"
        symbol.body = "x" * 150
        symbol.to_dict.side_effect = AssertionError("full body dictionary collection should not be materialised")
        symbols.append(symbol)

    renderer = LspSymbolCollectionRenderer(
        agent,
        -1,
        SymbolOutputParams(name_path=True, relative_path=True, include_body=True),
    )
    collection = LspSymbolCollection(symbols, renderer)

    result = collection.represent()

    assert "Shortened result" in result or "answer is too long" in result.lower()
    for symbol in symbols:
        symbol.to_dict.assert_not_called()
