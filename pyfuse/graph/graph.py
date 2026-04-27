"""Dependency graph: function registration, auto-discovery, and serialization."""

import ast
import sys
import base64
import pickle
import inspect
import logging
import warnings
import threading
import collections
import contextvars
from typing import Any, Self
from dataclasses import dataclass
from collections.abc import Callable

from pyfuse.core.errors import Error
from pyfuse.core.models import ImportInfo, FunctionNode
from pyfuse.graph.store import Store
from pyfuse.graph.tracing import _BUILTIN_NAMES, TracingMixin, _is_user_class, _is_user_function
from pyfuse.graph.analyzer import (
    filter_imports,
    get_used_names,
    has_super_call,
    find_bare_calls,
    find_self_calls,
    get_class_attrs,
    get_module_imports,
    get_function_source,
    _resolve_owner_class,
    get_module_assignments,
    detect_traced_dependencies,
    get_class_bases_from_source,
)

logger = logging.getLogger(__name__)


@dataclass
class _AnalysisResult:
    """Result of static analysis of a function."""

    source: str
    imports: list[ImportInfo]
    owner_class: str | None
    module_vars: dict[str, str]


def _analyze_function(func: Callable[..., object]) -> _AnalysisResult:
    """Extract source, imports, and module-level vars for a function.

    Shared logic between :meth:`Graph.register` and
    :meth:`Graph._auto_register`.
    """
    source = get_function_source(func)
    all_imports = get_module_imports(func)
    used_names = get_used_names(source)
    owner_class = _resolve_owner_class(func.__qualname__)

    try:
        all_assignments = get_module_assignments(func)
    except (OSError, TypeError):
        all_assignments = {}
    module_vars = {
        name: src
        for name, src in all_assignments.items()
        if name in used_names
    }
    for var_src in module_vars.values():
        used_names |= get_used_names(var_src)

    imports = filter_imports(all_imports, used_names)
    imports = [imp for imp in imports if imp.bound_name not in module_vars]

    return _AnalysisResult(source, imports, owner_class, module_vars)


def _try_constructor_expr(value: object) -> str | None:
    """Try to produce a valid Python expression for common stdlib types."""
    if isinstance(value, collections.defaultdict):
        factory = value.default_factory
        if factory is None:
            factory_repr = "None"
        elif factory in (int, float, str, list, dict, set, tuple, bool, bytes):
            factory_repr = factory.__name__
        else:
            return None
        items_repr = repr(dict(value))
        return f"__import__('collections').defaultdict({factory_repr}, {items_repr})"
    if isinstance(value, collections.Counter):
        return f"__import__('collections').Counter({repr(dict(value))})"
    if isinstance(value, collections.deque):
        if value.maxlen is not None:
            return f"__import__('collections').deque({repr(list(value))}, maxlen={value.maxlen})"
        return f"__import__('collections').deque({repr(list(value))})"
    return None


def _try_pickle_fallback(value: object) -> str | None:
    """Try to serialize a value via pickle+base64 into a self-contained expression."""
    try:
        pickled = pickle.dumps(value)
        encoded = base64.b64encode(pickled).decode("ascii")
        expr = f"__import__('pickle').loads(__import__('base64').b64decode('{encoded}'))"
        ast.parse(expr, mode="eval")
        return expr
    except (pickle.PicklingError, TypeError, AttributeError, SyntaxError):
        return None


def _try_get_lambda_source(func: Callable[..., object]) -> str | None:
    """Try to extract the lambda expression source from a lambda function."""
    try:
        source = inspect.getsource(func).strip()
    except (OSError, TypeError):
        return None
    if "lambda" not in source:
        return None
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda):
            return ast.unparse(node)
    return None


def _capture_closure(
    func: Callable[..., object],
) -> tuple[dict[str, str], dict[str, str], dict[str, Callable[..., object]], list[ImportInfo], dict[str, type]]:
    """Extract closure variables and traced function references from *func*.

    Returns ``(closure_vars, closure_func_refs, closure_func_objects, closure_module_imports, closure_classes)``
    where *closure_vars* maps variable names to repr strings,
    *closure_func_refs* maps variable names to qualified names,
    *closure_func_objects* maps qualified names to the actual callable
    objects (for auto-registration of non-traced functions),
    *closure_module_imports* is a list of :class:`ImportInfo` for module
    objects found in the closure (from inline imports), and
    *closure_classes* maps variable names to user-defined class objects
    that need auto-registration.
    """
    closure_vars: dict[str, str] = {}
    closure_func_refs: dict[str, str] = {}
    closure_func_objects: dict[str, Callable[..., object]] = {}
    closure_module_imports: list[ImportInfo] = []
    closure_classes: dict[str, type] = {}

    if not func.__code__.co_freevars:
        return closure_vars, closure_func_refs, closure_func_objects, closure_module_imports, closure_classes

    try:
        closure_info = inspect.getclosurevars(func)
    except ValueError:
        return closure_vars, closure_func_refs, closure_func_objects, closure_module_imports, closure_classes

    for name, value in closure_info.nonlocals.items():
        # Skip the implicit __class__ cell injected by Python for super() calls.
        # Reconstructed code uses explicit super(ClassName, self) instead.
        if name == "__class__" and inspect.isclass(value):
            continue

        try:
            repr_value = repr(value)
        except Exception:
            warnings.warn(
                f"Cannot repr closure variable '{name}' in "
                f"'{func.__qualname__}'",
                stacklevel=3,
            )
            continue

        try:
            ast.parse(repr_value, mode="eval")
            closure_vars[name] = repr_value
            continue
        except SyntaxError:
            pass

        # Module objects from inline imports (e.g. `import time as _time`)
        if inspect.ismodule(value):
            mod_name = value.__name__
            if name == mod_name or name == mod_name.split(".")[0]:
                stmt = f"import {mod_name}"
            else:
                stmt = f"import {mod_name} as {name}"
            closure_module_imports.append(ImportInfo(statement=stmt, bound_name=name))
            logger.debug("Closure var '%s' is module %s", name, mod_name)
            continue

        # Callables: prefer source-level capture over serialization
        if getattr(value, "__pyfuse_traced__", False):
            unwrapped = value
            while hasattr(unwrapped, "__wrapped__"):
                unwrapped = unwrapped.__wrapped__
            ref_qname = f"{unwrapped.__module__}.{unwrapped.__qualname__}"
            closure_func_refs[name] = ref_qname
            logger.debug("Closure var '%s' is traced function %s", name, ref_qname)
            continue
        if callable(value) and getattr(value, "__name__", "") == "<lambda>":
            lambda_src = _try_get_lambda_source(value)
            if lambda_src is not None:
                closure_vars[name] = lambda_src
                logger.debug("Closure var '%s' is lambda: %s", name, lambda_src)
                continue
        if callable(value) and _is_user_function(value):
            ref_qname = f"{value.__module__}.{value.__qualname__}"
            closure_func_refs[name] = ref_qname
            closure_func_objects[ref_qname] = value
            logger.debug("Closure var '%s' is untraced user function %s", name, ref_qname)
            continue

        # User-defined classes: auto-register all their methods
        if inspect.isclass(value) and _is_user_class(value):
            closure_classes[name] = value
            logger.debug("Closure var '%s' is user class %s", name, value.__qualname__)
            continue

        # Non-callable fallbacks
        ctor_expr = _try_constructor_expr(value)
        if ctor_expr is not None:
            closure_vars[name] = ctor_expr
            logger.debug("Closure var '%s' captured via constructor expression", name)
            continue

        pickle_expr = _try_pickle_fallback(value)
        if pickle_expr is not None:
            closure_vars[name] = pickle_expr
            logger.debug("Closure var '%s' captured via pickle fallback", name)
            continue

        warnings.warn(
            f"Closure variable '{name}' in "
            f"'{func.__qualname__}' (type: {type(value).__name__}) "
            f"cannot be serialized: repr is not valid Python "
            f"and not picklable",
            stacklevel=3,
        )

    return closure_vars, closure_func_refs, closure_func_objects, closure_module_imports, closure_classes


def _mermaid_node_id(qname: str) -> str:
    return qname.replace(".", "_")


def _render_mermaid(subgraph: dict[str, FunctionNode], direction: str) -> str:
    """Render a node subgraph as a Mermaid flowchart string."""
    lines: list[str] = [f"graph {direction}"]

    class_members: dict[str, list[FunctionNode]] = {}
    standalone: list[FunctionNode] = []
    for node in subgraph.values():
        if node.owner_class is not None:
            class_members.setdefault(node.owner_class, []).append(node)
        else:
            standalone.append(node)

    for owner_class, members in class_members.items():
        class_name = owner_class.rsplit(".", 1)[-1]
        lines.append(f"    subgraph {class_name}")
        for node in members:
            nid = _mermaid_node_id(node.qualified_name)
            lines.append(f'        {nid}["{node.name}"]')
        lines.append("    end")

    for node in standalone:
        nid = _mermaid_node_id(node.qualified_name)
        lines.append(f'    {nid}["{node.name}"]')

    for node in subgraph.values():
        src = _mermaid_node_id(node.qualified_name)
        for dep in node.dependencies:
            if dep in subgraph:
                lines.append(f"    {src} --> {_mermaid_node_id(dep)}")

    return "\n".join(lines) + "\n"


class Graph(TracingMixin):
    """Dependency graph of traced functions."""

    _default: "Graph | None" = None

    def __init__(self) -> None:
        self._nodes: dict[str, FunctionNode] = {}
        self._funcs: dict[str, Callable[..., object]] = {}
        self._call_stack: contextvars.ContextVar[list[str]] = contextvars.ContextVar(
            "pyfuse_call_stack"
        )
        self._runtime_deps: dict[str, set[str]] = {}
        self._lock: threading.Lock = threading.Lock()
        self._classes_in_progress: set[str] = set()
        self._inclusion_deps: dict[str, set[str]] = {}

    @classmethod
    def default(cls) -> "Graph":
        """Return the singleton default graph used by ``@trace``."""
        if cls._default is None:
            cls._default = Graph()
        return cls._default

    @classmethod
    def reset_default(cls) -> None:
        """Reset the default graph, clearing all registered functions."""
        cls._default = None

    @property
    def nodes(self) -> dict[str, FunctionNode]:
        """Snapshot of all registered function nodes, keyed by qualified name."""
        return dict(self._nodes)

    # -- Registration ----------------------------------------------------------

    @staticmethod
    def _unwrap_func(func: Callable[..., object]) -> Callable[..., object]:
        original = func
        while hasattr(original, "__wrapped__"):
            original = original.__wrapped__
        return original

    def _build_dependencies(
        self,
        analysis: _AnalysisResult,
        qualified_name: str,
        module: str,
        closure_func_refs: dict[str, str],
    ) -> list[str]:
        """Detect static and closure-based dependencies for a function."""
        deps = [
            dep for dep in detect_traced_dependencies(
                analysis.source, module, self._nodes,
                owner_class=analysis.owner_class,
            )
            if dep != qualified_name
        ]
        for ref_qname in closure_func_refs.values():
            if ref_qname != qualified_name and ref_qname not in deps:
                deps.append(ref_qname)
        return deps

    def register(self, func: Callable[..., object]) -> None:
        """Register a function for tracing and remote execution."""
        original = self._unwrap_func(func)
        qualified_name = f"{original.__module__}.{original.__qualname__}"
        logger.info("Registering %s", qualified_name)

        try:
            analysis = _analyze_function(original)
        except (OSError, TypeError) as exc:
            logger.info("Cannot register %s: source unavailable", qualified_name)
            raise Error(
                f"Cannot trace function '{original.__qualname__}': source code "
                "unavailable. Functions must be defined in .py source files."
            ) from exc

        closure_vars, closure_func_refs, closure_func_objects, closure_module_imports, closure_classes = _capture_closure(original)

        for ref_qname, func_obj in closure_func_objects.items():
            if ref_qname not in self._nodes:
                self._auto_register(func_obj)

        for cls_obj in closure_classes.values():
            self._auto_register_class(cls_obj)

        # Add module imports from closures (inline imports like `import time as _time`)
        existing = {imp.bound_name for imp in analysis.imports}
        for imp in closure_module_imports:
            if imp.bound_name not in existing:
                analysis.imports.append(imp)
                existing.add(imp.bound_name)

        if closure_vars:
            closure_names: set[str] = set()
            for cv in closure_vars.values():
                closure_names |= get_used_names(cv)
            if closure_names:
                all_imports = get_module_imports(original)
                for imp in all_imports:
                    if imp.bound_name in closure_names and imp.bound_name not in existing:
                        analysis.imports.append(imp)
                        existing.add(imp.bound_name)

        dependencies = self._build_dependencies(
            analysis, qualified_name, original.__module__, closure_func_refs,
        )

        node = FunctionNode(
            qualified_name=qualified_name,
            name=original.__name__,
            module=original.__module__,
            source=analysis.source,
            imports=analysis.imports,
            dependencies=dependencies,
            owner_class=analysis.owner_class,
            closure_vars=closure_vars,
            closure_func_refs=closure_func_refs,
            module_vars=analysis.module_vars,
        )
        self._nodes[qualified_name] = node
        self._funcs[qualified_name] = original

        logger.debug(
            "Registered %s: %d imports, %d deps, %d closure vars, "
            "%d closure func refs",
            qualified_name, len(analysis.imports), len(dependencies),
            len(closure_vars), len(closure_func_refs),
        )

        self.refresh()

    # -- Auto-discovery --------------------------------------------------------

    def _auto_register(self, func: Callable[..., object]) -> bool:
        """Auto-register an untraced function into the graph.

        Returns False on failure and emits a warning so the user knows
        a dependency could not be captured.
        """
        qualified_name = f"{func.__module__}.{func.__qualname__}"
        if qualified_name in self._nodes:
            return False
        if not _is_user_function(func):
            return False

        try:
            analysis = _analyze_function(func)
        except (OSError, TypeError, SyntaxError):
            warnings.warn(
                f"Cannot auto-register dependency '{func.__qualname__}': "
                "source code unavailable. The reconstructed code may be "
                "incomplete.",
                stacklevel=2,
            )
            return False

        closure_vars, closure_func_refs, closure_func_objects, closure_module_imports, closure_classes = _capture_closure(func)

        # Add module imports from closures (inline imports)
        existing_names = {imp.bound_name for imp in analysis.imports}
        for imp in closure_module_imports:
            if imp.bound_name not in existing_names:
                analysis.imports.append(imp)
                existing_names.add(imp.bound_name)

        # Add imports needed by closure var expressions
        if closure_vars:
            closure_names: set[str] = set()
            for cv in closure_vars.values():
                closure_names |= get_used_names(cv)
            if closure_names:
                try:
                    all_imports = get_module_imports(func)
                    for imp in all_imports:
                        if imp.bound_name in closure_names and imp.bound_name not in existing_names:
                            analysis.imports.append(imp)
                            existing_names.add(imp.bound_name)
                except (OSError, TypeError):
                    pass

        dependencies = [
            dep for dep in detect_traced_dependencies(
                analysis.source, func.__module__, self._nodes,
                owner_class=analysis.owner_class,
            )
            if dep != qualified_name
        ]
        for ref_qname in closure_func_refs.values():
            if ref_qname != qualified_name and ref_qname not in dependencies:
                dependencies.append(ref_qname)

        node = FunctionNode(
            qualified_name=qualified_name,
            name=func.__name__,
            module=func.__module__,
            source=analysis.source,
            imports=analysis.imports,
            dependencies=dependencies,
            owner_class=analysis.owner_class,
            closure_vars=closure_vars,
            closure_func_refs=closure_func_refs,
            module_vars=analysis.module_vars,
        )
        self._nodes[qualified_name] = node
        self._funcs[qualified_name] = func
        logger.info("Auto-registered untraced dependency %s", qualified_name)

        # Auto-register closure function deps (after node is in self._nodes to prevent re-entry)
        for ref_qname, func_obj in closure_func_objects.items():
            if ref_qname not in self._nodes:
                self._auto_register(func_obj)

        for cls_obj in closure_classes.values():
            self._auto_register_class(cls_obj)

        self._discover_untraced_deps(func.__module__, node)
        return True

    def _auto_register_class(self, cls: type) -> None:
        """Auto-register all user-defined methods of a class into the graph."""
        class_name = cls.__name__
        module_name = cls.__module__
        cls_key = f"{module_name}.{cls.__qualname__}"
        if cls_key in self._classes_in_progress:
            return
        self._classes_in_progress.add(cls_key)
        try:
            for attr_name, raw in cls.__dict__.items():
                if isinstance(raw, (classmethod, staticmethod)):
                    func = raw.__func__
                elif inspect.isfunction(raw):
                    func = raw
                else:
                    continue
                if not _is_user_function(func):
                    continue
                qname = f"{module_name}.{class_name}.{attr_name}"
                if qname in self._nodes:
                    continue
                self._auto_register(func)

            self._set_class_metadata(cls)
            self._resolve_class_bases(cls)

            # Subclass registry pattern: classes that hook ``__init_subclass__``
            # populate registries from subclass definitions. The traced source
            # may look subclasses up indirectly (e.g. by name); to make that
            # work on the worker, pull every user-defined subclass into the
            # graph so its definition fires the parent hook on reconstruct.
            if "__init_subclass__" in cls.__dict__:
                for sub in cls.__subclasses__():
                    if _is_user_class(sub):
                        self._auto_register_class(sub)
        finally:
            self._classes_in_progress.discard(cls_key)

    def _set_class_metadata(self, cls: type) -> None:
        """Capture class-level attributes and decorators onto method nodes."""
        class_name = cls.__name__
        module_name = cls.__module__

        attrs, decorators = get_class_attrs(cls)
        if not attrs and not decorators:
            return

        extra_names: set[str] = set()
        for attr_src in attrs:
            extra_names |= get_used_names(attr_src)
        for deco_src in decorators:
            extra_names |= get_used_names(deco_src)

        # User classes referenced from class-body RHS (e.g. descriptors like
        # ``field = Doubler()``) are not visible to bare-call discovery on
        # function bodies; register them here so they survive reconstruction.
        ref_method_qnames = self._register_class_attr_refs(cls, extra_names)

        for node in self._nodes.values():
            if node.owner_class == class_name and node.module == module_name:
                node.class_attrs = attrs
                node.class_decorators = decorators
                for ref_qname in ref_method_qnames:
                    if ref_qname != node.qualified_name:
                        self._inclusion_deps.setdefault(node.qualified_name, set()).add(ref_qname)
                if extra_names:
                    existing_names = {imp.bound_name for imp in node.imports}
                    try:
                        any_func = next(
                            f for f in self._funcs.values()
                            if f.__module__ == module_name
                        )
                        all_imports = get_module_imports(any_func)
                        for imp in all_imports:
                            if imp.bound_name in extra_names and imp.bound_name not in existing_names:
                                node.imports.append(imp)
                                existing_names.add(imp.bound_name)
                    except StopIteration:
                        pass

    def _register_class_attr_refs(
        self, cls: type, extra_names: set[str]
    ) -> list[str]:
        """Auto-register user classes referenced from the class body.

        Returns the qualified names of one method per referenced class so
        callers can wire dependency edges that keep them in the subgraph.
        """
        if not extra_names:
            return []
        module_obj = sys.modules.get(cls.__module__)
        if module_obj is None:
            return []

        ref_method_qnames: list[str] = []
        for name in extra_names:
            if name in _BUILTIN_NAMES:
                continue
            obj = getattr(module_obj, name, None)
            if obj is None or obj is cls:
                continue
            if not (inspect.isclass(obj) and _is_user_class(obj)):
                continue
            self._auto_register_class(obj)
            for ref_node in self._nodes.values():
                if (
                    ref_node.owner_class == obj.__name__
                    and ref_node.module == obj.__module__
                ):
                    ref_method_qnames.append(ref_node.qualified_name)
        return ref_method_qnames

    def _resolve_class_bases(self, cls: type) -> None:
        """Detect class bases and store them on method nodes.

        Also auto-registers user-defined base classes and adds dependency
        edges from child methods (that use ``super()``) to parent methods.
        """
        class_name = cls.__name__
        module_name = cls.__module__

        bases, keywords = get_class_bases_from_source(cls)
        if not bases and not keywords:
            return

        keyword_names: set[str] = set()
        for v in keywords.values():
            keyword_names |= get_used_names(v)

        for node in self._nodes.values():
            if node.owner_class == class_name and node.module == module_name:
                node.class_bases = bases
                node.class_keywords = keywords
                if keyword_names:
                    existing_names = {imp.bound_name for imp in node.imports}
                    try:
                        any_func = next(
                            f for f in self._funcs.values()
                            if f.__module__ == module_name
                        )
                        all_imports = get_module_imports(any_func)
                        for imp in all_imports:
                            if imp.bound_name in keyword_names and imp.bound_name not in existing_names:
                                node.imports.append(imp)
                    except StopIteration:
                        pass

        for base_cls in cls.__mro__[1:]:
            if base_cls is object:
                continue
            if _is_user_class(base_cls):
                self._auto_register_class(base_cls)

        # Add ordering edges from child methods to every parent method per
        # direct user base, so the topological reconstruction emits parents
        # first when the subclass is included via the registry pattern
        # (without relying on ``super()`` being present in the subclass body).
        for base_cls in cls.__bases__:
            if base_cls is object or not _is_user_class(base_cls):
                continue
            parent_method_qnames = [
                parent_node.qualified_name
                for parent_node in self._nodes.values()
                if (
                    parent_node.owner_class == base_cls.__name__
                    and parent_node.module == base_cls.__module__
                )
            ]
            if not parent_method_qnames:
                continue
            for child_node in self._nodes.values():
                if (
                    child_node.owner_class != class_name
                    or child_node.module != module_name
                ):
                    continue
                bucket = self._inclusion_deps.setdefault(
                    child_node.qualified_name, set()
                )
                for parent_qname in parent_method_qnames:
                    if parent_qname != child_node.qualified_name:
                        bucket.add(parent_qname)

    def _discover_untraced_deps(
        self, module_name: str, node: FunctionNode
    ) -> None:
        """Find and auto-register untraced dependencies of a node."""
        module_obj = sys.modules.get(module_name)
        if module_obj is None:
            warnings.warn(
                f"Cannot auto-discover dependencies for "
                f"'{node.qualified_name}': module '{module_name}' not found "
                "in sys.modules.",
                stacklevel=2,
            )
            return

        self._discover_bare_call_deps(node, module_obj)
        if node.owner_class:
            self._discover_self_call_deps(node, module_obj, module_name)
        self._discover_init_subclass_deps(node, module_obj)

    def _discover_bare_call_deps(
        self,
        node: FunctionNode,
        module_obj: object,
    ) -> None:
        """Discover and auto-register bare function/class call dependencies."""
        bare_calls = find_bare_calls(node.source)
        imports_to_remove: list[ImportInfo] = []

        for name in bare_calls:
            if name in _BUILTIN_NAMES:
                continue
            obj = getattr(module_obj, name, None)
            if obj is None:
                continue
            if inspect.isclass(obj) and _is_user_class(obj):
                self._auto_register_class(obj)
                if obj.__module__ != node.module:
                    imports_to_remove.extend(
                        imp for imp in node.imports if imp.bound_name == name
                    )
                continue
            if not inspect.isfunction(obj):
                continue
            if obj.__name__ != name:
                continue  # Skip aliased imports to avoid name mismatch
            self._auto_register(obj)
            qualified = f"{obj.__module__}.{obj.__qualname__}"
            if qualified in self._nodes and obj.__module__ != node.module:
                imports_to_remove.extend(
                    imp for imp in node.imports if imp.bound_name == name
                )

        if imports_to_remove:
            node.imports = [imp for imp in node.imports if imp not in imports_to_remove]

    def _discover_self_call_deps(
        self,
        node: FunctionNode,
        module_obj: object,
        module_name: str,
    ) -> None:
        """Discover and auto-register self.method() / cls.method() dependencies."""
        assert node.owner_class is not None
        class_simple = node.owner_class.rsplit(".", 1)[-1]
        cls_obj = getattr(module_obj, class_simple, None)
        if cls_obj is None:
            return

        for method_name in find_self_calls(node.source):
            method_qname = f"{module_name}.{class_simple}.{method_name}"
            if method_qname in self._nodes:
                continue
            raw = cls_obj.__dict__.get(method_name)
            if raw is not None and isinstance(raw, (classmethod, staticmethod)):
                self._auto_register(raw.__func__)
            else:
                method_obj = getattr(cls_obj, method_name, None)
                if method_obj is not None and inspect.isfunction(method_obj):
                    self._auto_register(method_obj)

        if inspect.isclass(cls_obj):
            self._set_class_metadata(cls_obj)
            self._resolve_class_bases(cls_obj)

    def _discover_init_subclass_deps(
        self,
        node: FunctionNode,
        module_obj: object,
    ) -> None:
        """Pull subclasses of registry-style parents into the caller's deps.

        When the traced source references a class with a user-defined
        ``__init_subclass__``, its subclasses participate by being defined --
        not by being named in the source.  Add inclusion edges from this
        node to one method of each user subclass so the subgraph keeps them.
        """
        for name in find_bare_calls(node.source) | get_used_names(node.source):
            if name in _BUILTIN_NAMES:
                continue
            obj = getattr(module_obj, name, None)
            if obj is None or not inspect.isclass(obj):
                continue
            if "__init_subclass__" not in obj.__dict__:
                continue
            # Skip when this node is itself a method of obj or an ancestor;
            # adding child-class edges from a parent method causes cycles
            # via the parent ordering edges added in ``_resolve_class_bases``.
            if node.owner_class is not None:
                node_cls = getattr(module_obj, node.owner_class.rsplit(".", 1)[-1], None)
                if (
                    inspect.isclass(node_cls)
                    and node_cls is not None
                    and (node_cls is obj or issubclass(obj, node_cls))
                ):
                    continue
            for sub in obj.__subclasses__():
                if not _is_user_class(sub):
                    continue
                self._auto_register_class(sub)
                for sub_node in self._nodes.values():
                    if (
                        sub_node.owner_class == sub.__name__
                        and sub_node.module == sub.__module__
                        and sub_node.qualified_name != node.qualified_name
                    ):
                        self._inclusion_deps.setdefault(
                            node.qualified_name, set()
                        ).add(sub_node.qualified_name)

    # -- Refresh & dependency merging ------------------------------------------

    def refresh(self) -> None:
        """Re-analyze all registered functions to update dependencies."""
        for node in list(self._nodes.values()):
            self._discover_untraced_deps(node.module, node)

        for qname, node in self._nodes.items():
            deps = [
                dep for dep in detect_traced_dependencies(
                    node.source, node.module, self._nodes,
                    owner_class=node.owner_class,
                )
                if dep != qname
            ]
            for ref_qname in node.closure_func_refs.values():
                if ref_qname != qname and ref_qname not in deps:
                    deps.append(ref_qname)
            for incl_qname in self._inclusion_deps.get(qname, set()):
                if incl_qname in self._nodes and incl_qname not in deps:
                    deps.append(incl_qname)
            node.dependencies = deps

    def _add_super_deps(self) -> None:
        """Add dependency edges from methods using super() to parent class methods."""
        for qname, node in self._nodes.items():
            if not node.owner_class or not node.class_bases:
                continue
            if not has_super_call(node.source):
                continue
            for parent_qname, parent_node in self._nodes.items():
                if parent_node.owner_class is None:
                    continue
                if parent_node.owner_class in node.class_bases and parent_qname not in node.dependencies:
                    node.dependencies.append(parent_qname)
                    logger.debug("Super dep: %s -> %s", qname, parent_qname)

    def _merge_runtime_deps(self) -> None:
        """Merge runtime-discovered dependencies into node dependency lists."""
        with self._lock:
            pending = dict(self._runtime_deps)
        added = 0
        for caller_qname, callees in pending.items():
            node = self._nodes.get(caller_qname)
            if node is None:
                continue
            existing = set(node.dependencies)
            new_edges = {c for c in callees if c in self._nodes} - existing
            if not new_edges:
                continue
            node.dependencies = sorted(existing | new_edges)
            added += len(new_edges)
            for dep in sorted(new_edges):
                logger.debug("Runtime dep: %s -> %s", caller_qname, dep)
        if added:
            logger.info("Merged %d runtime dependency edges", added)

    # -- Serialization ---------------------------------------------------------

    def _resolve_name(self, name: str | Callable[..., object]) -> str:
        if callable(name) and not isinstance(name, str):
            unwrapped = inspect.unwrap(name)
            return f"{unwrapped.__module__}.{unwrapped.__qualname__}"
        for qname, node in self._nodes.items():
            if qname == name or node.name == name:
                return qname
        raise KeyError(f"Function '{name}' not found in graph")

    def _collect_subgraph(self, root_names: list[str]) -> dict[str, FunctionNode]:
        collected: dict[str, FunctionNode] = {}
        stack = list(root_names)
        while stack:
            qname = stack.pop()
            if qname in collected:
                continue
            node = self._nodes[qname]
            collected[qname] = node
            stack.extend(node.dependencies)
        return collected

    def to_store(self, *funcs: Callable[..., object] | str) -> Store:
        """Build a :class:`Store` from this graph.

        Args:
            *funcs: If given, only include these functions and their
                transitive dependencies.  Otherwise the full graph.
        """
        self.refresh()
        self._add_super_deps()
        self._merge_runtime_deps()

        if funcs:
            root_names = [self._resolve_name(f) for f in funcs]
            subgraph = self._collect_subgraph(root_names)
            logger.info(
                "Serializing subgraph: %d/%d nodes",
                len(subgraph), len(self._nodes),
            )
        else:
            subgraph = dict(self._nodes)
            logger.info("Serializing full graph: %d nodes", len(subgraph))

        store = Store()
        qname_to_hash: dict[str, str] = {}

        for qname, node in subgraph.items():
            content_hash = store.put(node)
            qname_to_hash[qname] = content_hash
            store.set_ref(qname, content_hash)

        for qname, node in subgraph.items():
            dep_hashes = [
                qname_to_hash[dep]
                for dep in node.dependencies
                if dep in qname_to_hash
            ]
            store.set_deps(qname_to_hash[qname], dep_hashes)

        return store

    def serialize(self, *funcs: Callable[..., object] | str) -> str:
        """Serialize the graph (or a subgraph) to a JSON string."""
        return self.to_store(*funcs).to_json()

    @classmethod
    def deserialize_graph(cls, json_str: str) -> Self:
        """Reconstruct a Graph from a serialized JSON string."""
        store = Store.from_json(json_str)
        graph = cls()
        hash_to_qname = {h: qn for qn, h in store.refs.items()}

        for content_hash, qname in hash_to_qname.items():
            blob = store.get(content_hash)
            if blob is None:
                continue
            dep_qnames = [
                hash_to_qname.get(dep, dep) for dep in store.get_deps(content_hash)
            ]
            closure_func_refs = {
                var: hash_to_qname.get(ref_h, ref_h)
                for var, ref_h in blob.get("closure_func_refs", {}).items()
            }
            node = FunctionNode(
                qualified_name=qname,
                name=blob["name"],
                module=blob["module"],
                source=blob["source"],
                imports=[ImportInfo.from_dict(imp) for imp in blob["imports"]],
                dependencies=dep_qnames,
                owner_class=blob.get("owner_class"),
                closure_vars=blob.get("closure_vars", {}),
                closure_func_refs=closure_func_refs,
                module_vars=blob.get("module_vars", {}),
                class_bases=blob.get("class_bases", []),
                class_keywords=blob.get("class_keywords", {}),
                class_attrs=blob.get("class_attrs", []),
                class_decorators=blob.get("class_decorators", []),
            )
            graph._nodes[node.qualified_name] = node
        return graph

    @staticmethod
    def reconstruct(json_str: str, function_name: str) -> str:
        """Reconstruct executable Python source from serialized JSON."""
        store = Store.from_json(json_str)
        return store.reconstruct(function_name)

    # -- Visualization ---------------------------------------------------------

    def to_mermaid(
        self,
        *funcs: Callable[..., object] | str,
        direction: str = "TD",
    ) -> str:
        """Render the dependency graph as a Mermaid flowchart."""
        self._merge_runtime_deps()

        if funcs:
            root_names = [self._resolve_name(f) for f in funcs]
            subgraph = self._collect_subgraph(root_names)
        else:
            subgraph = dict(self._nodes)

        return _render_mermaid(subgraph, direction)
