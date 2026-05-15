"""Content-addressable store for serializing and reconstructing functions."""

import ast
import json
import logging
from typing import Any, Self
from graphlib import TopologicalSorter
from dataclasses import field, dataclass

from seeya.core.models import ImportInfo, FunctionNode
from seeya.core.version import _VERSION
from seeya.graph.analyzer import hoist_closure_vars, hoist_closure_func_refs

logger = logging.getLogger(__name__)


# -- Reconstruction helpers (used by Store.reconstruct) ----------------------


def _topological_order(nodes: dict[str, FunctionNode]) -> list[str]:
    """Return qualified names in dependency-first order."""
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for qname, node in nodes.items():
        sorter.add(qname, *[dep for dep in node.dependencies if dep in nodes])
    return list(sorter.static_order())


def _collect_imports(
    nodes: dict[str, FunctionNode], order: list[str]
) -> list[str]:
    """Deduplicate and sort import statements across all nodes."""
    seen: dict[str, None] = {}
    for qname in order:
        for imp in nodes[qname].imports:
            seen.setdefault(imp.statement, None)
    return sorted(seen)


def _collect_module_vars(
    nodes: dict[str, FunctionNode], order: list[str]
) -> list[str]:
    """Deduplicate module-level variable assignments across all nodes."""
    seen: dict[str, str] = {}
    func_and_class_names = {node.name for node in nodes.values()}
    for qname in order:
        for var_name, var_src in nodes[qname].module_vars.items():
            if var_name not in seen and var_name not in func_and_class_names:
                seen[var_name] = var_src
    return list(seen.values())


def _group_by_class(
    nodes: dict[str, FunctionNode], order: list[str]
) -> dict[str, list[str]]:
    """Group qualified names by owner_class, preserving topological order."""
    groups: dict[str, list[str]] = {}
    for qname in order:
        owner = nodes[qname].owner_class
        if owner is not None:
            groups.setdefault(owner, []).append(qname)
    return groups


def _apply_closure_transforms(
    node: FunctionNode, all_nodes: dict[str, FunctionNode]
) -> str:
    """Apply closure variable and function reference hoisting to source."""
    source = node.source
    if node.closure_vars:
        source = hoist_closure_vars(source, node.closure_vars)
    if node.closure_func_refs:
        source = hoist_closure_func_refs(source, node.closure_func_refs, all_nodes)
    return source


class _SuperRewriter(ast.NodeTransformer):
    """Replace ``super()`` with ``super(ClassName, self)`` or ``super(ClassName, cls)``."""

    def __init__(self, class_name: str) -> None:
        self._class_name = class_name
        self._first_param: str | None = None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        self._first_param = node.args.args[0].arg if node.args.args else "self"
        self.generic_visit(node)
        self._first_param = None
        return node

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> ast.Call:
        self.generic_visit(node)
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
            and not node.keywords
        ):
            node.args = [
                ast.Name(id=self._class_name, ctx=ast.Load()),
                ast.Name(id=self._first_param or "self", ctx=ast.Load()),
            ]
        return node


def _rewrite_bare_super(source: str, class_name: str) -> str:
    """Replace zero-arg ``super()`` with ``super(ClassName, self/cls)``."""
    if "super()" not in source:
        return source
    tree = ast.parse(source)
    tree = _SuperRewriter(class_name).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _indent_method(source: str) -> str:
    """Indent a method source for embedding inside a class block."""
    return "\n".join(
        ("    " + line if line.strip() else "")
        for line in source.rstrip().splitlines()
    )


def _build_class_block(
    owner_class: str,
    member_qnames: list[str],
    nodes: dict[str, FunctionNode],
) -> str:
    """Build a complete ``class ... :`` block from member nodes."""
    class_name = owner_class.rsplit(".", 1)[-1]

    method_sources = [
        _indent_method(_rewrite_bare_super(
            _apply_closure_transforms(nodes[qname], nodes), class_name,
        ))
        for qname in member_qnames
    ]

    bases: list[str] = []
    keywords: dict[str, str] = {}
    class_attrs: list[str] = []
    class_decorators: list[str] = []
    for qname in member_qnames:
        n = nodes[qname]
        if n.class_bases and not bases:
            bases = n.class_bases
        if n.class_keywords and not keywords:
            keywords = n.class_keywords
        if n.class_attrs and not class_attrs:
            class_attrs = n.class_attrs
        if n.class_decorators and not class_decorators:
            class_decorators = n.class_decorators

    header_parts = list(bases)
    for k, v in keywords.items():
        header_parts.append(f"{k}={v}")

    if header_parts:
        header = f"class {class_name}({', '.join(header_parts)}):\n"
    else:
        header = f"class {class_name}:\n"

    decorator_lines = "".join(f"@{d}\n" for d in class_decorators)
    attr_block = "".join(
        _indent_method(_rewrite_bare_super(attr, class_name)) + "\n\n"
        for attr in class_attrs
    )

    return decorator_lines + header + attr_block + "\n\n".join(method_sources)


@dataclass
class MergeResult:
    """Outcome of merging two stores."""

    added_objects: int = 0
    added_refs: int = 0
    conflicts: dict[str, tuple[str, str]] = field(default_factory=dict)


class Store:
    """Content-addressable store for function objects with separate topology.

    Separates three concerns:
    - **objects**: content-addressable blobs keyed by content hash
    - **deps**: dependency adjacency list (hash -> [dep hashes])
    - **refs**: named references (qualified name -> hash)
    """

    def __init__(self) -> None:
        self._objects: dict[str, dict[str, Any]] = {}
        self._deps: dict[str, list[str]] = {}
        self._refs: dict[str, str] = {}

    # -- Object operations ---------------------------------------------------

    def put(self, node: FunctionNode) -> str:
        """Store a node's content blob. Returns the content hash.

        Does NOT set deps -- call :meth:`set_deps` separately.
        """
        h = node.content_hash()
        if h not in self._objects:
            self._objects[h] = node.to_content_blob()
        return h

    def put_blob(self, h: str, blob: dict[str, Any]) -> None:
        """Store a pre-hashed content blob (used by deserialization)."""
        self._objects[h] = blob

    def get(self, h: str) -> dict[str, Any] | None:
        """Retrieve raw content blob by hash."""
        return self._objects.get(h)

    def has(self, h: str) -> bool:
        """Check whether a content hash exists in the store."""
        return h in self._objects

    @property
    def object_hashes(self) -> set[str]:
        """Set of all content hashes currently in the store."""
        return set(self._objects)

    # -- Dep operations ------------------------------------------------------

    def set_deps(self, h: str, dep_hashes: list[str]) -> None:
        """Set dependency edges for a content hash."""
        if dep_hashes:
            self._deps[h] = list(dep_hashes)
        else:
            self._deps.pop(h, None)

    def get_deps(self, h: str) -> list[str]:
        """Get dependency hashes for a content hash."""
        return list(self._deps.get(h, []))

    # -- Ref operations ------------------------------------------------------

    def set_ref(self, name: str, h: str) -> None:
        """Map a qualified name to a content hash."""
        self._refs[name] = h

    def get_ref(self, name: str) -> str | None:
        """Look up a content hash by qualified name."""
        return self._refs.get(name)

    def del_ref(self, name: str) -> None:
        """Remove a named reference."""
        self._refs.pop(name, None)

    @property
    def refs(self) -> dict[str, str]:
        """Snapshot of all named references (qualified name -> hash)."""
        return dict(self._refs)

    # -- Graph operations ----------------------------------------------------

    def walk(self, root_hash: str) -> list[str]:
        """BFS walk returning all reachable hashes from *root_hash*."""
        visited: dict[str, None] = {}
        stack = [root_hash]
        while stack:
            h = stack.pop()
            if h in visited:
                continue
            visited[h] = None
            stack.extend(self._deps.get(h, []))
        return list(visited)

    def subgraph(self, *root_hashes: str) -> "Store":
        """Extract transitive closure of *root_hashes* into a new store."""
        reachable: set[str] = set()
        for root in root_hashes:
            reachable.update(self.walk(root))

        store = Store()
        hash_to_qname = {h: qn for qn, h in self._refs.items()}
        for h in reachable:
            blob = self._objects.get(h)
            if blob is not None:
                store.put_blob(h, dict(blob))
            deps = self._deps.get(h)
            if deps:
                store.set_deps(h, [d for d in deps if d in reachable])
            qn = hash_to_qname.get(h)
            if qn is not None:
                store.set_ref(qn, h)
        return store

    def missing(self, hashes: set[str]) -> set[str]:
        """Return hashes from *hashes* not present in this store."""
        return hashes - self._objects.keys()

    def merge(self, other: "Store") -> MergeResult:
        """Merge *other* into this store.

        Objects are unioned by hash (same hash = same content).
        Deps are unioned per hash.
        Refs: existing refs are kept on conflict; conflicts are reported.
        """
        result = MergeResult()

        for h, blob in other._objects.items():
            if h not in self._objects:
                self._objects[h] = dict(blob)
                result.added_objects += 1

        for h, deps in other._deps.items():
            existing = self._deps.get(h)
            if existing is None:
                self._deps[h] = list(deps)
            else:
                merged = list(dict.fromkeys(existing + deps))
                self._deps[h] = merged

        for name, h in other._refs.items():
            existing_ref = self._refs.get(name)
            if existing_ref is None:
                self._refs[name] = h
                result.added_refs += 1
            elif existing_ref != h:
                result.conflicts[name] = (existing_ref, h)

        return result

    def gc(self) -> set[str]:
        """Remove objects unreachable from any ref. Returns removed hashes."""
        reachable: set[str] = set()
        for h in self._refs.values():
            reachable.update(self.walk(h))
        garbage = self._objects.keys() - reachable
        for h in garbage:
            del self._objects[h]
            self._deps.pop(h, None)
        return garbage

    # -- Reconstruction ------------------------------------------------------

    def collect(self, function_name: str) -> tuple[str, dict[str, FunctionNode]]:
        """Resolve *function_name*, walk deps, return (target_qname, nodes).

        Raises :class:`KeyError` if *function_name* is not found.
        """
        hash_to_qname = {h: qn for qn, h in self._refs.items()}
        target_hash = self._resolve_function_hash(function_name)
        reachable = self.walk(target_hash)

        needed: dict[str, FunctionNode] = {}
        for content_hash in reachable:
            blob = self._objects.get(content_hash)
            if blob is None:
                continue
            needed[hash_to_qname.get(content_hash, f"{blob['module']}.{blob['name']}")] = (
                self._blob_to_node(content_hash, blob, hash_to_qname)
            )

        target_qname = hash_to_qname.get(target_hash, f"unknown.{function_name}")
        return target_qname, needed

    def _blob_to_node(
        self,
        content_hash: str,
        blob: dict[str, Any],
        hash_to_qname: dict[str, str],
    ) -> FunctionNode:
        """Convert a raw content blob into a FunctionNode."""
        qname = hash_to_qname.get(content_hash, f"{blob['module']}.{blob['name']}")
        closure_func_refs = {
            var: hash_to_qname.get(ref_hash, ref_hash)
            for var, ref_hash in blob.get("closure_func_refs", {}).items()
        }
        dep_qnames = [
            hash_to_qname.get(dep_hash, dep_hash)
            for dep_hash in self._deps.get(content_hash, [])
        ]
        return FunctionNode(
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

    def reconstruct(self, function_name: str) -> str:
        """Reconstruct executable Python source for *function_name*."""
        target_qname, needed = self.collect(function_name)

        logger.debug(
            "Reconstructing %s: %d dependencies",
            target_qname, len(needed) - 1,
        )

        order = _topological_order(needed)
        logger.debug("Topological order: %s", order)

        import_lines = _collect_imports(needed, order)
        module_var_lines = _collect_module_vars(needed, order)
        class_groups = _group_by_class(needed, order)

        parts: list[str] = []
        if import_lines:
            parts.append("\n".join(import_lines))
        if module_var_lines:
            parts.append("\n".join(module_var_lines))

        emitted_classes: set[str] = set()
        for qname in order:
            node = needed[qname]
            source = _apply_closure_transforms(node, needed)
            if node.owner_class is None:
                parts.append(source.rstrip())
            elif node.owner_class not in emitted_classes:
                emitted_classes.add(node.owner_class)
                parts.append(_build_class_block(
                    node.owner_class, class_groups[node.owner_class], needed,
                ))

        return "\n\n\n".join(parts) + "\n"

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Export as a dict in v0.4.0 format."""
        qname_to_hash = self._refs

        # Build objects with closure_func_refs converted to hashes
        objects: dict[str, dict[str, Any]] = {}
        for h, blob in self._objects.items():
            out = dict(blob)
            if "closure_func_refs" in out:
                out["closure_func_refs"] = {
                    var: qname_to_hash.get(ref_qn, ref_qn)
                    for var, ref_qn in out["closure_func_refs"].items()
                }
            objects[h] = out

        return {
            "version": _VERSION,
            "objects": objects,
            "deps": {h: list(d) for h, d in self._deps.items()},
            "refs": dict(self._refs),
        }

    def to_json(self) -> str:
        """Serialize the store to a JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize a store from a dict (as produced by :meth:`to_dict`)."""
        store = cls()
        refs = data.get("refs", {})
        hash_to_qname = {h: qn for qn, h in refs.items()}

        for h, blob in data.get("objects", {}).items():
            out = dict(blob)
            # Convert closure_func_refs hashes back to qnames
            if "closure_func_refs" in out:
                out["closure_func_refs"] = {
                    var: hash_to_qname.get(ref_h, ref_h)
                    for var, ref_h in out["closure_func_refs"].items()
                }
            store.put_blob(h, out)

        for h, dep_list in data.get("deps", {}).items():
            store.set_deps(h, dep_list)

        for name, h in refs.items():
            store.set_ref(name, h)

        return store

    @classmethod
    def from_json(cls, json_str: str) -> Self:
        """Deserialize a store from a JSON string."""
        return cls.from_dict(json.loads(json_str))

    # -- Private helpers -----------------------------------------------------

    def _resolve_function_hash(self, function_name: str) -> str:
        """Resolve a function name (qualified or simple) to its hash."""
        if function_name in self._refs:
            return self._refs[function_name]
        for qn, h in self._refs.items():
            blob = self._objects.get(h)
            if blob and blob["name"] == function_name:
                return h
        raise KeyError(
            f"Function '{function_name}' not found in store"
        )

