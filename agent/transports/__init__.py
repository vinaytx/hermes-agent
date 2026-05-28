"""Transport registry for agent provider adapters.

Each transport module self-registers at import time via register_transport().
get_transport() lazily imports the relevant module on first request so
callers don't need to manage imports themselves.
"""

from __future__ import annotations

import importlib
from typing import Dict, Optional, Type

# api_mode → ProviderTransport singleton instance
_REGISTRY: Dict[str, object] = {}

# Lazy-load map: api_mode → dotted module path
_LAZY_MODULES: Dict[str, str] = {
    "anthropic_messages": "agent.transports.anthropic",
    "chat_completions": "agent.transports.chat_completions",
    "codex_responses": "agent.transports.codex",
    "bedrock_converse": "agent.transports.bedrock",
}


def register_transport(name: str, cls: Type) -> None:
    """Register *cls* as the transport for *name* (stores a singleton instance)."""
    _REGISTRY[name] = cls()


def get_transport(mode: str) -> Optional[object]:
    """Return the ProviderTransport instance for *mode*, or None.

    Lazily imports the transport module if it has not been imported yet.
    """
    if mode in _REGISTRY:
        return _REGISTRY[mode]
    if mode in _LAZY_MODULES:
        try:
            importlib.import_module(_LAZY_MODULES[mode])
        except ImportError:
            pass
        return _REGISTRY.get(mode)
    return None
