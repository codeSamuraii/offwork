from __future__ import annotations

import ast
import importlib
import inspect
import re
import textwrap
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pyfuse._models import ImportInfo

if TYPE_CHECKING:
    from pyfuse._models import FunctionNode

_TRACE_PATTERN = re.compile(r"^\s*@trace\s*$")


def get_function_source(func: Callable[..., object]) -> str:
    """Get dedented source of func with @trace decorator lines stripped."""
    source = textwrap.dedent(inspect.getsource(func))
    lines = source.splitlines(keepends=True)
    return "".join(line for line in lines if not _TRACE_PATTERN.match(line))


def get_module_imports(func: Callable[..., object]) -> list[ImportInfo]:
    """Extract all top-level import bindings from the module where func is defined."""
    source_file = inspect.getfile(func)
    source_text = Path(source_file).read_text()
    tree = ast.parse(source_text)
    imports: list[ImportInfo] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                stmt = ast.unparse(ast.Import(names=[alias]))
                imports.append(ImportInfo(statement=stmt, bound_name=bound))

        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    if node.module is None:
                        warnings.warn(
                            "Relative star import 'from . import *' "
                            "is not supported",
                            stacklevel=2,
                        )
                        continue
                    try:
                        star_mod = importlib.import_module(node.module)
                        exported: list[str]
                        if hasattr(star_mod, "__all__"):
                            exported = list(star_mod.__all__)
                        else:
                            exported = [
                                n for n in dir(star_mod) if not n.startswith("_")
                            ]
                        for export_name in exported:
                            stmt = f"from {node.module} import {export_name}"
                            imports.append(
                                ImportInfo(statement=stmt, bound_name=export_name)
                            )
                    except ImportError:
                        warnings.warn(
                            f"Cannot resolve 'from {node.module} import *': "
                            "module not importable",
                            stacklevel=2,
                        )
                    continue
                bound = alias.asname or alias.name
                stmt = ast.unparse(
                    ast.ImportFrom(
                        module=node.module, names=[alias], level=node.level
                    )
                )
                imports.append(ImportInfo(statement=stmt, bound_name=bound))

    return imports


def get_used_names(func_source: str) -> set[str]:
    """Collect all Name identifiers referenced in the given source code."""
    tree = ast.parse(func_source)
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def filter_imports(
    all_imports: list[ImportInfo], used_names: set[str]
) -> list[ImportInfo]:
    """Keep only imports whose bound_name appears in used_names."""
    return [imp for imp in all_imports if imp.bound_name in used_names]


def _resolve_owner_class(qualname: str) -> str | None:
    """Extract the owning class name from a function's __qualname__, or None."""
    parts = qualname.rsplit(".", 1)
    if len(parts) == 1:
        return None
    prefix = parts[0]
    if "<locals>" in prefix:
        return None
    return prefix


def detect_traced_dependencies(
    func_source: str,
    func_module: str,
    registry: dict[str, FunctionNode],
    owner_class: str | None = None,
) -> list[str]:
    """Find qualified names of traced functions called in func_source."""
    tree = ast.parse(func_source)
    called_names: set[str] = set()
    self_method_calls: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("self", "cls")
            ):
                self_method_calls.add(node.func.attr)

    deps: list[str] = []
    for called in called_names:
        matches = [n for n in registry.values() if n.name == called]
        if not matches:
            continue
        same_module = [m for m in matches if m.module == func_module]
        chosen = same_module[0] if same_module else matches[0]
        deps.append(chosen.qualified_name)

    if owner_class and self_method_calls:
        for method_name in self_method_calls:
            matches = [
                n
                for n in registry.values()
                if n.name == method_name and n.owner_class == owner_class
            ]
            if not matches:
                continue
            same_module = [m for m in matches if m.module == func_module]
            chosen = same_module[0] if same_module else matches[0]
            if chosen.qualified_name not in deps:
                deps.append(chosen.qualified_name)

    return sorted(deps)
