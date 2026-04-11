from __future__ import annotations

import ast
import contextvars
import inspect
import logging
import sys
import threading
import warnings
from collections.abc import Callable
from typing import Self

from pyfuse.core.errors import Error
from pyfuse.core.models import FunctionNode, ImportInfo
from pyfuse.graph.analyzer import (
    _resolve_owner_class,
    detect_traced_dependencies,
    filter_imports,
    find_bare_calls,
    find_self_calls,
    get_class_bases_from_source,
    get_function_source,
    get_module_assignments,
    get_module_imports,
    get_used_names,
    has_super_call,
)
from pyfuse.graph.store import Store
from pyfuse.graph.tracing import TracingMixin, _BUILTIN_NAMES, _is_user_class, _is_user_function

logger = logging.getLogger(__name__)


class Graph(TracingMixin):
    """Dependency graph of traced functions."""

    _default: Graph | None = None

    def __init__(self) -> None:
        self._nodes: dict[str, FunctionNode] = {}
        self._funcs: dict[str, Callable[..., object]] = {}
        self._call_stack: contextvars.ContextVar[list[str]] = contextvars.ContextVar(
            "pyfuse_call_stack"
        )
        self._runtime_deps: dict[str, set[str]] = {}
        self._lock: threading.Lock = threading.Lock()

    @classmethod
    def default(cls) -> Graph:
        if cls._default is None:
            cls._default = Graph()
        return cls._default

    @classmethod
    def reset_default(cls) -> None:
        cls._default = None

    @property
    def nodes(self) -> dict[str, FunctionNode]:
        return dict(self._nodes)

    def _auto_register_class(self, cls: type) -> None:
        """Auto-register all user-defined methods of a class into the graph."""
        class_name = cls.__name__
        module_name = cls.__module__

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

        # Detect class bases and propagate to registered methods
        self._resolve_class_bases(cls)

    def _resolve_class_bases(self, cls: type) -> None:
        """Detect class bases and store them on method nodes.

        Also auto-registers user-defined base classes and adds dependency
        edges from child methods (that use ``super()``) to parent methods.
        """
        class_name = cls.__name__
        module_name = cls.__module__

        bases = get_class_bases_from_source(cls)
        if not bases:
            return

        # Store bases on all method nodes of this class
        for qname, node in self._nodes.items():
            if node.owner_class == class_name and node.module == module_name:
                node.class_bases = bases

        # Auto-register user-defined base classes
        for base_cls in cls.__mro__[1:]:  # skip cls itself
            if base_cls is object:
                continue
            if _is_user_class(base_cls):
                self._auto_register_class(base_cls)

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
            source = get_function_source(func)
            all_imports = get_module_imports(func)
        except (OSError, TypeError, SyntaxError):
            warnings.warn(
                f"Cannot auto-register dependency '{func.__qualname__}': "
                "source code unavailable. The reconstructed code may be "
                "incomplete.",
                stacklevel=2,
            )
            return False

        used_names = get_used_names(source)
        owner_class = _resolve_owner_class(func.__qualname__)

        # Capture module-level variables referenced by this function
        try:
            all_assignments = get_module_assignments(func)
        except (OSError, TypeError):
            all_assignments = {}
        module_vars = {
            name: src
            for name, src in all_assignments.items()
            if name in used_names
        }
        # Include names used by module_vars in import filtering
        for var_src in module_vars.values():
            used_names |= get_used_names(var_src)

        imports = filter_imports(all_imports, used_names)
        # Remove module_var names from imports (they are assignments, not imports)
        imports = [imp for imp in imports if imp.bound_name not in module_vars]

        dependencies = [
            d for d in detect_traced_dependencies(
                source, func.__module__, self._nodes, owner_class=owner_class
            )
            if d != qualified_name
        ]

        node = FunctionNode(
            qualified_name=qualified_name,
            name=func.__name__,
            module=func.__module__,
            source=source,
            imports=imports,
            dependencies=dependencies,
            owner_class=owner_class,
            module_vars=module_vars,
        )
        self._nodes[qualified_name] = node
        self._funcs[qualified_name] = func
        logger.info("Auto-registered untraced dependency %s", qualified_name)

        # Recursively auto-discover this function's own untraced dependencies
        self._discover_untraced_deps(func.__module__, node)

        return True

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

        # Bare function calls (e.g. helper())
        bare_calls = find_bare_calls(node.source)
        imports_to_remove: list[ImportInfo] = []
        for name in bare_calls:
            if name in _BUILTIN_NAMES:
                continue
            obj = getattr(module_obj, name, None)
            if obj is None:
                continue
            # Class constructor: MyClass(...)
            if inspect.isclass(obj) and _is_user_class(obj):
                self._auto_register_class(obj)
                qualified = f"{obj.__module__}.{obj.__qualname__}"
                if obj.__module__ != module_name:
                    matching = [i for i in node.imports if i.bound_name == name]
                    imports_to_remove.extend(matching)
                continue
            if not inspect.isfunction(obj):
                continue
            if obj.__name__ != name:
                continue  # Skip aliased imports to avoid name mismatch
            self._auto_register(obj)
            qualified = f"{obj.__module__}.{obj.__qualname__}"
            if qualified in self._nodes and obj.__module__ != module_name:
                matching = [i for i in node.imports if i.bound_name == name]
                imports_to_remove.extend(matching)
        if imports_to_remove:
            node.imports = [i for i in node.imports if i not in imports_to_remove]

        # self.method() / cls.method() calls in class methods
        if node.owner_class:
            class_simple = node.owner_class.rsplit(".", 1)[-1]
            cls_obj = getattr(module_obj, class_simple, None)
            if cls_obj is None:
                return
            for method_name in find_self_calls(node.source):
                method_qname = f"{module_name}.{class_simple}.{method_name}"
                if method_qname in self._nodes:
                    continue
                # Access __dict__ to get raw descriptors (classmethod/staticmethod)
                raw = cls_obj.__dict__.get(method_name)
                if raw is not None and isinstance(raw, (classmethod, staticmethod)):
                    self._auto_register(raw.__func__)
                else:
                    method_obj = getattr(cls_obj, method_name, None)
                    if method_obj is not None and inspect.isfunction(method_obj):
                        self._auto_register(method_obj)
            # Resolve class bases (super() support, inheritance)
            if inspect.isclass(cls_obj):
                self._resolve_class_bases(cls_obj)

    def register(self, func: Callable[..., object]) -> None:
        # Unwrap if already traced
        original = func
        while hasattr(original, "__wrapped__"):
            original = original.__wrapped__

        qualified_name = f"{original.__module__}.{original.__qualname__}"
        logger.info("Registering %s", qualified_name)

        try:
            source = get_function_source(original)
            all_imports = get_module_imports(original)
        except (OSError, TypeError) as exc:
            logger.info(
                "Cannot register %s: source unavailable", qualified_name
            )
            raise Error(
                f"Cannot trace function '{original.__qualname__}': source code "
                "unavailable. Functions must be defined in .py source files."
            ) from exc

        used_names = get_used_names(source)
        owner_class = _resolve_owner_class(original.__qualname__)

        # Capture module-level variables referenced by this function
        try:
            all_assignments = get_module_assignments(original)
        except (OSError, TypeError):
            all_assignments = {}
        module_vars = {
            name: src
            for name, src in all_assignments.items()
            if name in used_names
        }
        # Include names used by module_vars in import filtering
        for var_src in module_vars.values():
            used_names |= get_used_names(var_src)

        imports = filter_imports(all_imports, used_names)
        # Remove module_var names from imports (they are assignments, not imports)
        imports = [imp for imp in imports if imp.bound_name not in module_vars]

        closure_vars: dict[str, str] = {}
        closure_func_refs: dict[str, str] = {}
        if original.__code__.co_freevars:
            try:
                cv = inspect.getclosurevars(original)
            except ValueError:
                # Empty cell — e.g. implicit __class__ from super()
                cv = None
            for name, value in (cv.nonlocals.items() if cv else ()):
                try:
                    repr_value = repr(value)
                except Exception:
                    warnings.warn(
                        f"Cannot repr closure variable '{name}' in "
                        f"'{original.__qualname__}'",
                        stacklevel=3,
                    )
                    continue

                try:
                    ast.parse(repr_value, mode="eval")
                    closure_vars[name] = repr_value
                except SyntaxError:
                    if getattr(value, "__pyfuse_traced__", False):
                        unwrapped = value
                        while hasattr(unwrapped, "__wrapped__"):
                            unwrapped = unwrapped.__wrapped__
                        ref_qname = (
                            f"{unwrapped.__module__}.{unwrapped.__qualname__}"
                        )
                        closure_func_refs[name] = ref_qname
                        logger.debug(
                            "Closure var '%s' is traced function %s",
                            name,
                            ref_qname,
                        )
                    else:
                        warnings.warn(
                            f"Closure variable '{name}' in "
                            f"'{original.__qualname__}' has repr that is not "
                            f"valid Python: {repr_value!r}",
                            stacklevel=3,
                        )

        dependencies = [
            d for d in detect_traced_dependencies(
                source, original.__module__, self._nodes, owner_class=owner_class
            )
            if d != qualified_name
        ]
        for ref_qname in closure_func_refs.values():
            if ref_qname != qualified_name and ref_qname not in dependencies:
                dependencies.append(ref_qname)

        node = FunctionNode(
            qualified_name=qualified_name,
            name=original.__name__,
            module=original.__module__,
            source=source,
            imports=imports,
            dependencies=dependencies,
            owner_class=owner_class,
            closure_vars=closure_vars,
            closure_func_refs=closure_func_refs,
            module_vars=module_vars,
        )
        self._nodes[qualified_name] = node
        self._funcs[qualified_name] = original

        logger.debug(
            "Registered %s: %d imports, %d deps, %d closure vars, "
            "%d closure func refs",
            qualified_name,
            len(imports),
            len(dependencies),
            len(closure_vars),
            len(closure_func_refs),
        )

        self.refresh()

    def refresh(self) -> None:
        """Re-analyze all registered functions to update dependencies."""
        for qname in list(self._nodes):
            node = self._nodes[qname]
            self._discover_untraced_deps(node.module, node)

        for qname in list(self._nodes):
            node = self._nodes[qname]
            deps = [
                d for d in detect_traced_dependencies(
                    node.source, node.module, self._nodes,
                    owner_class=node.owner_class,
                )
                if d != qname
            ]
            for ref_qname in node.closure_func_refs.values():
                if ref_qname != qname and ref_qname not in deps:
                    deps.append(ref_qname)
            node.dependencies = deps

    def _add_super_deps(self) -> None:
        """Add dependency edges from methods using super() to parent class methods."""
        for qname, node in self._nodes.items():
            if not node.owner_class or not node.class_bases:
                continue
            if not has_super_call(node.source):
                continue
            # Find parent class methods in the graph
            for pqn, pnode in self._nodes.items():
                if pnode.owner_class is None:
                    continue
                if pnode.owner_class in node.class_bases and pqn not in node.dependencies:
                    node.dependencies.append(pqn)
                    logger.debug("Super dep: %s -> %s", qname, pqn)

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

    def _resolve_name(self, name: str | Callable[..., object]) -> str:
        if callable(name) and not isinstance(name, str):
            unwrapped = inspect.unwrap(name)
            return f"{unwrapped.__module__}.{unwrapped.__qualname__}"
        for qname, node in self._nodes.items():
            if qname == name or node.name == name:
                return qname
        raise KeyError(f"Function '{name}' not found in graph")

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
            # Only add deps that point to known nodes
            new = {c for c in callees if c in self._nodes}
            new_edges = new - existing
            if new_edges:
                node.dependencies = sorted(existing | new)
                added += len(new_edges)
                for dep in sorted(new_edges):
                    logger.debug(
                        "Runtime dep: %s -> %s", caller_qname, dep
                    )
        if added:
            logger.info("Merged %d runtime dependency edges", added)

    def to_store(self, *funcs: Callable[..., object] | str) -> Store:
        """Build a :class:`Store` from this graph.

        Args:
            *funcs: If given, only include these functions and their
                transitive dependencies.  Otherwise the full graph.
        """
        # Re-run discovery now that all modules are fully loaded.
        # This catches deps that were missed at registration time
        # (e.g. self.method() calls in classes that hadn't been created yet).
        self.refresh()
        self._add_super_deps()
        self._merge_runtime_deps()

        if funcs:
            root_names = [self._resolve_name(f) for f in funcs]
            subgraph = self._collect_subgraph(root_names)
            logger.info(
                "Serializing subgraph: %d/%d nodes",
                len(subgraph),
                len(self._nodes),
            )
        else:
            subgraph = dict(self._nodes)
            logger.info("Serializing full graph: %d nodes", len(subgraph))

        store = Store()
        qname_to_hash: dict[str, str] = {}

        for qn, node in subgraph.items():
            h = store.put(node)
            qname_to_hash[qn] = h
            store.set_ref(qn, h)

        for qn, node in subgraph.items():
            dep_hashes = [
                qname_to_hash[d]
                for d in node.dependencies
                if d in qname_to_hash
            ]
            store.set_deps(qname_to_hash[qn], dep_hashes)

        return store

    def serialize(self, *funcs: Callable[..., object] | str) -> str:
        return self.to_store(*funcs).to_json()

    @classmethod
    def deserialize_graph(cls, json_str: str) -> Graph:
        store = Store.from_json(json_str)
        graph = cls()
        hash_to_qname = {h: qn for qn, h in store.refs.items()}

        for h, qn in hash_to_qname.items():
            blob = store.get(h)
            if blob is None:
                continue
            dep_qnames = [
                hash_to_qname.get(d, d) for d in store.get_deps(h)
            ]
            closure_func_refs = {
                var: hash_to_qname.get(ref_h, ref_h)
                for var, ref_h in blob.get("closure_func_refs", {}).items()
            }
            node = FunctionNode(
                qualified_name=qn,
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
            )
            graph._nodes[node.qualified_name] = node
        return graph

    @staticmethod
    def reconstruct(json_str: str, function_name: str) -> str:
        store = Store.from_json(json_str)
        return store.reconstruct(function_name)

    def to_mermaid(
        self,
        *funcs: Callable[..., object] | str,
        direction: str = "TD",
    ) -> str:
        """Render the dependency graph as a Mermaid flowchart.

        Args:
            *funcs: Optional functions/names to scope to a subgraph.
            direction: Graph direction ("TD", "LR", "BT", "RL").

        Returns:
            A Mermaid flowchart string.
        """
        self._merge_runtime_deps()

        if funcs:
            root_names = [self._resolve_name(f) for f in funcs]
            subgraph = self._collect_subgraph(root_names)
        else:
            subgraph = dict(self._nodes)

        def _node_id(qname: str) -> str:
            return qname.replace(".", "_")

        lines: list[str] = [f"graph {direction}"]

        # Group nodes by owner_class
        class_members: dict[str, list[FunctionNode]] = {}
        standalone: list[FunctionNode] = []
        for node in subgraph.values():
            if node.owner_class is not None:
                class_members.setdefault(node.owner_class, []).append(node)
            else:
                standalone.append(node)

        # Emit class subgraphs
        for owner_class, members in class_members.items():
            class_name = owner_class.rsplit(".", 1)[-1]
            lines.append(f"    subgraph {class_name}")
            for node in members:
                nid = _node_id(node.qualified_name)
                lines.append(f'        {nid}["{node.name}"]')
            lines.append("    end")

        # Emit standalone nodes
        for node in standalone:
            nid = _node_id(node.qualified_name)
            lines.append(f'    {nid}["{node.name}"]')

        # Emit edges
        for node in subgraph.values():
            src = _node_id(node.qualified_name)
            for dep in node.dependencies:
                if dep in subgraph:
                    lines.append(f"    {src} --> {_node_id(dep)}")

        return "\n".join(lines) + "\n"
