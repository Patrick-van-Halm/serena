# SPDX-License-Identifier: GPL-3.0-or-later

"""Public bulk-tool selection, permissions, recovery and instruction contracts."""

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

from serena.agent import ToolSet
from serena.config.serena_config import NamedToolInclusionDefinition
from serena.constants import PROMPT_TEMPLATES_DIR_INTERNAL, SERENAS_OWN_MODE_YAMLS_DIR
from serena.generated.generated_prompt_factory import PromptFactory
from serena.shared_mcp_recovery import DaemonRecovery, DaemonRecoveryRefusedError, RecoveryEvidence, RecoveryScope
from serena.tools import ToolRegistry


@pytest.mark.parametrize(
    "names",
    [
        ["read_file", "list_dir", "replace_content"],
        ["read_file", "list_dir", "replace_regex"],
        ["read_files", "list_dirs", "replace_contents"],
    ],
)
def test_fixed_and_included_configurations_resolve_to_supported_bulk_tools(names):
    expected = {"read_files", "list_dirs", "replace_contents"}
    fixed = NamedToolInclusionDefinition(name="fixed", fixed_tools=names)
    included = NamedToolInclusionDefinition(name="included", included_optional_tools=names)
    assert ToolSet.default().apply(fixed).get_tool_names() == expected
    assert ToolSet(set()).apply(included).get_tool_names() == expected
    for name in expected:
        assert ToolRegistry().get_tool_class_by_name(name).get_name_from_cls() == name


def test_legacy_exclusions_still_protect_the_corresponding_capabilities():
    selection = ToolSet({"read_files", "list_dirs", "replace_contents", "read_file_raw"})
    exclusions = NamedToolInclusionDefinition(name="legacy", excluded_tools=["read_file", "list_dir", "replace_regex"])
    assert selection.apply(exclusions).get_tool_names() == {"read_file_raw"}
    assert selection.without_editing_tools().get_tool_names() == {"read_files", "list_dirs", "read_file_raw"}


def test_initial_instructions_describe_single_item_arrays():
    names = set(ToolRegistry().get_tool_names_default_enabled())
    mapping = {name: name for name in names}
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
    mapping = {name: name for name in names}
    env = Environment(undefined=StrictUndefined)
    classic = env.from_string(template).render(available_tools=names, tool_names=mapping)
    assert "one-item array for a single change" in classic
    assert "`replace_contents`" in classic
    repl_mapping = dict(mapping, replace_contents="edit.replace_content")
    repl = env.from_string(template).render(available_tools=names | {"replace_content"}, tool_names=repl_mapping)
    assert "`edit.replace_content`" in repl


@pytest.mark.parametrize("tool_name", ["read_files", "list_dirs", "replace_contents"])
def test_recovery_requires_repeated_evidence_and_retrieval_only_work(tool_name, monkeypatch):
    class EvidenceSource:
        def get_recovery_evidence(self, scope):
            return RecoveryEvidence("operation", tool_name, tool_name != "replace_contents", True, 31.0)

    now = [100.0]
    monkeypatch.setattr("serena.shared_mcp_recovery.time.monotonic", lambda: now[0])
    controller = DaemonRecovery(EvidenceSource())
    scope = RecoveryScope("/project", "agent", "session")
    if tool_name == "replace_contents":
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
