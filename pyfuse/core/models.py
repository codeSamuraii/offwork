"""Data models for function nodes and import bindings."""

import json
import hashlib
from typing import Any, Self
from dataclasses import field, dataclass


@dataclass(frozen=True)
class ImportInfo:
    """A single import binding brought into scope by an import statement."""

    statement: str
    bound_name: str
    package: str | None = None
    worker_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        d: dict[str, Any] = {"statement": self.statement, "bound_name": self.bound_name}
        if self.package is not None:
            d["package"] = self.package
        if self.worker_only:
            d["worker_only"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize from a plain dict.

        Raises
        ------
        KeyError
            If required fields (``statement``, ``bound_name``) are missing.
        """
        return cls(
            statement=data["statement"],
            bound_name=data["bound_name"],
            package=data.get("package"),
            worker_only=bool(data.get("worker_only", False)),
        )


@dataclass
class FunctionNode:
    """A node in the dependency graph representing one traced function."""

    qualified_name: str
    name: str
    module: str
    source: str
    imports: list[ImportInfo] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    owner_class: str | None = None
    closure_vars: dict[str, str] = field(default_factory=dict)
    closure_func_refs: dict[str, str] = field(default_factory=dict)
    module_vars: dict[str, str] = field(default_factory=dict)
    class_bases: list[str] = field(default_factory=list)
    class_keywords: dict[str, str] = field(default_factory=dict)
    class_attrs: list[str] = field(default_factory=list)
    class_decorators: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict including all fields."""
        d: dict[str, Any] = {
            "qualified_name": self.qualified_name,
            "name": self.name,
            "module": self.module,
            "source": self.source,
            "imports": [imp.to_dict() for imp in self.imports],
            "dependencies": list(self.dependencies),
            "owner_class": self.owner_class,
        }
        if self.closure_vars:
            d["closure_vars"] = dict(self.closure_vars)
        if self.closure_func_refs:
            d["closure_func_refs"] = dict(self.closure_func_refs)
        if self.module_vars:
            d["module_vars"] = dict(self.module_vars)
        if self.class_bases:
            d["class_bases"] = list(self.class_bases)
        if self.class_keywords:
            d["class_keywords"] = dict(self.class_keywords)
        if self.class_attrs:
            d["class_attrs"] = list(self.class_attrs)
        if self.class_decorators:
            d["class_decorators"] = list(self.class_decorators)
        return d

    def to_content_blob(self) -> dict[str, Any]:
        """Return only the fields stored in the content-addressable store.

        Excludes ``qualified_name`` and ``dependencies`` which are graph
        topology, not intrinsic content.
        """
        blob: dict[str, Any] = {
            "name": self.name,
            "module": self.module,
            "source": self.source,
            "imports": [imp.to_dict() for imp in self.imports],
            "owner_class": self.owner_class,
        }
        if self.closure_vars:
            blob["closure_vars"] = dict(self.closure_vars)
        if self.closure_func_refs:
            blob["closure_func_refs"] = dict(self.closure_func_refs)
        if self.module_vars:
            blob["module_vars"] = dict(self.module_vars)
        if self.class_bases:
            blob["class_bases"] = list(self.class_bases)
        if self.class_keywords:
            blob["class_keywords"] = dict(self.class_keywords)
        if self.class_attrs:
            blob["class_attrs"] = list(self.class_attrs)
        if self.class_decorators:
            blob["class_decorators"] = list(self.class_decorators)
        return blob

    def content_hash(self) -> str:
        """Compute a deterministic content hash for this node.

        The hash covers the node's own content but NOT its dependencies,
        so adding/removing edges doesn't invalidate existing hashes.
        """
        canonical = {
            "name": self.name,
            "module": self.module,
            "source": self.source,
            "imports": [
                imp.to_dict()
                for imp in sorted(self.imports, key=lambda i: i.statement)
            ],
            "owner_class": self.owner_class,
            "closure_vars": dict(sorted(self.closure_vars.items())),
            "closure_func_refs": dict(sorted(self.closure_func_refs.items())),
            "module_vars": dict(sorted(self.module_vars.items())),
            "class_bases": list(self.class_bases),
            "class_keywords": dict(sorted(self.class_keywords.items())),
            "class_attrs": list(self.class_attrs),
            "class_decorators": list(self.class_decorators),
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize from a plain dict.

        Raises
        ------
        KeyError
            If required fields (``qualified_name``, ``name``, ``module``,
            ``source``, ``imports``, ``dependencies``) are missing.
        """
        required = ("qualified_name", "name", "module", "source", "imports", "dependencies")
        missing = [k for k in required if k not in data]
        if missing:
            raise KeyError(f"FunctionNode missing required fields: {', '.join(missing)}")
        return cls(
            qualified_name=data["qualified_name"],
            name=data["name"],
            module=data["module"],
            source=data["source"],
            imports=[ImportInfo.from_dict(imp) for imp in data["imports"]],
            dependencies=data["dependencies"],
            owner_class=data.get("owner_class"),
            closure_vars=data.get("closure_vars", {}),
            closure_func_refs=data.get("closure_func_refs", {}),
            module_vars=data.get("module_vars", {}),
            class_bases=data.get("class_bases", []),
            class_keywords=data.get("class_keywords", {}),
            class_attrs=data.get("class_attrs", []),
            class_decorators=data.get("class_decorators", []),
        )
