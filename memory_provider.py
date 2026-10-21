#!/usr/bin/env python3
"""
MemoryProvider — Abstract base class for pluggable memory providers.
Inspired by Hermes Agent (NousResearch/hermes-agent).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

PRE_COMPRESS_CHECKPOINT_API_VERSION = 2

INDICATOR_GLYPH = "🧠"

CORE_MEMORY_PROVIDER_SENTINELS = frozenset({"", "default", "builtin", "built-in", "none"})


def is_core_memory_provider(name: Optional[str]) -> bool:
    return str(name or "").strip().lower() in CORE_MEMORY_PROVIDER_SENTINELS


@dataclass(frozen=True)
class RecallStatus:
    injected: int = 0
    skipped: int = 0
    reason: Optional[str] = None
    glyph: str = INDICATOR_GLYPH


class MemoryProvider(ABC):
    pre_compress_checkpoint_api_version = 2

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider."""

    @abstractmethod
    def is_available(self) -> bool:
        """Configured, credentialed and ready? Gates activation."""

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize once at agent startup."""

    def unavailable_reason(self) -> str:
        return ""

    def system_prompt_block(self) -> str:
        """STATIC system-prompt text; "" to skip."""
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Formatted recall context for the upcoming turn."""
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        pass

    def recall_status(self) -> Optional[RecallStatus]:
        return None

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: list = None,
                  turn_author: dict = None) -> None:
        raise NotImplementedError

    def get_tool_schemas(self) -> list:
        return []

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        pass

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        pass

    def identity_signature(self) -> dict:
        return {}

    def on_session_end(self, messages: list) -> None:
        pass

    def on_session_switch(self, new_session_id: str, *,
                          parent_session_id: str = "",
                          reset: bool = False,
                          rewound: bool = False,
                          **kwargs) -> None:
        pass

    def on_pre_compress(self, messages: list) -> str:
        return ""

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "",
                      **kwargs) -> None:
        pass

    def get_config_schema(self) -> list:
        return []

    def save_config(self, values: dict, hermes_home: str) -> None:
        pass

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: dict = None) -> None:
        pass

    def backup_paths(self) -> list:
        return []

    def reinforce(self, record_id: str, signal: str, delta: float = 0.1,
                  source: str = "") -> float:
        raise NotImplementedError


if __name__ == "__main__":
    print("MemoryProvider ABC loaded")
    print(f"PRE_COMPRESS_CHECKPOINT_API_VERSION = {PRE_COMPRESS_CHECKPOINT_API_VERSION}")
    print(f"INDICATOR_GLYPH = {INDICATOR_GLYPH}")
