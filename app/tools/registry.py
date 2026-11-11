"""The closed set of tools, and which of them a given person may see.

The registry is built once from the typed definitions in this package. A tool that is not in it cannot be named by a plan, an API
call or an assistant, so nothing can invent one.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.tools.contract import Scope, ToolError, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ToolError(f"{spec.name}: already registered")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def all(self) -> list[ToolSpec]:
        return [self._tools[n] for n in self.names()]

    def __len__(self) -> int:
        return len(self._tools)

    def visible_to(self, scopes: Iterable[Scope], permissions: Iterable[str]) -> list[ToolSpec]:
        """The tools a credential with these scopes may use for a person holding these role permissions: the tool's scope must be
        granted, the person's role must hold the tool's permission, and the tool must be switched on."""
        granted, held = set(scopes), set(permissions)
        return [
            t for t in self.all()
            if t.enabled and t.scope in granted and (t.permission is None or t.permission in held)
        ]

    def catalog(self, scopes: Iterable[Scope], permissions: Iterable[str]) -> list[dict]:
        return [t.catalog_entry() for t in self.visible_to(scopes, permissions)]


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    """Every tool, built on first use."""
    global _registry
    if _registry is None:
        registry = ToolRegistry()
        from app.tools import actions, catalog_account, catalog_audio, changes, compose, catalog_campaigns, catalog_images, catalog_library, catalog_video, catalog_publish, catalog_text, pictures, reads, videos

        for module in (reads, pictures, videos, actions, catalog_library, catalog_text, catalog_publish, catalog_audio, catalog_images, catalog_campaigns, catalog_video, catalog_account, changes, compose):
            for spec in module.specs():
                registry.register(spec)
        _registry = registry
    return _registry
