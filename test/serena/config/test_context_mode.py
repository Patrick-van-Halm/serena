import pytest

from interprompt.jinja_template import JinjaTemplate
from serena.agent import ToolSet
from serena.config.context_mode import SerenaAgentContext
from serena.tools import ToolRegistry

GROK_EXCLUDED_TOOLS = {
    "create_text_files",
    "delete_paths",
    "copy_paths",
    "move_paths",
    "read_files",
    "execute_shell_command",
    "find_files",
    "list_dirs",
    "search_for_patterns",
}

BUILTIN_RUNTIME_CONTEXT_NAMES = [
    name for name in SerenaAgentContext.list_registered_context_names(include_user_contexts=False) if name != "context.template"
]


def _render_context_prompt(context: SerenaAgentContext) -> str:
    """Render with the same template variables SerenaAgent._format_prompt provides.

    Context templates use the same name mapping as the outer system prompt.
    """
    names = set(ToolRegistry().get_tool_names_default_enabled())
    return JinjaTemplate(context.prompt).render(
        available_tools=names,
        available_markers=["ToolMarkerSymbolicRead"],
        tool_names=dict(ToolSet.LEGACY_TOOL_NAME_MAPPING) | {name: name for name in names},
    )


def test_grok_context_loads():
    context = SerenaAgentContext.from_name("grok")

    assert context.name == "grok"
    assert context.single_project is True
    assert context.structured_tool_output is None
    assert set(context.excluded_tools) == GROK_EXCLUDED_TOOLS


def test_grok_context_prompt_renders():
    context = SerenaAgentContext.from_name("grok")

    rendered_prompt = _render_context_prompt(context)

    assert rendered_prompt.strip()
    assert "Serena's code intelligence tools" in rendered_prompt


@pytest.mark.parametrize("context_name", BUILTIN_RUNTIME_CONTEXT_NAMES)
def test_builtin_contexts_load_and_prompt_templates_render(context_name: str):
    context = SerenaAgentContext.from_name(context_name)

    rendered_prompt = _render_context_prompt(context)

    assert context.name == context_name
    assert isinstance(context.excluded_tools, list)
    assert rendered_prompt == "" or rendered_prompt.strip()
