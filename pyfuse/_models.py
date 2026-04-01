from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Self


@dataclass(frozen=True)
class ImportInfo:
    """A single import binding brought into scope by an import statement."""

    statement: str
    bound_name: str

    def to_dict(self) -> dict[str, str]:
        return {"statement": self.statement, "bound_name": self.bound_name}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Self:
        return cls(statement=data["statement"], bound_name=data["bound_name"])


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

    def to_dict(self) -> dict[str, Any]:
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
        return d

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
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
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
        )
