import ast
import builtins
from dataclasses import dataclass, field
from typing import Any

from pyfuse.core.errors import SandboxViolationError

_DEFAULT_BLOCKED_BUILTINS = frozenset({
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "input",
    "open",
})

_DEFAULT_BLOCKED_MODULES = frozenset({
    "builtins",
    "code",
    "ctypes",
    "fcntl",
    "gc",
    "importlib",
    "mmap",
    "resource",
    "socket",
    "subprocess",
    "tracemalloc",
})

_DEFAULT_BLOCKED_CALLS: dict[str, frozenset[str]] = {
    "os": frozenset({
        "chmod",
        "chown",
        "fchmod",
        "fchown",
        "fork",
        "kill",
        "killpg",
        "link",
        "makedirs",
        "mkdir",
        "open",
        "popen",
        "posix_spawn",
        "remove",
        "removedirs",
        "rename",
        "replace",
        "rmdir",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "symlink",
        "system",
        "truncate",
        "unlink",
    }),
    "sys": frozenset({
        "addaudithook",
        "setdlopenflags",
        "setprofile",
        "setrecursionlimit",
        "setswitchinterval",
        "settrace",
    }),
}


def _default_safe_builtins() -> dict[str, Any]:
    safe = dict(builtins.__dict__)
    for name in _DEFAULT_BLOCKED_BUILTINS:
        safe.pop(name, None)
    return safe


@dataclass(frozen=True)
class ExecSandbox:
    """Best-effort in-process guardrails for reconstructed worker code.

    This is intentionally not a hard security boundary; it blocks a set of
    dangerous builtins, imports, and direct call targets before `exec`.
    """

    blocked_builtins: frozenset[str] = field(
        default_factory=lambda: _DEFAULT_BLOCKED_BUILTINS
    )
    blocked_modules: frozenset[str] = field(
        default_factory=lambda: _DEFAULT_BLOCKED_MODULES
    )
    blocked_calls: dict[str, frozenset[str]] = field(
        default_factory=lambda: dict(_DEFAULT_BLOCKED_CALLS)
    )

    def validate_source(self, source: str, function_name: str) -> None:
        tree = ast.parse(source, filename=f"<pyfuse:{function_name}>", mode="exec")
        _SandboxValidator(self).visit(tree)

    def build_namespace(self) -> dict[str, Any]:
        safe_builtins = _default_safe_builtins()
        for name in self.blocked_builtins:
            safe_builtins.pop(name, None)
        safe_builtins["__import__"] = self._guarded_import
        return {"__builtins__": safe_builtins}

    def _guarded_import(
        self,
        name: str,
        globals_dict: dict[str, Any] | None = None,
        locals_dict: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] | list[str] = (),
        level: int = 0,
    ) -> Any:
        top_level = name.split(".", 1)[0]
        if top_level in self.blocked_modules:
            raise SandboxViolationError(
                f"Sandbox blocked import of module '{top_level}'"
            )
        return builtins.__import__(name, globals_dict, locals_dict, fromlist, level)


DEFAULT_EXEC_SANDBOX = ExecSandbox()


class _SandboxValidator(ast.NodeVisitor):
    def __init__(self, sandbox: ExecSandbox) -> None:
        self._sandbox = sandbox

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._ensure_module_allowed(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is not None:
            self._ensure_module_allowed(node.module)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in self._sandbox.blocked_builtins:
            raise SandboxViolationError(
                f"Sandbox blocked builtin call '{node.func.id}()'"
            )

        call_target = self._resolve_call_target(node.func)
        if call_target is not None:
            root, _, attr = call_target.partition(".")
            blocked_attrs = self._sandbox.blocked_calls.get(root)
            if blocked_attrs is not None and attr in blocked_attrs:
                raise SandboxViolationError(
                    f"Sandbox blocked call '{call_target}()'"
                )

        self.generic_visit(node)

    def _ensure_module_allowed(self, module_name: str) -> None:
        top_level = module_name.split(".", 1)[0]
        if top_level in self._sandbox.blocked_modules:
            raise SandboxViolationError(
                f"Sandbox blocked import of module '{top_level}'"
            )

    def _resolve_call_target(self, node: ast.AST) -> str | None:
        parts: list[str] = []
        current: ast.AST | None = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
        return None
