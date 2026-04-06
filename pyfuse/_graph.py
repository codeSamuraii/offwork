from __future__ import annotations

import ast
import builtins
import contextvars
import functools
import inspect
import logging
import os
import sys
import sysconfig
import threading
import warnings
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path
from typing import Self, TypeVar

logger = logging.getLogger(__name__)

from pyfuse._analyzer import (
    _resolve_owner_class,
    detect_traced_dependencies,
    filter_imports,
    find_bare_calls,
    get_function_source,
    get_module_imports,
    get_used_names,
)
from pyfuse._errors import PyFuseError
from pyfuse._models import FunctionNode, ImportInfo

_F = TypeVar("_F", bound=Callable[..., object])


def _make_run_method(
    wrapper: Callable[..., object], func: Callable[..., object]
) -> Callable[..., object]:
    """Create the ``.run()`` method attached to a traced wrapper."""

    def run(*args: object, **kwargs: object) -> object:
        from pyfuse._remote import submit_remote

        return submit_remote(func, wrapper, *args, **kwargs)

    return run


from pyfuse._store import FuseStore

_VERSION = "0.3.0"
_BUILTIN_NAMES = set(dir(builtins))

def _get_stdlib_dirs() -> list[str]:
    dirs: list[str] = []
    for key in ("stdlib", "platstdlib"):
        val = sysconfig.get_path(key)
        if val:
            dirs.append(str(Path(val).resolve()))
    return dirs


_STDLIB_DIRS = _get_stdlib_dirs()


def _is_user_function(func: Callable[..., object]) -> bool:
    """Return True if func is user-defined (not stdlib or third-party)."""
    if not inspect.isfunction(func):
        return False
    top_module = func.__module__.split(".")[0]
    if hasattr(sys, "stdlib_module_names") and top_module in sys.stdlib_module_names:
        return False
    try:
        source_file = inspect.getfile(func)
    except (TypeError, OSError):
        return False
    resolved = str(Path(source_file).resolve())
    for stdlib_dir in _STDLIB_DIRS:
        if resolved.startswith(stdlib_dir):
            return False
    # Heuristic: anything under a site-packages directory is third-party
    if f"{os.sep}site-packages{os.sep}" in resolved:
        return False
    return True


class FuseGraph:
    """Dependency graph of traced functions."""

    _default: FuseGraph | None = None

    def __init__(self) -> None:
        self._nodes: dict[str, FunctionNode] = {}
        self._funcs: dict[str, Callable[..., object]] = {}
        self._call_stack: contextvars.ContextVar[list[str]] = contextvars.ContextVar(
            "pyfuse_call_stack"
        )
        self._runtime_deps: dict[str, set[str]] = {}
        self._lock: threading.Lock = threading.Lock()

    @classmethod
    def default(cls) -> FuseGraph:
        if cls._default is None:
            cls._default = FuseGraph()
        return cls._default

    @classmethod
    def reset_default(cls) -> None:
        cls._default = None

    def _get_call_stack(self) -> list[str]:
        try:
            return self._call_stack.get()
        except LookupError:
            stack: list[str] = []
            self._call_stack.set(stack)
            return stack

    def _ensure_isolated_stack(self) -> list[str]:
        """Return a call stack isolated for the current async context.

        ContextVar copies the reference to the list when a new Task is created,
        so async wrappers must call this to get a fresh copy, preventing
        mutations from leaking across tasks.
        """
        stack = list(self._get_call_stack())
        self._call_stack.set(stack)
        return stack

    def _record_edge(self, stack: list[str], qualified_name: str) -> None:
        """Record a runtime dependency edge if a caller is on the stack."""
        if stack and stack[-1] != qualified_name:
            with self._lock:
                self._runtime_deps.setdefault(stack[-1], set()).add(qualified_name)

    def create_wrapper(self, func: _F) -> _F:
        """Wrap func to record runtime caller-callee edges."""
        qualified_name = f"{func.__module__}.{func.__qualname__}"

        if inspect.isasyncgenfunction(func):
            logger.debug("Creating async generator wrapper for %s", qualified_name)

            @functools.wraps(func)
            def async_gen_wrapper(*args: object, **kwargs: object) -> object:
                self._record_edge(self._get_call_stack(), qualified_name)
                async_gen = func(*args, **kwargs)
                return self._proxy_async_generator(async_gen, qualified_name)

            async_gen_wrapper.__pyfuse_traced__ = True  # type: ignore[attr-defined]
            async_gen_wrapper.run = _make_run_method(async_gen_wrapper, func)  # type: ignore[attr-defined]
            return async_gen_wrapper  # type: ignore[return-value]

        if inspect.iscoroutinefunction(func):
            logger.debug("Creating async wrapper for %s", qualified_name)

            @functools.wraps(func)
            async def async_wrapper(*args: object, **kwargs: object) -> object:
                stack = self._ensure_isolated_stack()
                self._record_edge(stack, qualified_name)
                stack.append(qualified_name)
                try:
                    return await func(*args, **kwargs)
                finally:
                    stack.pop()

            async_wrapper.__pyfuse_traced__ = True  # type: ignore[attr-defined]
            async_wrapper.run = _make_run_method(async_wrapper, func)  # type: ignore[attr-defined]
            return async_wrapper  # type: ignore[return-value]

        if inspect.isgeneratorfunction(func):
            logger.debug("Creating generator wrapper for %s", qualified_name)

            @functools.wraps(func)
            def gen_wrapper(*args: object, **kwargs: object) -> object:
                self._record_edge(self._get_call_stack(), qualified_name)
                gen = func(*args, **kwargs)
                return self._proxy_generator(gen, qualified_name)

            gen_wrapper.__pyfuse_traced__ = True  # type: ignore[attr-defined]
            gen_wrapper.run = _make_run_method(gen_wrapper, func)  # type: ignore[attr-defined]
            return gen_wrapper  # type: ignore[return-value]

        logger.debug("Creating wrapper for %s", qualified_name)

        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            stack = self._get_call_stack()
            self._record_edge(stack, qualified_name)
            stack.append(qualified_name)
            try:
                return func(*args, **kwargs)
            finally:
                stack.pop()

        wrapper.__pyfuse_traced__ = True  # type: ignore[attr-defined]
        wrapper.run = _make_run_method(wrapper, func)  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    def _proxy_generator(
        self,
        gen: Generator[object, object, object],
        qualified_name: str,
    ) -> Generator[object, object, object]:
        """Wrap a generator to maintain call stack context during iteration."""
        stack = self._get_call_stack()
        stack.append(qualified_name)
        try:
            value = next(gen)
        except StopIteration as e:
            return e.value
        finally:
            stack.pop()

        while True:
            try:
                sent = yield value
            except GeneratorExit:
                gen.close()
                return  # type: ignore[return-value]
            except BaseException as exc:
                stack = self._get_call_stack()
                stack.append(qualified_name)
                try:
                    value = gen.throw(exc)
                except StopIteration as e:
                    return e.value
                finally:
                    stack.pop()
            else:
                stack = self._get_call_stack()
                stack.append(qualified_name)
                try:
                    value = gen.send(sent)
                except StopIteration as e:
                    return e.value
                finally:
                    stack.pop()

    async def _proxy_async_generator(
        self,
        async_gen: AsyncGenerator[object, object],
        qualified_name: str,
    ) -> AsyncGenerator[object, object]:
        """Wrap an async generator to maintain call stack context during iteration."""
        # Isolate once at entry; subsequent calls reuse the same isolated stack.
        self._ensure_isolated_stack()
        stack = self._get_call_stack()
        stack.append(qualified_name)
        try:
            value = await async_gen.__anext__()
        except StopAsyncIteration:
            return
        finally:
            stack.pop()

        while True:
            try:
                sent = yield value
            except GeneratorExit:
                await async_gen.aclose()
                return
            except BaseException as exc:
                stack = self._get_call_stack()
                stack.append(qualified_name)
                try:
                    value = await async_gen.athrow(exc)
                except StopAsyncIteration:
                    return
                finally:
                    stack.pop()
            else:
                stack = self._get_call_stack()
                stack.append(qualified_name)
                try:
                    value = await async_gen.asend(sent)
                except StopAsyncIteration:
                    return
                finally:
                    stack.pop()

    @property
    def nodes(self) -> dict[str, FunctionNode]:
        return dict(self._nodes)

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
        imports = filter_imports(all_imports, used_names)
        owner_class = _resolve_owner_class(func.__qualname__)

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
        """Find and auto-register untraced bare-call dependencies of a node."""
        module_obj = sys.modules.get(module_name)
        if module_obj is None:
            warnings.warn(
                f"Cannot auto-discover dependencies for "
                f"'{node.qualified_name}': module '{module_name}' not found "
                "in sys.modules.",
                stacklevel=2,
            )
            return
        bare_calls = find_bare_calls(node.source)
        imports_to_remove: list[ImportInfo] = []
        for name in bare_calls:
            if name in _BUILTIN_NAMES:
                continue
            func_obj = getattr(module_obj, name, None)
            if func_obj is None or not inspect.isfunction(func_obj):
                continue
            if func_obj.__name__ != name:
                continue  # Skip aliased imports to avoid name mismatch
            if self._auto_register(func_obj):
                if func_obj.__module__ != module_name:
                    matching = [i for i in node.imports if i.bound_name == name]
                    imports_to_remove.extend(matching)
        if imports_to_remove:
            node.imports = [i for i in node.imports if i not in imports_to_remove]

    def register(self, func: Callable[..., object]) -> None:
        # Unwrap if already traced
        original = func
        while hasattr(original, "__wrapped__"):
            original = original.__wrapped__  # type: ignore[attr-defined]

        qualified_name = f"{original.__module__}.{original.__qualname__}"
        logger.info("Registering %s", qualified_name)

        try:
            source = get_function_source(original)
            all_imports = get_module_imports(original)
        except (OSError, TypeError) as exc:
            logger.info(
                "Cannot register %s: source unavailable", qualified_name
            )
            raise PyFuseError(
                f"Cannot trace function '{original.__qualname__}': source code "
                "unavailable. Functions must be defined in .py source files."
            ) from exc

        used_names = get_used_names(source)
        imports = filter_imports(all_imports, used_names)
        owner_class = _resolve_owner_class(original.__qualname__)

        closure_vars: dict[str, str] = {}
        closure_func_refs: dict[str, str] = {}
        if original.__code__.co_freevars:
            cv = inspect.getclosurevars(original)
            for name, value in cv.nonlocals.items():
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

    def to_store(self, *funcs: Callable[..., object] | str) -> FuseStore:
        """Build a :class:`FuseStore` from this graph.

        Args:
            *funcs: If given, only include these functions and their
                transitive dependencies.  Otherwise the full graph.
        """
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

        store = FuseStore()
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
    def deserialize_graph(cls, json_str: str) -> FuseGraph:
        store = FuseStore.from_json(json_str)
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
            )
            graph._nodes[node.qualified_name] = node
        return graph

    @staticmethod
    def reconstruct(json_str: str, function_name: str) -> str:
        store = FuseStore.from_json(json_str)
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
