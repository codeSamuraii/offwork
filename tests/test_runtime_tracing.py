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


def test_static_typed_obj_method_no_execution(tmp_path: Path) -> None:
    """obj.method() with type annotation is detected statically, no execution needed."""
    mod = create_module(
        tmp_path,
        "staticobj",
        (
            "from pyfuse import trace\n\n"
            "class Processor:\n"
            "    @trace\n"
            "    def step(self, x):\n"
            "        return x.upper()\n\n"
            "@trace\n"
            "def run(proc: Processor, data: str) -> str:\n"
            "    return proc.step(data)\n"
        ),
    )
    # Do NOT call mod.run() -- dependency should be detected statically
    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    run_deps = data["nodes"]["staticobj.run"]["dependencies"]
    assert "staticobj.Processor.step" in run_deps


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
# Generator wrapping
# ---------------------------------------------------------------------------


def test_generator_body_dep_detected(tmp_path: Path) -> None:
    """Traced calls inside a generator body are recorded during iteration."""
    mod = create_module(
        tmp_path,
        "genbody",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def helper(x):\n"
            "    return x + 1\n\n"
            "@trace\n"
            "def gen_func(items):\n"
            "    for item in items:\n"
            "        yield helper(item)\n"
        ),
    )
    assert list(mod.gen_func([1, 2, 3])) == [2, 3, 4]

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = data["nodes"]["genbody.gen_func"]["dependencies"]
    assert "genbody.helper" in deps


def test_generator_caller_dep_detected(tmp_path: Path) -> None:
    """Caller of a generator function has the caller->gen edge recorded."""
    mod = create_module(
        tmp_path,
        "gencaller",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def gen_func(items):\n"
            "    yield from items\n\n"
            "@trace\n"
            "def caller():\n"
            "    return list(gen_func([1, 2, 3]))\n"
        ),
    )
    assert mod.caller() == [1, 2, 3]

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = data["nodes"]["gencaller.caller"]["dependencies"]
    assert "gencaller.gen_func" in deps


def test_generator_send_maintains_context(tmp_path: Path) -> None:
    """Dependencies are tracked when generator.send() is used."""
    mod = create_module(
        tmp_path,
        "gensend",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def transform(x):\n"
            "    return x * 10\n\n"
            "@trace\n"
            "def accumulator():\n"
            "    total = 0\n"
            "    while True:\n"
            "        value = yield total\n"
            "        if value is None:\n"
            "            break\n"
            "        total += transform(value)\n"
        ),
    )
    gen = mod.accumulator()
    next(gen)  # prime the generator
    gen.send(1)
    gen.send(2)
    try:
        gen.send(None)
    except StopIteration:
        pass

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = data["nodes"]["gensend.accumulator"]["dependencies"]
    assert "gensend.transform" in deps


def test_generator_preserves_values(tmp_path: Path) -> None:
    """Proxy generator yields the same values as the original."""
    mod = create_module(
        tmp_path,
        "genvals",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def countdown(n):\n"
            "    while n > 0:\n"
            "        yield n\n"
            "        n -= 1\n"
        ),
    )
    assert list(mod.countdown(5)) == [5, 4, 3, 2, 1]


def test_generator_close_works(tmp_path: Path) -> None:
    """Calling .close() on the proxy properly closes the underlying generator."""
    mod = create_module(
        tmp_path,
        "genclose",
        (
            "from pyfuse import trace\n\n"
            "closed = False\n\n"
            "@trace\n"
            "def infinite():\n"
            "    global closed\n"
            "    try:\n"
            "        while True:\n"
            "            yield 1\n"
            "    finally:\n"
            "        closed = True\n"
        ),
    )
    gen = mod.infinite()
    next(gen)
    gen.close()
    assert mod.closed is True


def test_generator_reconstruction(tmp_path: Path) -> None:
    """Generator with traced deps reconstructs correctly end-to-end."""
    mod = create_module(
        tmp_path,
        "genrecon",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def double(x):\n"
            "    return x * 2\n\n"
            "@trace\n"
            "def gen_doubles(items):\n"
            "    for item in items:\n"
            "        yield double(item)\n"
        ),
    )
    list(mod.gen_doubles([1, 2]))

    source = reconstruct(serialize(), "gen_doubles")
    assert "def double(x):" in source
    assert "def gen_doubles(items):" in source


def test_generator_throw_maintains_context(tmp_path: Path) -> None:
    """Dependencies are tracked when generator.throw() is used."""
    mod = create_module(
        tmp_path,
        "genthrow",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def fallback(x):\n"
            "    return x * -1\n\n"
            "@trace\n"
            "def resilient():\n"
            "    while True:\n"
            "        try:\n"
            "            value = yield\n"
            "        except ValueError:\n"
            "            yield fallback(0)\n"
        ),
    )
    gen = mod.resilient()
    next(gen)  # prime
    result = gen.throw(ValueError("bad"))
    assert result == 0

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = data["nodes"]["genthrow.resilient"]["dependencies"]
    assert "genthrow.fallback" in deps


def test_generator_return_value(tmp_path: Path) -> None:
    """Proxy preserves StopIteration.value from generator return."""
    mod = create_module(
        tmp_path,
        "genret",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def gen_with_return():\n"
            "    yield 1\n"
            "    yield 2\n"
            "    return 'done'\n"
        ),
    )
    gen = mod.gen_with_return()
    assert next(gen) == 1
    assert next(gen) == 2
    try:
        next(gen)
        raise AssertionError("Should have raised StopIteration")
    except StopIteration as e:
        assert e.value == "done"


def test_generator_nested_chain(tmp_path: Path) -> None:
    """Nested generators: gen_outer -> gen_inner dependency tracked."""
    mod = create_module(
        tmp_path,
        "genchain",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def gen_inner():\n"
            "    yield 1\n"
            "    yield 2\n\n"
            "@trace\n"
            "def gen_outer():\n"
            "    for val in gen_inner():\n"
            "        yield val * 10\n"
        ),
    )
    assert list(mod.gen_outer()) == [10, 20]

    graph = FuseGraph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = data["nodes"]["genchain.gen_outer"]["dependencies"]
    assert "genchain.gen_inner" in deps


def test_generator_wrapper_preserves_metadata(tmp_path: Path) -> None:
    """Generator wrapper preserves __name__, __qualname__, and __wrapped__."""
    mod = create_module(
        tmp_path,
        "genmeta",
        (
            "import inspect\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def my_gen(n):\n"
            "    yield n\n"
        ),
    )
    assert mod.my_gen.__name__ == "my_gen"
    assert mod.my_gen.__qualname__ == "my_gen"
    # __wrapped__ points to the original generator function
    assert mod.inspect.isgeneratorfunction(mod.my_gen.__wrapped__)


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
    assert node.closure_func_refs == {}


# ---------------------------------------------------------------------------
# Closure variable validation
# ---------------------------------------------------------------------------


def test_closure_valid_repr_accepted(tmp_path: Path) -> None:
    """A closure var with valid Python repr is captured normally."""
    mod = create_module(
        tmp_path,
        "cvvalid",
        (
            "from pyfuse import trace\n\n"
            "def outer():\n"
            "    x = 42\n"
            "    label = 'hello'\n"
            "    @trace\n"
            "    def inner():\n"
            "        return label + str(x)\n"
            "    return inner\n"
        ),
    )
    mod.outer()
    graph = FuseGraph.default()
    node = graph.nodes["cvvalid.outer.<locals>.inner"]
    assert node.closure_vars == {"x": "42", "label": "'hello'"}
    assert node.closure_func_refs == {}


def test_closure_traced_func_detected_as_dep(tmp_path: Path) -> None:
    """Traced function captured as closure -> dependency, not in closure_vars."""
    mod = create_module(
        tmp_path,
        "cvfunc",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def helper(x):\n"
            "    return x + 1\n\n"
            "def outer():\n"
            "    fn = helper\n"
            "    @trace\n"
            "    def inner(x):\n"
            "        return fn(x)\n"
            "    return inner\n"
        ),
    )
    mod.outer()
    graph = FuseGraph.default()
    node = graph.nodes["cvfunc.outer.<locals>.inner"]
    # fn should NOT be in closure_vars (repr is not valid Python)
    assert "fn" not in node.closure_vars
    # fn -> helper should be in closure_func_refs
    assert node.closure_func_refs == {"fn": "cvfunc.helper"}
    # helper should be in dependencies
    assert "cvfunc.helper" in node.dependencies


def test_closure_invalid_repr_non_traced_warns(tmp_path: Path) -> None:
    """Non-traced object with invalid repr emits descriptive warning."""
    import warnings as w

    mod = create_module(
        tmp_path,
        "cvinvalid",
        (
            "import io\n\n"
            "from pyfuse import trace\n\n"
            "def outer():\n"
            "    f = io.StringIO()\n"
            "    @trace\n"
            "    def inner():\n"
            "        return f.read()\n"
            "    return inner\n"
        ),
    )
    with w.catch_warnings(record=True) as caught:
        w.simplefilter("always")
        mod.outer()

    msgs = [str(c.message) for c in caught]
    assert any("not valid Python" in m for m in msgs)


def test_closure_func_refs_survive_refresh(tmp_path: Path) -> None:
    """closure_func_refs are preserved after refresh()."""
    mod = create_module(
        tmp_path,
        "cvrefresh",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def helper(x):\n"
            "    return x + 1\n\n"
            "def outer():\n"
            "    fn = helper\n"
            "    @trace\n"
            "    def inner(x):\n"
            "        return fn(x)\n"
            "    return inner\n"
        ),
    )
    mod.outer()
    graph = FuseGraph.default()
    graph.refresh()  # explicit refresh
    node = graph.nodes["cvrefresh.outer.<locals>.inner"]
    assert "cvrefresh.helper" in node.dependencies
    assert node.closure_func_refs == {"fn": "cvrefresh.helper"}


def test_closure_func_refs_serialization_roundtrip(tmp_path: Path) -> None:
    """closure_func_refs survive serialize/deserialize roundtrip."""
    mod = create_module(
        tmp_path,
        "cvserial",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def helper(x):\n"
            "    return x + 1\n\n"
            "def outer():\n"
            "    fn = helper\n"
            "    @trace\n"
            "    def inner(x):\n"
            "        return fn(x)\n"
            "    return inner\n"
        ),
    )
    mod.outer()
    graph = FuseGraph.default()
    json_str = graph.serialize()
    restored = FuseGraph.deserialize_graph(json_str)
    node = restored.nodes["cvserial.outer.<locals>.inner"]
    assert node.closure_func_refs == {"fn": "cvserial.helper"}
    assert "cvserial.helper" in node.dependencies


def test_closure_func_ref_reconstructed_code_is_runnable(tmp_path: Path) -> None:
    """Reconstructed code with closure func ref is hoisted and executable."""
    mod = create_module(
        tmp_path,
        "cvfuncrun",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def helper(x):\n"
            "    return x + 1\n\n"
            "def outer():\n"
            "    fn = helper\n"
            "    @trace\n"
            "    def inner(x):\n"
            "        return fn(x) * 2\n"
            "    return inner\n"
        ),
    )
    mod.outer()
    source = reconstruct(serialize(), "inner")
    # helper should be defined, inner should have fn=helper as kwonly param
    assert "def helper(x):" in source
    assert "fn=helper" in source
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["inner"](4) == 10  # helper(4)=5, 5*2=10


def test_closure_func_ref_end_to_end_pipeline(tmp_path: Path) -> None:
    """Full pipeline: trace → serialize → reconstruct → exec with closure func ref."""
    mod = create_module(
        tmp_path,
        "cvpipe",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def add_one(x):\n"
            "    return x + 1\n\n"
            "@trace\n"
            "def double(x):\n"
            "    return x * 2\n\n"
            "def make_pipeline():\n"
            "    step1 = add_one\n"
            "    step2 = double\n"
            "    @trace\n"
            "    def pipeline(x):\n"
            "        return step2(step1(x))\n"
            "    return pipeline\n"
        ),
    )
    pipe = mod.make_pipeline()
    pipe(3)  # trigger runtime tracing

    source = reconstruct(serialize(), "pipeline")
    assert "def add_one(x):" in source
    assert "def double(x):" in source
    assert "step1=add_one" in source
    assert "step2=double" in source
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["pipeline"](3) == 8  # add_one(3)=4, double(4)=8
