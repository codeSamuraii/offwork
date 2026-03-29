from __future__ import annotations

import inspect
import json
from pathlib import Path

from pyfuse import reconstruct, serialize
from pyfuse._graph import FuseGraph
from tests.conftest import create_module


# ---------------------------------------------------------------------------
# Runtime dependency tracing
# ---------------------------------------------------------------------------


def test_wrapper_preserves_behavior(tmp_path: Path) -> None:
    """Wrapper returns same values and preserves __name__/__qualname__."""
    mod = create_module(
        tmp_path,
        "wbehav",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def double(x):\n"
            "    return x * 2\n"
        ),
    )
    assert mod.double(5) == 10
    assert mod.double.__name__ == "double"
    assert mod.double.__qualname__ == "double"
    assert mod.double.__module__ == "wbehav"


def test_wrapper_inspect_getsource(tmp_path: Path) -> None:
    """inspect.getsource() follows __wrapped__ to the original source."""
    mod = create_module(
        tmp_path,
        "winspect",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def greet(name):\n"
            "    return f'hello {name}'\n"
        ),
    )
    source = inspect.getsource(mod.greet)
    assert "def greet(name):" in source


def test_runtime_dep_obj_method_call(tmp_path: Path) -> None:
    """obj.method() is detected as a runtime dependency."""
    mod = create_module(
        tmp_path,
        "objcall",
        (
            "from pyfuse import trace\n\n"
            "class Processor:\n"
            "    @trace\n"
            "    def step(self, x):\n"
            "        return x.upper()\n\n"
            "@trace\n"
            "def run(proc, data):\n"
            "    return proc.step(data)\n"
        ),
    )
    p = mod.Processor()
    mod.run(p, "hello")

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    run_deps = data["nodes"]["objcall.run"]["dependencies"]
    assert "objcall.Processor.step" in run_deps


def test_runtime_dep_direct_call(tmp_path: Path) -> None:
    """Direct calls to traced functions are recorded at runtime."""
    mod = create_module(
        tmp_path,
        "dircall",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def helper(x):\n"
            "    return x + 1\n\n"
            "@trace\n"
            "def caller(x):\n"
            "    return helper(x)\n"
        ),
    )
    mod.caller(5)

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    assert "dircall.helper" in data["nodes"]["dircall.caller"]["dependencies"]


def test_runtime_dep_chain(tmp_path: Path) -> None:
    """A -> B -> C chain is captured through wrappers."""
    mod = create_module(
        tmp_path,
        "rtchain",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def step_a(x):\n"
            "    return x\n\n"
            "@trace\n"
            "def step_b(x):\n"
            "    return step_a(x)\n\n"
            "@trace\n"
            "def step_c(x):\n"
            "    return step_b(x)\n"
        ),
    )
    mod.step_c(1)

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    assert "rtchain.step_b" in data["nodes"]["rtchain.step_c"]["dependencies"]
    assert "rtchain.step_a" in data["nodes"]["rtchain.step_b"]["dependencies"]


def test_runtime_dep_no_self_dependency(tmp_path: Path) -> None:
    """Recursive calls don't create A -> A edges."""
    mod = create_module(
        tmp_path,
        "selfcall",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def factorial(n):\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * factorial(n - 1)\n"
        ),
    )
    mod.factorial(5)

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = data["nodes"]["selfcall.factorial"]["dependencies"]
    assert "selfcall.factorial" not in deps


def test_runtime_dep_merges_with_static(tmp_path: Path) -> None:
    """Runtime deps are unioned with static deps."""
    mod = create_module(
        tmp_path,
        "merge",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def static_dep(x):\n"
            "    return x\n\n"
            "class Worker:\n"
            "    @trace\n"
            "    def dynamic_dep(self, x):\n"
            "        return x\n\n"
            "@trace\n"
            "def caller(w, x):\n"
            "    # static_dep is detectable statically\n"
            "    a = static_dep(x)\n"
            "    # w.dynamic_dep is only detectable at runtime\n"
            "    b = w.dynamic_dep(x)\n"
            "    return a, b\n"
        ),
    )
    w = mod.Worker()
    mod.caller(w, 1)

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = data["nodes"]["merge.caller"]["dependencies"]
    assert "merge.static_dep" in deps
    assert "merge.Worker.dynamic_dep" in deps


def test_runtime_dep_obj_method_reconstruction(tmp_path: Path) -> None:
    """End-to-end: obj.method() dep leads to complete reconstruction."""
    mod = create_module(
        tmp_path,
        "objrecon",
        (
            "import json\n\n"
            "from pyfuse import trace\n\n"
            "class Formatter:\n"
            "    @trace\n"
            "    def format(self, data):\n"
            "        return json.dumps(data)\n\n"
            "@trace\n"
            "def process(fmt, data):\n"
            "    return fmt.format(data)\n"
        ),
    )
    f = mod.Formatter()
    mod.process(f, {"key": "value"})

    source = reconstruct(serialize(), "process")
    assert "import json" in source
    assert "class Formatter:" in source
    assert "def format(self, data):" in source
    assert "def process(fmt, data):" in source


# ---------------------------------------------------------------------------
# Closure variable capture
# ---------------------------------------------------------------------------


def test_closure_vars_captured(tmp_path: Path) -> None:
    """Closure variables are stored in the FunctionNode."""
    mod = create_module(
        tmp_path,
        "cvcap",
        (
            "from pyfuse import trace\n\n"
            "def make_multiplier(factor):\n"
            "    @trace\n"
            "    def multiply(x):\n"
            "        return x * factor\n"
            "    return multiply\n"
        ),
    )
    mod.make_multiplier(3)
    graph = FuseGraph.default()
    node = graph.nodes["cvcap.make_multiplier.<locals>.multiply"]
    assert node.closure_vars == {"factor": "3"}


def test_closure_vars_serialized(tmp_path: Path) -> None:
    """closure_vars survive serialize/deserialize roundtrip."""
    mod = create_module(
        tmp_path,
        "cvser",
        (
            "from pyfuse import trace\n\n"
            "def outer(scale, label):\n"
            "    @trace\n"
            "    def inner(x):\n"
            "        return label + str(x * scale)\n"
            "    return inner\n"
        ),
    )
    mod.outer(2.5, "val:")
    graph = FuseGraph.default()
    json_str = graph.serialize()
    restored = FuseGraph.deserialize_graph(json_str)
    node = restored.nodes["cvser.outer.<locals>.inner"]
    assert node.closure_vars == {"scale": "2.5", "label": "'val:'"}


def test_closure_hoisted_as_kwonly_params(tmp_path: Path) -> None:
    """Reconstructed source has closure vars as keyword-only params."""
    mod = create_module(
        tmp_path,
        "cvhoist",
        (
            "from pyfuse import trace\n\n"
            "def outer(scale):\n"
            "    @trace\n"
            "    def inner(x):\n"
            "        return x * scale\n"
            "    return inner\n"
        ),
    )
    mod.outer(5)
    source = reconstruct(serialize(), "inner")
    assert "scale=5" in source


def test_closure_reconstructed_code_is_runnable(tmp_path: Path) -> None:
    """Reconstructed code with hoisted closure vars can be exec'd and called."""
    mod = create_module(
        tmp_path,
        "cvrun",
        (
            "from pyfuse import trace\n\n"
            "def outer(factor):\n"
            "    @trace\n"
            "    def multiply(x):\n"
            "        return x * factor\n"
            "    return multiply\n"
        ),
    )
    mod.outer(7)
    source = reconstruct(serialize(), "multiply")
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["multiply"](6) == 42  # type: ignore[operator]


def test_backward_compat_no_closure_vars() -> None:
    """Old JSON without closure_vars deserializes correctly."""
    old_json = json.dumps({
        "version": "0.1.0",
        "nodes": {
            "mod.func": {
                "qualified_name": "mod.func",
                "name": "func",
                "module": "mod",
                "source": "def func():\n    pass",
                "imports": [],
                "dependencies": [],
                "owner_class": None,
            }
        },
    })
    graph = FuseGraph.deserialize_graph(old_json)
    node = graph.nodes["mod.func"]
    assert node.closure_vars == {}
