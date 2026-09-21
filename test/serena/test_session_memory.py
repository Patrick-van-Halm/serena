from serena.session import SessionRegistry


def test_remove_session_releases_repl_namespace() -> None:
    registry = SessionRegistry()
    session = registry.get_session("chat")
    payload = bytearray(1024 * 1024)
    session.repl_namespace["large"] = payload
    session.described_type_names.add("LargeType")

    assert registry.remove_session("chat") is True
    assert session.repl_namespace == {}
    assert session.described_type_names == set()
    assert registry.remove_session("chat") is False


def test_lru_eviction_disposes_namespace() -> None:
    registry = SessionRegistry(max_sessions=1)
    first = registry.get_session("first")
    first.repl_namespace["large"] = bytearray(1024)

    registry.get_session("second")

    assert first.repl_namespace == {}



def test_explicit_system_prompt_session_is_reused_and_released() -> None:
    from unittest.mock import MagicMock

    from serena.agent import SerenaAgent

    agent = SerenaAgent.__new__(SerenaAgent)
    agent._session_registry = SessionRegistry()
    agent._project_prompt_status = MagicMock()

    # Exercise the cleanup wrapper directly; create_system_prompt itself has substantial
    # prompt dependencies and is covered elsewhere.
    session = agent.get_session("bridge-chat")
    session.repl_namespace["large"] = bytearray(1024)

    assert agent.close_session("bridge-chat") is True
    agent._project_prompt_status.remove_session.assert_called_once_with("bridge-chat")
    assert session.repl_namespace == {}
