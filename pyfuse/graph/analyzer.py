"""AST-based source capture, import extraction, and dependency detection."""

import ast
import inspect
import logging
import textwrap
import warnings
import importlib
from pathlib import Path
from collections.abc import Callable

from pyfuse.core.models import ImportInfo, FunctionNode

logger = logging.getLogger(__name__)


def _is_trace_decorator(node: ast.expr) -> bool:
    """Return True if *node* is a ``@trace`` or ``@trace(...)`` decorator."""
    if isinstance(node, ast.Name) and node.id == "trace":
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "trace"
    ):
        return True
    return False


def get_function_source(func: Callable[..., object]) -> str:
    """Get dedented source of func with @trace decorator lines stripped."""
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    func_def = tree.body[0]
    if isinstance(func_def, (ast.FunctionDef, ast.AsyncFunctionDef)):
        lines_to_remove: set[int] = set()
        for decorator in func_def.decorator_list:
            if _is_trace_decorator(decorator):
                for line_no in range(decorator.lineno, (decorator.end_lineno or decorator.lineno) + 1):
                    lines_to_remove.add(line_no)
        if lines_to_remove:
            src_lines = source.splitlines(keepends=True)
            source = "".join(
                line for i, line in enumerate(src_lines, 1)
                if i not in lines_to_remove
            )
    logger.debug(
        "Extracted source for %s (%d lines)",
        func.__qualname__,
        source.count("\n"),
    )
    return source


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


def get_module_assignments(func: Callable[..., object]) -> dict[str, str]:
    """Extract top-level variable assignments from the module where func is defined.

    Returns a dict mapping variable name to its assignment source code.
    Skips dunder names, function/class definitions, and TYPE_CHECKING blocks.
    """
    source_file = inspect.getfile(func)
    source_text = Path(source_file).read_text()
    tree = ast.parse(source_text)
    assignments: dict[str, str] = {}

    for node in tree.body:
        # Simple assignment: x = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("__"):
                    assignments[target.id] = ast.get_source_segment(source_text, node) or ast.unparse(node)

        # Annotated assignment: x: int = ...
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name) and not node.target.id.startswith("__"):
                assignments[node.target.id] = ast.get_source_segment(source_text, node) or ast.unparse(node)

    logger.debug(
        "Found %d module-level assignments in %s",
        len(assignments),
        source_file,
    )
    return assignments


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


def has_super_call(func_source: str) -> bool:
    """Return True if the function source contains a ``super()`` call."""
    tree = ast.parse(func_source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
        ):
            return True
    return False


def get_class_bases_from_source(
    cls: type,
) -> tuple[list[str], dict[str, str]]:
    """Extract base class names and keyword arguments from the class definition.

    Returns ``(bases, keywords)`` where *bases* is a list of base class names
    (excluding ``object``) and *keywords* maps keyword names to their unparsed
    AST values (e.g. ``{"metaclass": "ABCMeta"}``).
    """
    try:
        source = textwrap.dedent(inspect.getsource(cls))
    except (OSError, TypeError):
        return [], {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
            bases = [ast.unparse(b) for b in node.bases if ast.unparse(b) != "object"]
            keywords: dict[str, str] = {}
            for kw in node.keywords:
                if kw.arg is not None:
                    keywords[kw.arg] = ast.unparse(kw.value)
            return bases, keywords
    return [], {}


def get_class_attrs(cls: type) -> tuple[list[str], list[str]]:
    """Extract class-level attributes and decorators from the class source AST.

    Returns ``(attrs, decorators)`` where *attrs* is a list of source code
    strings for class body statements (assignments, annotated assignments,
    docstrings) and *decorators* is a list of decorator source strings
    (without the ``@`` prefix).
    """
    try:
        source = textwrap.dedent(inspect.getsource(cls))
    except (OSError, TypeError):
        return [], []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], []
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == cls.__name__):
            continue
        attrs: list[str] = []
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            segment = ast.get_source_segment(source, child)
            if segment is not None:
                attrs.append(textwrap.dedent(segment))
            else:
                attrs.append(ast.unparse(child))
        decorators = [ast.unparse(d) for d in node.decorator_list]
        return attrs, decorators
    return [], []


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
    if "<locals>" not in prefix:
        return prefix
    # For nested classes like "outer.<locals>.MyClass.__init__",
    # extract the class name after the last "<locals>." segment.
    after_locals = prefix.rsplit("<locals>.", 1)[-1]
    # If there's still a class name (not empty, not another scope marker)
    if after_locals and "<" not in after_locals:
        return after_locals
    return None


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


def _prefer_same_module(
    matches: list[FunctionNode], func_module: str
) -> FunctionNode:
    """Pick a node from the same module when possible, otherwise first match."""
    same_module = [m for m in matches if m.module == func_module]
    return same_module[0] if same_module else matches[0]


def _classify_calls(
    tree: ast.Module,
) -> tuple[set[str], set[str], set[tuple[str, str]]]:
    """Walk the AST and classify calls into bare, self/cls, and obj.method."""
    bare_calls: set[str] = set()
    self_calls: set[str] = set()
    obj_method_calls: set[tuple[str, str]] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            bare_calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            root_var = _resolve_root_var(node.func.value)
            if root_var is None:
                continue
            if root_var in ("self", "cls"):
                self_calls.add(node.func.attr)
            else:
                obj_method_calls.add((root_var, node.func.attr))

    return bare_calls, self_calls, obj_method_calls


def _extract_param_types(tree: ast.Module) -> dict[str, set[str]]:
    """Extract parameter type annotation names from the first function def."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        param_types: dict[str, set[str]] = {}
        for arg in node.args.args + node.args.kwonlyargs:
            if arg.annotation is not None:
                param_types[arg.arg] = _extract_annotation_type_names(arg.annotation)
        if param_types:
            logger.debug("Type annotations: %s", {k: sorted(v) for k, v in param_types.items()})
        return param_types
    return {}


def _resolve_bare_calls(
    called_names: set[str],
    func_module: str,
    registry: dict[str, FunctionNode],
) -> list[str]:
    """Match bare function calls (``helper()``) against the registry."""
    deps: list[str] = []
    for called in called_names:
        matches = [node for node in registry.values() if node.name == called]
        if matches:
            chosen = _prefer_same_module(matches, func_module)
            logger.debug("Bare call %s() -> %s", called, chosen.qualified_name)
            deps.append(chosen.qualified_name)
            continue
        # Check for class constructor: ClassName() -> ClassName.__init__
        init_matches = [
            node for node in registry.values()
            if node.name == "__init__" and node.owner_class == called
        ]
        if init_matches:
            chosen = _prefer_same_module(init_matches, func_module)
            logger.debug("Constructor call %s() -> %s", called, chosen.qualified_name)
            deps.append(chosen.qualified_name)
    return deps


def _resolve_self_calls(
    self_calls: set[str],
    owner_class: str,
    func_module: str,
    registry: dict[str, FunctionNode],
    existing_deps: list[str],
) -> list[str]:
    """Match ``self.method()`` / ``cls.method()`` calls against the registry."""
    deps: list[str] = []
    for method_name in self_calls:
        matches = [
            node for node in registry.values()
            if node.name == method_name and node.owner_class == owner_class
        ]
        if not matches:
            continue
        chosen = _prefer_same_module(matches, func_module)
        if chosen.qualified_name not in existing_deps:
            logger.debug("self/cls call .%s() -> %s", method_name, chosen.qualified_name)
            deps.append(chosen.qualified_name)
    return deps


def _build_class_method_index(
    registry: dict[str, FunctionNode],
) -> dict[str, dict[str, str]]:
    """Build lookup: simple class name -> {method_name -> qualified_name}."""
    index: dict[str, dict[str, str]] = {}
    for node in registry.values():
        if node.owner_class is None:
            continue
        simple_class = node.owner_class.rsplit(".", 1)[-1]
        index.setdefault(simple_class, {})[node.name] = node.qualified_name
    return index


def _resolve_obj_method_calls(
    obj_method_calls: set[tuple[str, str]],
    param_types: dict[str, set[str]],
    registry: dict[str, FunctionNode],
    existing_deps: list[str],
) -> list[str]:
    """Match ``obj.method()`` calls via type annotations or unambiguous match."""
    class_methods = _build_class_method_index(registry)
    deps: list[str] = []

    for var_name, method_name in obj_method_calls:
        resolved = _resolve_single_obj_method(
            var_name, method_name, param_types, class_methods, registry,
        )
        if resolved is not None and resolved not in existing_deps:
            deps.append(resolved)

    return deps


def _resolve_single_obj_method(
    var_name: str,
    method_name: str,
    param_types: dict[str, set[str]],
    class_methods: dict[str, dict[str, str]],
    registry: dict[str, FunctionNode],
) -> str | None:
    """Resolve a single ``obj.method()`` call to a qualified name."""
    if var_name in param_types:
        for type_name in param_types[var_name]:
            methods = class_methods.get(type_name)
            if methods and method_name in methods:
                resolved = methods[method_name]
                logger.debug("%s.%s() resolved via annotation -> %s", var_name, method_name, resolved)
                return resolved
        return None

    candidates = [
        node.qualified_name for node in registry.values()
        if node.name == method_name and node.owner_class is not None
    ]
    if len(candidates) == 1:
        logger.debug("%s.%s() resolved via unambiguous match -> %s", var_name, method_name, candidates[0])
        return candidates[0]
    return None


def detect_traced_dependencies(
    func_source: str,
    func_module: str,
    registry: dict[str, FunctionNode],
    owner_class: str | None = None,
) -> list[str]:
    """Find qualified names of traced functions called in func_source."""
    tree = ast.parse(func_source)
    called_names, self_calls, obj_method_calls = _classify_calls(tree)
    param_types = _extract_param_types(tree)

    deps = _resolve_bare_calls(called_names, func_module, registry)

    if owner_class and self_calls:
        deps.extend(_resolve_self_calls(
            self_calls, owner_class, func_module, registry, deps,
        ))

    if obj_method_calls:
        deps.extend(_resolve_obj_method_calls(
            obj_method_calls, param_types, registry, deps,
        ))

    return sorted(deps)
