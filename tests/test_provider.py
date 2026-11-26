"""The provider ABC: an abstract contract with no test was free to rot.

Nothing implements MemoryProvider today. It is exported as public API, so the
contract itself is what has to hold: every method exists, the abstract ones
refuse a bare subclass, and the defaults do what they claim. Without this,
a future adapter would discover the breakage instead of CI.
"""
import pytest

from omp_memory import provider
from omp_memory.provider import (INDICATOR_GLYPH, MemoryProvider, RecallStatus,
                                 is_core_memory_provider)


def test_cannot_instantiate_without_the_abstract_methods():
    with pytest.raises(TypeError):
        MemoryProvider()  # type: ignore[abstract]


def _minimal() -> MemoryProvider:
    class Impl(MemoryProvider):
        @property
        def name(self) -> str:
            return "probe"

        def is_available(self) -> bool:
            return True

        def initialize(self, session_id: str, **kwargs) -> None:
            self.session = session_id

    return Impl()


def test_minimal_subclass_works_and_defaults_are_inert():
    impl = _minimal()
    impl.initialize("s1")
    assert impl.name == "probe"
    assert impl.is_available() is True
    # Defaults must be safe no-ops, not accidental behaviour.
    assert impl.system_prompt_block() == ""
    assert impl.prefetch("q") == ""
    assert impl.recall_status() is None
    assert impl.get_tool_schemas() == []
    assert impl.backup_paths() == []
    assert impl.identity_signature() == {}
    assert impl.on_pre_compress([]) == ""
    assert impl.unavailable_reason() == ""


def test_methods_that_must_be_implemented_raise_on_the_base():
    impl = _minimal()
    with pytest.raises(NotImplementedError):
        impl.sync_turn("u", "a")
    with pytest.raises(NotImplementedError):
        impl.reinforce("rec", "success")
    with pytest.raises(NotImplementedError, match="does not handle tool"):
        impl.handle_tool_call("unknown", {})


def test_default_indicators():
    assert RecallStatus().glyph == INDICATOR_GLYPH
    assert RecallStatus(injected=2, skipped=1).injected == 2


def test_core_memory_provider_sentinels():
    for name in ("", "default", "builtin", "built-in", "none", " DEFAULT "):
        assert is_core_memory_provider(name) is True, name
    for name in ("honcho", "custom-plugin", "x"):
        assert is_core_memory_provider(name) is False, name


def test_pre_compress_api_version_is_pinned():
    assert provider.PRE_COMPRESS_CHECKPOINT_API_VERSION == 2
    assert _minimal().pre_compress_checkpoint_api_version == 2
