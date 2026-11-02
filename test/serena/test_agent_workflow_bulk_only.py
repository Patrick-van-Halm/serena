# SPDX-License-Identifier: GPL-3.0-or-later

"""Public bulk-tool selection, permissions, recovery and instruction contracts."""

from types import SimpleNamespace

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

from serena.agent import AvailableTools, SerenaAgent, ToolSet
from serena.config.serena_config import AgentInterface, LanguageBackend, NamedToolInclusionDefinition
from serena.constants import PROMPT_TEMPLATES_DIR_INTERNAL, SERENAS_OWN_MODE_YAMLS_DIR
from serena.generated.generated_prompt_factory import PromptFactory
from serena.shared_mcp_recovery import DaemonRecovery, DaemonRecoveryRefusedError, RecoveryEvidence, RecoveryScope
from serena.tools import FindReferencingSymbolsTool, FindSymbolTool, GetSymbolsOverviewTool, SerenaReplTool, ToolRegistry


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["read_file", "list_dir", "replace_content"], {"read_files", "list_dirs", "replace_contents"}),
        (["read_file", "list_dir", "replace_regex"], {"read_files", "list_dirs", "replace_contents"}),
        (["read_files", "list_dirs", "replace_contents"], {"read_files", "list_dirs", "replace_contents"}),
        (["create_text_file", "copy_path", "read_file_raw"], {"create_text_files", "copy_paths", "read_files_raw"}),
        (["read_memory", "write_memory", "rename_memory"], {"read_memories", "write_memories", "rename_memories"}),
        (["find_symbol", "get_diagnostics_for_file", "rename_symbol"], {"find_symbols", "get_diagnostics_for_files", "rename_symbols"}),
        (
            ["jet_brains_get_symbols_overview", "jet_brains_move", "jet_brains_inline_symbol"],
            {"jet_brains_get_symbols_overviews", "jet_brains_move_items", "jet_brains_inline_symbols"},
        ),
    ],
)
def test_fixed_and_included_configurations_resolve_to_supported_bulk_tools(names, expected):
    fixed = NamedToolInclusionDefinition(name="fixed", fixed_tools=names)
    included = NamedToolInclusionDefinition(name="included", included_optional_tools=names)
    assert ToolSet.default().apply(fixed).get_tool_names() == expected
    assert ToolSet(set()).apply(included).get_tool_names() == expected
    for name in expected:
        assert ToolRegistry().get_tool_class_by_name(name).get_name_from_cls() == name


def test_legacy_exclusions_still_protect_the_corresponding_capabilities():
    selection = ToolSet({"read_files", "list_dirs", "replace_contents", "read_files_raw"})
    exclusions = NamedToolInclusionDefinition(name="legacy", excluded_tools=["read_file", "list_dir", "replace_regex"])
    assert selection.apply(exclusions).get_tool_names() == {"read_files_raw"}
    assert selection.without_editing_tools().get_tool_names() == {"read_files", "list_dirs", "read_files_raw"}


@pytest.mark.parametrize(
    ("legacy", "bulk"),
    [
        ("create_text_file", "create_text_files"),
        ("delete_path", "delete_paths"),
        ("edit_memory", "edit_memories"),
        ("replace_symbol_body", "replace_symbol_bodies"),
        ("jet_brains_safe_delete", "jet_brains_safe_delete_items"),
    ],
)
def test_legacy_edit_exclusions_and_read_only_policy_protect_bulk_capabilities(legacy, bulk):
    selection = ToolSet({bulk, "read_files"})
    exclusions = NamedToolInclusionDefinition(name="legacy", excluded_tools=[legacy])
    assert selection.apply(exclusions).get_tool_names() == {"read_files"}
    assert selection.without_editing_tools().get_tool_names() == {"read_files"}


@pytest.mark.parametrize(
    ("interface", "backend", "expected_find", "expected_overview"),
    [
        (AgentInterface.TOOLS, LanguageBackend.LSP, "find_symbols", "get_symbols_overviews"),
        (AgentInterface.TOOLS, LanguageBackend.JETBRAINS, "jet_brains_find_symbols", "jet_brains_get_symbols_overviews"),
        (AgentInterface.REPL, LanguageBackend.LSP, "lsp.find_symbol", "lsp.get_symbols_overview"),
        (AgentInterface.REPL, LanguageBackend.JETBRAINS, "jb.find_symbol", "jb.get_symbols_overview"),
    ],
)
def test_initial_instructions_name_callable_operations_for_the_interface_and_backend(
    interface, backend, expected_find, expected_overview, monkeypatch
):
    from serena.repl.facade import ApiScope
    from serena.repl.repl import SerenaReplEntrypoint

    # assemble real tool/facade metadata without starting an unrelated language server or IDE
    agent = SerenaAgent.__new__(SerenaAgent)
    agent._prompt_params = None
    agent._agent_interface = interface
    agent._language_backend = backend
    agent._all_tools = {cls: cls(agent) for cls in ToolRegistry().get_all_tool_classes()}
    if interface == AgentInterface.REPL:
        scope = ApiScope()
        entrypoint = SerenaReplEntrypoint(backend.create_facades(agent, scope), scope)
        monkeypatch.setattr(agent, "get_repl", lambda: SimpleNamespace(entrypoint=entrypoint))
        exposed = [SerenaReplTool]
    else:
        replacements = backend.get_lsp_tool_class_replacements()
        exposed = [replacements.get(cls, cls) for cls in (FindSymbolTool, GetSymbolsOverviewTool, FindReferencingSymbolsTool)]
    agent._exposed_tools = AvailableTools([agent._all_tools[cls] for cls in exposed])
    params = agent._get_prompt_params()

    # the rendered instructions must name operations the selected interface can actually call
    prompt = PromptFactory(PROMPT_TEMPLATES_DIR_INTERNAL).create_system_prompt(
        available_markers=params.available_markers,
        available_tools=params.available_tools,
        context_system_prompt="",
        global_memories_list="",
        mode_system_prompts=[],
        tool_names=params.tool_names_mapping,
    )
    assert f"`{expected_find}`" in prompt
    assert f"`{expected_overview}`" in prompt


def test_initial_instructions_describe_single_item_arrays():
    names = set(ToolRegistry().get_tool_names_default_enabled())
    mapping = dict(ToolSet.LEGACY_TOOL_NAME_MAPPING) | {name: name for name in names}
    factory = PromptFactory(PROMPT_TEMPLATES_DIR_INTERNAL)
    prompt = factory.create_system_prompt(
        available_markers=set(),
        available_tools=names,
        context_system_prompt="",
        global_memories_list="",
        mode_system_prompts=[],
        tool_names=mapping,
    )
    assert "one-item `files` array" in prompt
    assert "one-item `directories` array" in prompt
    assert "one-item `replacements` array" in prompt


def test_editing_instructions_distinguish_classic_batches_from_repl_methods():
    from pathlib import Path

    template = yaml.safe_load(Path(SERENAS_OWN_MODE_YAMLS_DIR, "editing.yml").read_text(encoding="utf-8"))["prompt"]
    names = set(ToolRegistry().get_tool_names())
    mapping = dict(ToolSet.LEGACY_TOOL_NAME_MAPPING) | {name: name for name in names}
    env = Environment(undefined=StrictUndefined)
    classic = env.from_string(template).render(available_tools=names, tool_names=mapping)
    assert "one-item array for a single change" in classic
    assert "`replace_contents`" in classic
    repl_mapping = dict(mapping, replace_contents="edit.replace_content")
    repl = env.from_string(template).render(available_tools=names | {"replace_content"}, tool_names=repl_mapping)
    assert "`edit.replace_content`" in repl


@pytest.mark.parametrize(
    "tool_name", ["read_files", "list_dirs", "find_symbols", "search_for_patterns", "replace_contents", "rename_symbols"]
)
def test_recovery_requires_repeated_evidence_and_retrieval_only_work(tool_name, monkeypatch):
    class EvidenceSource:
        def get_recovery_evidence(self, scope):
            return RecoveryEvidence("operation", tool_name, tool_name not in {"replace_contents", "rename_symbols"}, True, 31.0)

    now = [100.0]
    monkeypatch.setattr("serena.shared_mcp_recovery.time.monotonic", lambda: now[0])
    controller = DaemonRecovery(EvidenceSource())
    scope = RecoveryScope("/project", "agent", "session")
    if tool_name in {"replace_contents", "rename_symbols"}:
        with pytest.raises(DaemonRecoveryRefusedError):
            controller.observe(scope)
        return
    controller.observe(scope)
    with pytest.raises(DaemonRecoveryRefusedError):
        controller.claim(scope, "operation")
    controller.observe(scope)
    now[0] += 2.1
    token = controller.claim(scope, "operation")
    controller.release(scope, token)
    with controller.admit():
        pass
