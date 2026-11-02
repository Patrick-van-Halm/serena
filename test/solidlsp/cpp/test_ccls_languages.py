import os

import pytest

from solidlsp.ls_config import LanguageServerId
from test.conftest import language_server_tests_enabled, start_ls_context


@pytest.mark.cpp
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.CPP_CCLS), reason="CCLS tests are disabled")
class TestCCLSLanguages:
    @pytest.mark.parametrize(
        "lang, unit, names",
        [
            ("C", "a.c", {"main"}),
            ("Objective-C", "a.m", {"main", "Hello", "hello"}),
        ],
    )
    def test_get_document_symbols(self, lang: str, unit: str, names: set[str]) -> None:
        with start_ls_context(LanguageServerId.CPP_CCLS) as ccls:
            path = os.path.join(lang, unit)
            symbols, roots = ccls.request_document_symbols(path).get_all_symbols_and_roots()
            actual_names = {symbol["name"] for symbol in symbols}
            assert names == actual_names, f"Expected '{names}' in document symbols, got: {actual_names}"
            if lang == "Objective-C":
                interface = next(symbol for symbol in roots if symbol["name"] == "Hello")
                assert {child["name"] for child in interface["children"]} == {"hello"}
