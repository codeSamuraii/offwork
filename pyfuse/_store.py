from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from typing import Any

from pyfuse._analyzer import hoist_closure_func_refs, hoist_closure_vars
from pyfuse._models import FunctionNode, ImportInfo

logger = logging.getLogger(__name__)

_VERSION = "0.3.0"


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
        return h in self._objects

    @property
    def object_hashes(self) -> set[str]:
        return set(self._objects)

    # -- Dep operations ------------------------------------------------------

    def set_deps(self, h: str, dep_hashes: list[str]) -> None:
        if dep_hashes:
            self._deps[h] = list(dep_hashes)
        else:
            self._deps.pop(h, None)

    def get_deps(self, h: str) -> list[str]:
        return list(self._deps.get(h, []))

    # -- Ref operations ------------------------------------------------------

    def set_ref(self, name: str, h: str) -> None:
        self._refs[name] = h

    def get_ref(self, name: str) -> str | None:
        return self._refs.get(name)

    def del_ref(self, name: str) -> None:
        self._refs.pop(name, None)

    @property
    def refs(self) -> dict[str, str]:
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

    def subgraph(self, *root_hashes: str) -> Store:
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

    def merge(self, other: Store) -> MergeResult:
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
        for h in reachable:
            blob = self._objects.get(h)
            if blob is None:
                continue
            qn = hash_to_qname.get(h, f"{blob['module']}.{blob['name']}")
            closure_func_refs = {
                var: hash_to_qname.get(ref_h, ref_h)
                for var, ref_h in blob.get("closure_func_refs", {}).items()
            }
            dep_qnames = [
                hash_to_qname.get(d, d)
                for d in self._deps.get(h, [])
            ]
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
            )
            needed[qn] = node

        target_qname = hash_to_qname.get(
            target_hash, f"unknown.{function_name}"
        )
        return target_qname, needed

    def reconstruct(self, function_name: str) -> str:
        """Reconstruct executable Python source for *function_name*."""
        target_qname, needed = self.collect(function_name)

        logger.info(
            "Reconstructing %s: %d dependencies",
            target_qname,
            len(needed) - 1,
        )

        sorter: TopologicalSorter[str] = TopologicalSorter()
        for qn, node in needed.items():
            sorter.add(qn, *[d for d in node.dependencies if d in needed])
        order = list(sorter.static_order())
        logger.debug("Topological order: %s", order)

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
            source = node.source
            if node.closure_vars:
                source = hoist_closure_vars(source, node.closure_vars)
            if node.closure_func_refs:
                source = hoist_closure_func_refs(
                    source, node.closure_func_refs, needed
                )
            if node.owner_class is None:
                parts.append(source.rstrip())
            elif node.owner_class not in emitted_classes:
                emitted_classes.add(node.owner_class)
                class_name = node.owner_class.rsplit(".", 1)[-1]
                method_sources: list[str] = []
                for member_qn in class_groups[node.owner_class]:
                    member_node = needed[member_qn]
                    member_src = member_node.source
                    if member_node.closure_vars:
                        member_src = hoist_closure_vars(
                            member_src, member_node.closure_vars
                        )
                    if member_node.closure_func_refs:
                        member_src = hoist_closure_func_refs(
                            member_src, member_node.closure_func_refs, needed
                        )
                    member_src = member_src.rstrip()
                    indented = "\n".join(
                        ("    " + line if line.strip() else "")
                        for line in member_src.splitlines()
                    )
                    method_sources.append(indented)
                class_block = (
                    f"class {class_name}:\n"
                    + "\n\n".join(method_sources)
                )
                parts.append(class_block)

        return "\n\n\n".join(parts) + "\n"

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Export as a dict in v0.3.0 format."""
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
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Store:
        """Import from dict. Supports v0.2.0 and v0.3.0 formats."""
        store = cls()
        version = data.get("version", "0.2.0")
        refs = data.get("refs", {})
        hash_to_qname = {h: qn for qn, h in refs.items()}

        if version < "0.3.0":
            return cls._from_v020(data)

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
    def from_json(cls, json_str: str) -> Store:
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

    @classmethod
    def _from_v020(cls, data: dict[str, Any]) -> Store:
        """Import from v0.2.0 format (deps inside objects, hash field)."""
        store = cls()
        refs = data.get("refs", {})
        hash_to_qname = {h: qn for qn, h in refs.items()}

        for h, obj in data.get("objects", {}).items():
            blob: dict[str, Any] = {
                "name": obj["name"],
                "module": obj["module"],
                "source": obj["source"],
                "imports": obj["imports"],
                "owner_class": obj.get("owner_class"),
            }
            if obj.get("closure_vars"):
                blob["closure_vars"] = dict(obj["closure_vars"])
            if obj.get("closure_func_refs"):
                blob["closure_func_refs"] = {
                    var: hash_to_qname.get(ref_h, ref_h)
                    for var, ref_h in obj["closure_func_refs"].items()
                }
            store.put_blob(h, blob)

            deps = obj.get("deps", [])
            if deps:
                store.set_deps(h, deps)

        for name, h in refs.items():
            store.set_ref(name, h)

        return store


# Backward-compat alias
FuseStore = Store
