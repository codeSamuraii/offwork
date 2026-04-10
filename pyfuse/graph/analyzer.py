from __future__ import annotations

import ast
import importlib
import inspect
import logging
import re
import textwrap
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pyfuse.core.models import ImportInfo

if TYPE_CHECKING:
    from pyfuse.core.models import FunctionNode

logger = logging.getLogger(__name__)

_TRACE_PATTERN = re.compile(r"^\s*@trace\s*(\(.*\))?\s*$")


def get_function_source(func: Callable[..., object]) -> str:
    """Get dedented source of func with @trace decorator lines stripped."""
    source = textwrap.dedent(inspect.getsource(func))
    lines = source.splitlines(keepends=True)
    result = "".join(line for line in lines if not _TRACE_PATTERN.match(line))
    logger.debug(
        "Extracted source for %s (%d lines)", func.__qualname__, len(lines)
    )
    return result


def get_module_imports(func: Callable[..., object]) -> list[ImportInfo]:
    """Extract all top-level import bindings from the module where func is defined."""
    source_file = inspect.getfile(func)
    source_text = Path(source_file).read_text()
    tree = ast.parse(source_text)
    imports: list[ImportInfo] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(_extract_import(node))

        elif isinstance(node, ast.ImportFrom):
            imports.extend(_extract_import_from(node))

        elif isinstance(node, ast.With):
            package = _parse_install_package_as(node)
            if package is not None:
                for child in node.body:
                    if isinstance(child, ast.Import):
                        imports.extend(_extract_import(child, package))
                    elif isinstance(child, ast.ImportFrom):
                        imports.extend(_extract_import_from(child, package))

    logger.debug(
        "Found %d import bindings in %s", len(imports), source_file
    )
    return imports


def _parse_install_package_as(node: ast.With) -> str | None:
    """Return the package name if *node* is ``with install_package_as(...)``."""
    if len(node.items) != 1:
        return None
    ctx = node.items[0].context_expr
    if not (
        isinstance(ctx, ast.Call)
        and isinstance(ctx.func, ast.Name)
        and ctx.func.id == "install_package_as"
        and len(ctx.args) == 1
        and isinstance(ctx.args[0], ast.Constant)
        and isinstance(ctx.args[0].value, str)
    ):
        return None
    return ctx.args[0].value


def _extract_import(
    node: ast.Import, package: str | None = None
) -> list[ImportInfo]:
    result: list[ImportInfo] = []
    for alias in node.names:
        bound = alias.asname or alias.name.split(".")[0]
        stmt = ast.unparse(ast.Import(names=[alias]))
        result.append(ImportInfo(statement=stmt, bound_name=bound, package=package))
    return result


def _extract_import_from(
    node: ast.ImportFrom, package: str | None = None
) -> list[ImportInfo]:
    result: list[ImportInfo] = []
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
                logger.debug(
                    "Resolved 'from %s import *': %d names",
                    node.module,
                    len(exported),
                )
                for export_name in exported:
                    stmt = f"from {node.module} import {export_name}"
                    result.append(
                        ImportInfo(statement=stmt, bound_name=export_name, package=package)
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
        result.append(ImportInfo(statement=stmt, bound_name=bound, package=package))
    return result


def get_used_names(func_source: str) -> set[str]:
    """Collect all Name identifiers referenced in the given source code."""
    tree = ast.parse(func_source)
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def find_bare_calls(func_source: str) -> set[str]:
    """Return all bare function call names from the source AST."""
    tree = ast.parse(func_source)
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def find_self_calls(func_source: str) -> set[str]:
    """Return method names from ``self.method()`` / ``cls.method()`` calls."""
    tree = ast.parse(func_source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ("self", "cls")
        ):
            names.add(node.func.attr)
    return names


def filter_imports(
    all_imports: list[ImportInfo], used_names: set[str]
) -> list[ImportInfo]:
    """Keep only imports whose bound_name appears in used_names."""
    result = [imp for imp in all_imports if imp.bound_name in used_names]
    logger.debug(
        "Filtered imports: %d/%d retained", len(result), len(all_imports)
    )
    return result


def hoist_closure_vars(source: str, closure_vars: dict[str, str]) -> str:
    """Add closure vars as keyword-only params with default values."""
    if not closure_vars:
        return source
    tree = ast.parse(source)
    func_def = tree.body[0]
    assert isinstance(func_def, (ast.FunctionDef, ast.AsyncFunctionDef))
    for name, repr_value in closure_vars.items():
        func_def.args.kwonlyargs.append(ast.arg(arg=name))
        func_def.args.kw_defaults.append(
            ast.parse(repr_value, mode="eval").body
        )
    return ast.unparse(tree)


def hoist_closure_func_refs(
    source: str,
    closure_func_refs: dict[str, str],
    nodes: dict[str, FunctionNode],
) -> str:
    """Add closure func refs as keyword-only params defaulting to the function name."""
    if not closure_func_refs:
        return source
    tree = ast.parse(source)
    func_def = tree.body[0]
    assert isinstance(func_def, (ast.FunctionDef, ast.AsyncFunctionDef))
    for var_name, qname in closure_func_refs.items():
        func_name = nodes[qname].name if qname in nodes else qname.rsplit(".", 1)[-1]
        func_def.args.kwonlyargs.append(ast.arg(arg=var_name))
        func_def.args.kw_defaults.append(
            ast.Name(id=func_name, ctx=ast.Load())
        )
    return ast.unparse(tree)


def _resolve_owner_class(qualname: str) -> str | None:
    """Extract the owning class name from a function's __qualname__, or None."""
    parts = qualname.rsplit(".", 1)
    if len(parts) == 1:
        return None
    prefix = parts[0]
    if "<locals>" in prefix:
        return None
    return prefix


def _extract_annotation_type_names(annotation: ast.expr) -> set[str]:
    """Extract potential class names from a type annotation AST node."""
    names: set[str] = set()
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _resolve_root_var(node: ast.expr) -> str | None:
    """Walk up through subscripts/attributes to find the root variable name."""
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, (ast.Subscript, ast.Attribute)):
            node = node.value
        else:
            return None


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
    obj_method_calls: set[tuple[str, str]] = set()

    # Extract parameter type annotations
    param_types: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.annotation is not None:
                    param_types[arg.arg] = _extract_annotation_type_names(
                        arg.annotation
                    )
            break
    if param_types:
        logger.debug(
            "Type annotations: %s",
            {k: sorted(v) for k, v in param_types.items()},
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            root_var = _resolve_root_var(node.func.value)
            if root_var is not None:
                if root_var in ("self", "cls"):
                    self_method_calls.add(node.func.attr)
                else:
                    obj_method_calls.add((root_var, node.func.attr))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)

    deps: list[str] = []
    for called in called_names:
        matches = [n for n in registry.values() if n.name == called]
        if not matches:
            continue
        same_module = [m for m in matches if m.module == func_module]
        chosen = same_module[0] if same_module else matches[0]
        logger.debug("Bare call %s() -> %s", called, chosen.qualified_name)
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
                logger.debug(
                    "self/cls call .%s() -> %s",
                    method_name,
                    chosen.qualified_name,
                )
                deps.append(chosen.qualified_name)

    # Resolve obj.method() calls via type annotations or unambiguous match
    if obj_method_calls:
        # Build lookup: simple class name -> {method_name -> qualified_name}
        class_methods: dict[str, dict[str, str]] = {}
        for n in registry.values():
            if n.owner_class is not None:
                simple_class = n.owner_class.rsplit(".", 1)[-1]
                class_methods.setdefault(simple_class, {})[n.name] = (
                    n.qualified_name
                )

        for var_name, method_name in obj_method_calls:
            resolved: str | None = None

            if var_name in param_types:
                # Type annotation present -- use it to resolve
                type_names = param_types[var_name]
                for type_name in type_names:
                    methods = class_methods.get(type_name)
                    if methods and method_name in methods:
                        resolved = methods[method_name]
                        logger.debug(
                            "%s.%s() resolved via annotation -> %s",
                            var_name,
                            method_name,
                            resolved,
                        )
                        break
            else:
                # No annotation -- fallback to unambiguous match
                candidates = [
                    n.qualified_name
                    for n in registry.values()
                    if n.name == method_name and n.owner_class is not None
                ]
                if len(candidates) == 1:
                    resolved = candidates[0]
                    logger.debug(
                        "%s.%s() resolved via unambiguous match -> %s",
                        var_name,
                        method_name,
                        resolved,
                    )

            if resolved is not None and resolved not in deps:
                deps.append(resolved)

    return sorted(deps)
