"""Plugins: third-party embedders, rerankers, extractors and storage backends.

A plugin is a Python package that declares an entry point in one of four
groups. Nothing else is needed; VectrixDB finds it by name.

    # in the plugin's pyproject.toml
    [project.entry-points."vectrixdb.embedders"]
    my-encoder = "my_package.encoders:MyEncoder"

    [project.entry-points."vectrixdb.storage"]
    my-store = "my_package.storage:MyStorage"

Then:

    Vectrix("docs", dense_model="plugin:my-encoder", dimension=768)
    StorageConfig(backend="plugin:my-store")

What each group must provide:

* ``vectrixdb.embedders``: a class or factory whose instance has
  ``embed(texts) -> np.ndarray`` (shape ``(n, dimension)``). A ``dimension``
  attribute saves passing one.
* ``vectrixdb.rerankers``: an instance with ``score(query, documents) ->
  list[float]``.
* ``vectrixdb.extractors``: an instance with ``extract(text) -> list`` of
  triplets, for the knowledge graph.
* ``vectrixdb.storage``: a subclass of ``vectrixdb.core.storage.BaseStorage``
  taking a ``StorageConfig``. The storage contract suite in the VectrixDB
  repository is the definition of done for one of these.

``load()`` returns the entry point's object (a class or factory); callers
instantiate it. ``available()`` lists what is installed, for error messages
and for a CLI to show. Registration in code, ``register()``, is for tests
and for programs that ship their own implementation without packaging it.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any, Callable, Dict, List

__all__ = ["GROUPS", "available", "load", "register", "PluginNotFound"]


# ============================================================================
# SETTINGS: the four groups, and the in-code registrations
# ============================================================================
#
# Embedders, rerankers, extractors and storage backends, and the registrations
# made in code beside the entry points.

GROUPS = ("embedders", "rerankers", "extractors", "storage")
_REGISTERED: Dict[str, Dict[str, Any]] = {group: {} for group in GROUPS}


# ============================================================================
# NOT FOUND
# ============================================================================
#
# INPUT   a name and a group
# OUTPUT  the error, with what is available
#
# So a typo is answered with the list.


class PluginNotFound(LookupError):
    """No plugin of that name in that group, with what is available."""


def _group_name(group: str) -> str:
    if group not in GROUPS:
        raise ValueError(f"group must be one of {GROUPS}, got {group!r}")
    return f"vectrixdb.{group}"


# ============================================================================
# AVAILABLE, LOAD, REGISTER, AND PARSE
# ============================================================================
#
# INPUT   a group; a name; an object; a plugin:name reference
# OUTPUT  the names installed, entry points and in-code registrations; the
#         class or factory behind a name, not an instance; a registration in
#         code, and the function that removes it; the name in a plugin:name
#         reference, or None
#
# A plugin is a package that declares an entry point in one of four groups.
# Nothing is imported until it is asked for by name.


def available(group: str) -> List[str]:
    """Names installed for a group, entry points and in-code registrations."""
    _group_name(group)
    names = set(_REGISTERED[group])
    try:
        eps: Any = metadata.entry_points()
        selected = (
            eps.select(group=_group_name(group))
            if hasattr(eps, "select")
            else eps.get(_group_name(group), [])
        )
        names.update(ep.name for ep in selected)
    except Exception:  # pragma: no cover - a broken distribution must not break us
        pass
    return sorted(names)


def load(group: str, name: str) -> Any:
    """The object behind ``name`` in ``group``: a class or factory, not an instance."""
    _group_name(group)
    if name in _REGISTERED[group]:
        return _REGISTERED[group][name]
    try:
        eps: Any = metadata.entry_points()
        selected = (
            eps.select(group=_group_name(group), name=name)
            if hasattr(eps, "select")
            else [ep for ep in eps.get(_group_name(group), []) if ep.name == name]
        )
    except Exception as exc:  # pragma: no cover
        raise PluginNotFound(f"could not read entry points: {exc}") from exc
    for ep in selected:
        return ep.load()
    have = available(group)
    hint = f" Installed: {', '.join(have)}." if have else " Nothing is installed for that group."
    raise PluginNotFound(f"no {group} plugin named {name!r}.{hint}")


def register(group: str, name: str, obj: Any) -> Callable[[], None]:
    """Register in code; returns a function that removes the registration."""
    _group_name(group)
    _REGISTERED[group][name] = obj

    def unregister() -> None:
        _REGISTERED[group].pop(name, None)

    return unregister


def parse_ref(value: Any) -> Any:
    """``"plugin:name"`` -> ``"name"``; anything else -> None."""
    if isinstance(value, str) and value.startswith("plugin:") and len(value) > 7:
        return value[7:]
    return None
