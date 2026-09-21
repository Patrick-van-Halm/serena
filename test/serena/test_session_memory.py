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
