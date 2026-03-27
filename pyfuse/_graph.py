from __future__ import annotations

import json
import warnings
from collections.abc import Callable
from graphlib import TopologicalSorter
from typing import Self

from pyfuse._analyzer import (
    _resolve_owner_class,
    detect_traced_dependencies,
    filter_imports,
    get_function_source,
    get_module_imports,
    get_used_names,
)
from pyfuse._errors import PyFuseError
from pyfuse._models import FunctionNode, ImportInfo

_VERSION = "0.1.0"


class FuseGraph:
    """Dependency graph of traced functions."""

    _default: FuseGraph | None = None

    def __init__(self) -> None:
        self._nodes: dict[str, FunctionNode] = {}
        self._funcs: dict[str, Callable[..., object]] = {}

    @classmethod
    def default(cls) -> FuseGraph:
        if cls._default is None:
            cls._default = FuseGraph()
        return cls._default

    @classmethod
    def reset_default(cls) -> None:
        cls._default = None

    @property
    def nodes(self) -> dict[str, FunctionNode]:
        return dict(self._nodes)

    def register(self, func: Callable[..., object]) -> None:
        qualified_name = f"{func.__module__}.{func.__qualname__}"

        try:
            source = get_function_source(func)
            all_imports = get_module_imports(func)
        except (OSError, TypeError) as exc:
            raise PyFuseError(
                f"Cannot trace function '{func.__qualname__}': source code "
                "unavailable. Functions must be defined in .py source files."
            ) from exc

        used_names = get_used_names(source)
        imports = filter_imports(all_imports, used_names)
        owner_class = _resolve_owner_class(func.__qualname__)

        if "<locals>" in func.__qualname__ and func.__code__.co_freevars:
            warnings.warn(
                f"Function '{func.__qualname__}' captures variables from "
                f"enclosing scope: {set(func.__code__.co_freevars)}. "
                "Reconstructed code may not be complete.",
                stacklevel=3,
            )

        dependencies = detect_traced_dependencies(
            source, func.__module__, self._nodes, owner_class=owner_class
        )
        node = FunctionNode(
            qualified_name=qualified_name,
            name=func.__name__,
            module=func.__module__,
            source=source,
            imports=imports,
            dependencies=dependencies,
            owner_class=owner_class,
        )
        self._nodes[qualified_name] = node
        self._funcs[qualified_name] = func
        self.refresh()

    def refresh(self) -> None:
        """Re-analyze all registered functions to update dependencies."""
        for qname, func in list(self._funcs.items()):
            node = self._nodes[qname]
            deps = detect_traced_dependencies(
                node.source, node.module, self._nodes,
                owner_class=node.owner_class,
            )
            self._nodes[qname] = FunctionNode(
                qualified_name=node.qualified_name,
                name=node.name,
                module=node.module,
                source=node.source,
                imports=node.imports,
                dependencies=deps,
                owner_class=node.owner_class,
            )

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
            return f"{name.__module__}.{name.__qualname__}"
        for qname, node in self._nodes.items():
            if qname == name or node.name == name:
                return qname
        raise KeyError(f"Function '{name}' not found in graph")

    def serialize(self, *funcs: Callable[..., object] | str) -> str:
        if funcs:
            root_names = [self._resolve_name(f) for f in funcs]
            subgraph = self._collect_subgraph(root_names)
        else:
            subgraph = dict(self._nodes)

        data = {
            "version": _VERSION,
            "nodes": {qn: node.to_dict() for qn, node in subgraph.items()},
        }
        return json.dumps(data, indent=2)

    @classmethod
    def deserialize_graph(cls, json_str: str) -> FuseGraph:
        data = json.loads(json_str)
        graph = cls()
        for node_data in data["nodes"].values():
            node = FunctionNode.from_dict(node_data)
            graph._nodes[node.qualified_name] = node
        return graph

    @staticmethod
    def reconstruct(json_str: str, function_name: str) -> str:
        data = json.loads(json_str)
        nodes = {qn: FunctionNode.from_dict(nd) for qn, nd in data["nodes"].items()}

        # Resolve function_name to qualified name
        target_qname: str | None = None
        for qname, node in nodes.items():
            if qname == function_name or node.name == function_name:
                target_qname = qname
                break
        if target_qname is None:
            raise KeyError(f"Function '{function_name}' not found in serialized graph")

        # Collect transitive dependencies
        needed: dict[str, FunctionNode] = {}
        stack = [target_qname]
        while stack:
            qn = stack.pop()
            if qn in needed:
                continue
            needed[qn] = nodes[qn]
            stack.extend(nodes[qn].dependencies)

        # Topological sort
        sorter: TopologicalSorter[str] = TopologicalSorter()
        for qn, node in needed.items():
            sorter.add(qn, *[d for d in node.dependencies if d in needed])
        order = list(sorter.static_order())

        # Collect and deduplicate imports
        seen_statements: dict[str, None] = {}
        for qn in order:
            for imp in needed[qn].imports:
                seen_statements.setdefault(imp.statement, None)
        import_lines = sorted(seen_statements.keys())

        # Group nodes by owner_class
        class_groups: dict[str, list[str]] = {}
        for qn in order:
            oc = needed[qn].owner_class
            if oc is not None:
                class_groups.setdefault(oc, []).append(qn)

        # Assemble script
        parts: list[str] = []
        if import_lines:
            parts.append("\n".join(import_lines))

        emitted_classes: set[str] = set()
        for qn in order:
            node = needed[qn]
            if node.owner_class is None:
                parts.append(node.source.rstrip())
            elif node.owner_class not in emitted_classes:
                emitted_classes.add(node.owner_class)
                class_name = node.owner_class.rsplit(".", 1)[-1]
                method_sources: list[str] = []
                for member_qn in class_groups[node.owner_class]:
                    member_src = needed[member_qn].source.rstrip()
                    indented = "\n".join(
                        ("    " + line if line.strip() else "")
                        for line in member_src.splitlines()
                    )
                    method_sources.append(indented)
                class_block = f"class {class_name}:\n" + "\n\n".join(method_sources)
                parts.append(class_block)

        return "\n\n\n".join(parts) + "\n"
