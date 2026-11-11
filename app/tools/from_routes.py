"""Build a tool's typed input, and find its permission, from the route it stands for.

A tool over an existing route should not retype that route's fields by hand: a typo would let the tool and the app disagree. So the
input is built from the route's own definition (its path ids, query options and request body), and the permission is read from the
route's own role check. The meaning (summary, risk, price, undo) is still written by a person in the catalog.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal, Optional

from fastapi.datastructures import UploadFile
from pydantic import BaseModel, ConfigDict, create_model
from pydantic_core import PydanticUndefined

from app.core.rbac import ROLE_PERMISSIONS

_ALL_PERMISSIONS = frozenset().union(*ROLE_PERMISSIONS.values())


class UnsupportedRoute(ValueError):
    """A route whose input is not plain typed data (a file upload, a stream). It needs a hand-made definition or an exemption."""


@lru_cache(maxsize=1)
def route_index() -> dict[str, Any]:
    """Every API route under /api/v1 by "METHOD /path"."""
    from fastapi.routing import APIRoute

    from app.main import app

    found: dict[str, Any] = {}

    def walk(routes, prefix: str) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                for method in route.methods - {"HEAD", "OPTIONS"}:
                    found[f"{method} {prefix}{route.path}"] = route
            elif hasattr(route, "original_router") and hasattr(route, "include_context"):
                walk(route.original_router.routes, prefix + (route.include_context.prefix or ""))

    walk(app.routes, "")
    return found


def route_permission(route_key: str) -> Optional[str]:
    """The role permission a route checks (`require("edit_content")`), or None when any member may call it."""
    route = route_index()[route_key]
    for dep in route.dependant.dependencies:
        for cell in getattr(dep.call, "__closure__", None) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, str) and value in _ALL_PERMISSIONS:
                return value
    return None


def _field(parameter) -> tuple[Any, Any]:
    info = parameter.field_info
    annotation = info.annotation if info.annotation is not None else str
    default = info.default
    if default is PydanticUndefined or getattr(parameter, "required", False):
        return annotation, ...
    return annotation, info


@lru_cache(maxsize=None)
def derive_input(route_key: str, model_name: str, confirm: bool = False) -> type[BaseModel]:
    """A typed input for the route: its path ids and query options, plus its request body when it has one."""
    route = route_index()[route_key]
    dependant = route.dependant
    extra: dict[str, Any] = {}
    for parameter in [*dependant.path_params, *dependant.query_params]:
        extra[parameter.name] = _field(parameter)
    body_model: Optional[type[BaseModel]] = None
    for parameter in dependant.body_params:
        annotation = parameter.field_info.annotation
        if annotation is UploadFile or getattr(annotation, "__name__", "") == "UploadFile":
            raise UnsupportedRoute(f"{route_key}: a file upload")
        if isinstance(annotation, type) and issubclass(annotation, BaseModel) and body_model is None and len(dependant.body_params) == 1:
            body_model = annotation
        else:
            extra[parameter.name] = _field(parameter)
    if confirm:
        extra["confirm"] = (Literal[True], ...)    # a missing or false confirm is refused
    if body_model is not None:
        clash = set(extra) & set(body_model.model_fields)
        if clash:
            raise UnsupportedRoute(f"{route_key}: path or query names clash with the body fields {sorted(clash)}")
        model = create_model(model_name, __base__=body_model, **extra)
    else:
        model = create_model(model_name, __config__=ConfigDict(extra="forbid"), **extra)
    return model
