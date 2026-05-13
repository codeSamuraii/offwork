import asyncio
import inspect
import json
import warnings
from pathlib import Path

from away import get_graph, reconstruct, serialize
from away.graph.graph import Graph
from tests.conftest import create_module


def _get_dep_names(data: dict, qname: str) -> list[str]:
    """Resolve a node's dependency hashes back to qualified names."""
    h = data["refs"][qname]
    dep_hashes = data.get("deps", {}).get(h, [])
    hash_to_name = {v: k for k, v in data["refs"].items()}
    return [hash_to_name[dh] for dh in dep_hashes if dh in hash_to_name]


# ---------------------------------------------------------------------------
# Runtime dependency tracing
# ---------------------------------------------------------------------------


def test_wrapper_preserves_behavior(tmp_path: Path) -> None:
    """Wrapper returns same values and preserves __name__/__qualname__."""
    mod = create_module(
        tmp_path,
        "wbehav",
        (
            "from away import trace\n\n"
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
            "from away import trace\n\n"
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
            "from away import trace\n\n"
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
    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    run_deps = _get_dep_names(data, "staticobj.run")
    assert "staticobj.Processor.step" in run_deps


def test_runtime_dep_obj_method_call(tmp_path: Path) -> None:
    """obj.method() is detected as a runtime dependency."""
    mod = create_module(
        tmp_path,
        "objcall",
        (
            "from away import trace\n\n"
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

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    run_deps = _get_dep_names(data, "objcall.run")
    assert "objcall.Processor.step" in run_deps


def test_runtime_dep_direct_call(tmp_path: Path) -> None:
    """Direct calls to traced functions are recorded at runtime."""
    mod = create_module(
        tmp_path,
        "dircall",
        (
            "from away import trace\n\n"
            "@trace\n"
            "def helper(x):\n"
            "    return x + 1\n\n"
            "@trace\n"
            "def caller(x):\n"
            "    return helper(x)\n"
        ),
    )
    mod.caller(5)

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    assert "dircall.helper" in _get_dep_names(data, "dircall.caller")


def test_runtime_dep_chain(tmp_path: Path) -> None:
    """A -> B -> C chain is captured through wrappers."""
    mod = create_module(
        tmp_path,
        "rtchain",
        (
            "from away import trace\n\n"
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

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    assert "rtchain.step_b" in _get_dep_names(data, "rtchain.step_c")
    assert "rtchain.step_a" in _get_dep_names(data, "rtchain.step_b")


def test_runtime_dep_no_self_dependency(tmp_path: Path) -> None:
    """Recursive calls don't create A -> A edges."""
    mod = create_module(
        tmp_path,
        "selfcall",
        (
            "from away import trace\n\n"
            "@trace\n"
            "def factorial(n):\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * factorial(n - 1)\n"
        ),
    )
    mod.factorial(5)

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "selfcall.factorial")
    assert "selfcall.factorial" not in deps


def test_runtime_dep_merges_with_static(tmp_path: Path) -> None:
    """Runtime deps are unioned with static deps."""
    mod = create_module(
        tmp_path,
        "merge",
        (
            "from away import trace\n\n"
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

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "merge.caller")
    assert "merge.static_dep" in deps
    assert "merge.Worker.dynamic_dep" in deps


def test_runtime_dep_obj_method_reconstruction(tmp_path: Path) -> None:
    """End-to-end: obj.method() dep leads to complete reconstruction."""
    mod = create_module(
        tmp_path,
        "objrecon",
        (
            "import json\n\n"
            "from away import trace\n\n"
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
            "from away import trace\n\n"
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

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "genbody.gen_func")
    assert "genbody.helper" in deps


def test_generator_caller_dep_detected(tmp_path: Path) -> None:
    """Caller of a generator function has the caller->gen edge recorded."""
    mod = create_module(
        tmp_path,
        "gencaller",
        (
            "from away import trace\n\n"
            "@trace\n"
            "def gen_func(items):\n"
            "    yield from items\n\n"
            "@trace\n"
            "def caller():\n"
            "    return list(gen_func([1, 2, 3]))\n"
        ),
    )
    assert mod.caller() == [1, 2, 3]

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "gencaller.caller")
    assert "gencaller.gen_func" in deps


def test_generator_send_maintains_context(tmp_path: Path) -> None:
    """Dependencies are tracked when generator.send() is used."""
    mod = create_module(
        tmp_path,
        "gensend",
        (
            "from away import trace\n\n"
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

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "gensend.accumulator")
    assert "gensend.transform" in deps


def test_generator_preserves_values(tmp_path: Path) -> None:
    """Proxy generator yields the same values as the original."""
    mod = create_module(
        tmp_path,
        "genvals",
        (
            "from away import trace\n\n"
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
            "from away import trace\n\n"
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
            "from away import trace\n\n"
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
            "from away import trace\n\n"
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

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "genthrow.resilient")
    assert "genthrow.fallback" in deps


def test_generator_return_value(tmp_path: Path) -> None:
    """Proxy preserves StopIteration.value from generator return."""
    mod = create_module(
        tmp_path,
        "genret",
        (
            "from away import trace\n\n"
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
            "from away import trace\n\n"
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

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "genchain.gen_outer")
    assert "genchain.gen_inner" in deps


def test_generator_wrapper_preserves_metadata(tmp_path: Path) -> None:
    """Generator wrapper preserves __name__, __qualname__, and __wrapped__."""
    mod = create_module(
        tmp_path,
        "genmeta",
        (
            "import inspect\n\n"
            "from away import trace\n\n"
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
            "from away import trace\n\n"
            "def make_multiplier(factor):\n"
            "    @trace\n"
            "    def multiply(x):\n"
            "        return x * factor\n"
            "    return multiply\n"
        ),
    )
    mod.make_multiplier(3)
    graph = Graph.default()
    node = graph.nodes["cvcap.make_multiplier.<locals>.multiply"]
    assert node.closure_vars == {"factor": "3"}


def test_closure_vars_serialized(tmp_path: Path) -> None:
    """closure_vars survive serialize/deserialize roundtrip."""
    mod = create_module(
        tmp_path,
        "cvser",
        (
            "from away import trace\n\n"
            "def outer(scale, label):\n"
            "    @trace\n"
            "    def inner(x):\n"
            "        return label + str(x * scale)\n"
            "    return inner\n"
        ),
    )
    mod.outer(2.5, "val:")
    graph = Graph.default()
    json_str = graph.serialize()
    restored = Graph.deserialize_graph(json_str)
    node = restored.nodes["cvser.outer.<locals>.inner"]
    assert node.closure_vars == {"scale": "2.5", "label": "'val:'"}


def test_closure_hoisted_as_kwonly_params(tmp_path: Path) -> None:
    """Reconstructed source has closure vars as keyword-only params."""
    mod = create_module(
        tmp_path,
        "cvhoist",
        (
            "from away import trace\n\n"
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
            "from away import trace\n\n"
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


def test_deserialize_no_closure_vars() -> None:
    """JSON without closure_vars/closure_func_refs deserializes correctly."""
    node_hash = "abcdef0123456789"
    store_json = json.dumps({
        "version": "0.2.0",
        "objects": {
            node_hash: {
                "hash": node_hash,
                "name": "func",
                "module": "mod",
                "source": "def func():\n    pass",
                "imports": [],
                "deps": [],
                "owner_class": None,
            }
        },
        "refs": {
            "mod.func": node_hash,
        },
    })
    graph = Graph.deserialize_graph(store_json)
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
            "from away import trace\n\n"
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
    graph = Graph.default()
    node = graph.nodes["cvvalid.outer.<locals>.inner"]
    assert node.closure_vars == {"x": "42", "label": "'hello'"}
    assert node.closure_func_refs == {}


def test_closure_traced_func_detected_as_dep(tmp_path: Path) -> None:
    """Traced function captured as closure -> dependency, not in closure_vars."""
    mod = create_module(
        tmp_path,
        "cvfunc",
        (
            "from away import trace\n\n"
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
    graph = Graph.default()
    node = graph.nodes["cvfunc.outer.<locals>.inner"]
    # fn should NOT be in closure_vars (repr is not valid Python)
    assert "fn" not in node.closure_vars
    # fn -> helper should be in closure_func_refs
    assert node.closure_func_refs == {"fn": "cvfunc.helper"}
    # helper should be in dependencies
    assert "cvfunc.helper" in node.dependencies


def test_closure_invalid_repr_picklable_captured(tmp_path: Path) -> None:
    """Picklable object with invalid repr is captured via pickle fallback."""
    mod = create_module(
        tmp_path,
        "cvinvalid",
        (
            "import io\n\n"
            "from away import trace\n\n"
            "def outer():\n"
            "    f = io.StringIO()\n"
            "    @trace\n"
            "    def inner():\n"
            "        return f.read()\n"
            "    return inner\n"
        ),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        func = mod.outer()

    msgs = [str(c.message) for c in caught]
    assert not any("cannot be serialized" in m for m in msgs)

    node = get_graph().nodes["cvinvalid.outer.<locals>.inner"]
    assert "f" in node.closure_vars
    assert "__import__('pickle')" in node.closure_vars["f"]


def test_closure_func_refs_survive_refresh(tmp_path: Path) -> None:
    """closure_func_refs are preserved after refresh()."""
    mod = create_module(
        tmp_path,
        "cvrefresh",
        (
            "from away import trace\n\n"
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
    graph = Graph.default()
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
            "from away import trace\n\n"
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
    graph = Graph.default()
    json_str = graph.serialize()
    restored = Graph.deserialize_graph(json_str)
    node = restored.nodes["cvserial.outer.<locals>.inner"]
    assert node.closure_func_refs == {"fn": "cvserial.helper"}
    assert "cvserial.helper" in node.dependencies


def test_closure_func_ref_reconstructed_code_is_runnable(tmp_path: Path) -> None:
    """Reconstructed code with closure func ref is hoisted and executable."""
    mod = create_module(
        tmp_path,
        "cvfuncrun",
        (
            "from away import trace\n\n"
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
            "from away import trace\n\n"
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


# ---------------------------------------------------------------------------
# Async function wrapping
# ---------------------------------------------------------------------------


def test_async_wrapper_preserves_behavior(tmp_path: Path) -> None:
    """Async wrapper returns correct value via await."""
    mod = create_module(
        tmp_path,
        "asyncbehav",
        (
            "from away import trace\n\n"
            "@trace\n"
            "async def double(x):\n"
            "    return x * 2\n"
        ),
    )
    assert asyncio.run(mod.double(5)) == 10


def test_async_wrapper_preserves_coroutine_flag(tmp_path: Path) -> None:
    """Wrapped async function is still a coroutine function."""
    mod = create_module(
        tmp_path,
        "asyncflag",
        (
            "from away import trace\n\n"
            "@trace\n"
            "async def afunc():\n"
            "    return 1\n"
        ),
    )
    assert inspect.iscoroutinefunction(mod.afunc)


def test_async_wrapper_preserves_metadata(tmp_path: Path) -> None:
    """Async wrapper preserves __name__, __qualname__, __wrapped__."""
    mod = create_module(
        tmp_path,
        "asyncmeta",
        (
            "import inspect\n\n"
            "from away import trace\n\n"
            "@trace\n"
            "async def my_coro(x):\n"
            "    return x\n"
        ),
    )
    assert mod.my_coro.__name__ == "my_coro"
    assert mod.my_coro.__qualname__ == "my_coro"
    assert mod.inspect.iscoroutinefunction(mod.my_coro.__wrapped__)


def test_async_caller_to_async_callee(tmp_path: Path) -> None:
    """async caller -> async callee edge is recorded via await."""
    mod = create_module(
        tmp_path,
        "asynccall",
        (
            "from away import trace\n\n"
            "@trace\n"
            "async def async_helper(x):\n"
            "    return x + 1\n\n"
            "@trace\n"
            "async def async_caller(x):\n"
            "    return await async_helper(x)\n"
        ),
    )
    assert asyncio.run(mod.async_caller(5)) == 6

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "asynccall.async_caller")
    assert "asynccall.async_helper" in deps


def test_async_caller_to_sync_callee(tmp_path: Path) -> None:
    """Async function calling a sync traced function records the edge."""
    mod = create_module(
        tmp_path,
        "asyncsync",
        (
            "from away import trace\n\n"
            "@trace\n"
            "def sync_helper(x):\n"
            "    return x + 1\n\n"
            "@trace\n"
            "async def async_caller(x):\n"
            "    return sync_helper(x)\n"
        ),
    )
    assert asyncio.run(mod.async_caller(5)) == 6

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "asyncsync.async_caller")
    assert "asyncsync.sync_helper" in deps


def test_async_dep_chain(tmp_path: Path) -> None:
    """A -> B -> C chain via await is captured."""
    mod = create_module(
        tmp_path,
        "asyncchain",
        (
            "from away import trace\n\n"
            "@trace\n"
            "async def step_a(x):\n"
            "    return x\n\n"
            "@trace\n"
            "async def step_b(x):\n"
            "    return await step_a(x)\n\n"
            "@trace\n"
            "async def step_c(x):\n"
            "    return await step_b(x)\n"
        ),
    )
    asyncio.run(mod.step_c(1))

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    assert "asyncchain.step_b" in _get_dep_names(data, "asyncchain.step_c")
    assert "asyncchain.step_a" in _get_dep_names(data, "asyncchain.step_b")


def test_async_no_self_dependency(tmp_path: Path) -> None:
    """Recursive async calls don't create self-edges."""
    mod = create_module(
        tmp_path,
        "asyncself",
        (
            "from away import trace\n\n"
            "@trace\n"
            "async def factorial(n):\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * await factorial(n - 1)\n"
        ),
    )
    assert asyncio.run(mod.factorial(5)) == 120

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "asyncself.factorial")
    assert "asyncself.factorial" not in deps


def test_async_runtime_dep_merges_with_static(tmp_path: Path) -> None:
    """Runtime async deps are merged with static deps."""
    mod = create_module(
        tmp_path,
        "asyncmerge",
        (
            "from away import trace\n\n"
            "@trace\n"
            "def static_dep(x):\n"
            "    return x\n\n"
            "class Worker:\n"
            "    @trace\n"
            "    def dynamic_dep(self, x):\n"
            "        return x\n\n"
            "@trace\n"
            "async def caller(w, x):\n"
            "    a = static_dep(x)\n"
            "    b = w.dynamic_dep(x)\n"
            "    return a, b\n"
        ),
    )
    w = mod.Worker()
    asyncio.run(mod.caller(w, 1))

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "asyncmerge.caller")
    assert "asyncmerge.static_dep" in deps
    assert "asyncmerge.Worker.dynamic_dep" in deps


def test_async_reconstruction(tmp_path: Path) -> None:
    """End-to-end: async function trace/serialize/reconstruct."""
    mod = create_module(
        tmp_path,
        "asyncrecon",
        (
            "import json\n\n"
            "from away import trace\n\n"
            "@trace\n"
            "async def async_helper(data):\n"
            "    return json.dumps(data)\n\n"
            "@trace\n"
            "async def async_main(data):\n"
            "    return await async_helper(data)\n"
        ),
    )
    asyncio.run(mod.async_main({"key": "value"}))

    source = reconstruct(serialize(), "async_main")
    assert "import json" in source
    assert "async def async_helper(data):" in source
    assert "async def async_main(data):" in source


def test_async_concurrent_tasks_isolated(tmp_path: Path) -> None:
    """Two concurrent async tasks don't contaminate each other's deps."""
    mod = create_module(
        tmp_path,
        "asynciso",
        (
            "import asyncio\n\n"
            "from away import trace\n\n"
            "@trace\n"
            "async def dep_a(x):\n"
            "    return x\n\n"
            "@trace\n"
            "async def dep_b(x):\n"
            "    return x\n\n"
            "@trace\n"
            "async def task_a(x):\n"
            "    await asyncio.sleep(0)\n"
            "    return await dep_a(x)\n\n"
            "@trace\n"
            "async def task_b(x):\n"
            "    await asyncio.sleep(0)\n"
            "    return await dep_b(x)\n"
        ),
    )

    async def main() -> None:
        await asyncio.gather(mod.task_a(1), mod.task_b(2))

    asyncio.run(main())

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps_a = _get_dep_names(data, "asynciso.task_a")
    deps_b = _get_dep_names(data, "asynciso.task_b")
    assert "asynciso.dep_a" in deps_a
    assert "asynciso.dep_b" not in deps_a
    assert "asynciso.dep_b" in deps_b
    assert "asynciso.dep_a" not in deps_b


# ---------------------------------------------------------------------------
# Async generator wrapping
# ---------------------------------------------------------------------------


def test_async_gen_body_dep_detected(tmp_path: Path) -> None:
    """Traced calls inside an async generator body are recorded during async for."""
    mod = create_module(
        tmp_path,
        "agenbody",
        (
            "from away import trace\n\n"
            "@trace\n"
            "def helper(x):\n"
            "    return x + 1\n\n"
            "@trace\n"
            "async def async_gen(items):\n"
            "    for item in items:\n"
            "        yield helper(item)\n"
        ),
    )

    async def main() -> list[int]:
        return [x async for x in mod.async_gen([1, 2, 3])]

    assert asyncio.run(main()) == [2, 3, 4]

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "agenbody.async_gen")
    assert "agenbody.helper" in deps


def test_async_gen_caller_dep_detected(tmp_path: Path) -> None:
    """Caller of an async generator has the caller->asyncgen edge recorded."""
    mod = create_module(
        tmp_path,
        "agencaller",
        (
            "from away import trace\n\n"
            "@trace\n"
            "async def async_gen(items):\n"
            "    for item in items:\n"
            "        yield item\n\n"
            "@trace\n"
            "async def caller():\n"
            "    return [x async for x in async_gen([1, 2, 3])]\n"
        ),
    )
    assert asyncio.run(mod.caller()) == [1, 2, 3]

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "agencaller.caller")
    assert "agencaller.async_gen" in deps


def test_async_gen_preserves_values(tmp_path: Path) -> None:
    """Async generator proxy yields the same values as the original."""
    mod = create_module(
        tmp_path,
        "agenvals",
        (
            "from away import trace\n\n"
            "@trace\n"
            "async def countdown(n):\n"
            "    while n > 0:\n"
            "        yield n\n"
            "        n -= 1\n"
        ),
    )

    async def main() -> list[int]:
        return [x async for x in mod.countdown(5)]

    assert asyncio.run(main()) == [5, 4, 3, 2, 1]


def test_async_gen_asend(tmp_path: Path) -> None:
    """Dependencies are tracked when asend() is used."""
    mod = create_module(
        tmp_path,
        "agensend",
        (
            "from away import trace\n\n"
            "@trace\n"
            "def transform(x):\n"
            "    return x * 10\n\n"
            "@trace\n"
            "async def accumulator():\n"
            "    total = 0\n"
            "    while True:\n"
            "        value = yield total\n"
            "        if value is None:\n"
            "            break\n"
            "        total += transform(value)\n"
        ),
    )

    async def main() -> None:
        gen = mod.accumulator()
        await gen.__anext__()
        await gen.asend(1)
        await gen.asend(2)
        try:
            await gen.asend(None)
        except StopAsyncIteration:
            pass

    asyncio.run(main())

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "agensend.accumulator")
    assert "agensend.transform" in deps


def test_async_gen_athrow(tmp_path: Path) -> None:
    """Dependencies are tracked when athrow() is used."""
    mod = create_module(
        tmp_path,
        "agenthrow",
        (
            "from away import trace\n\n"
            "@trace\n"
            "def fallback(x):\n"
            "    return x * -1\n\n"
            "@trace\n"
            "async def resilient():\n"
            "    while True:\n"
            "        try:\n"
            "            value = yield\n"
            "        except ValueError:\n"
            "            yield fallback(0)\n"
        ),
    )

    async def main() -> int:
        gen = mod.resilient()
        await gen.__anext__()  # prime
        return await gen.athrow(ValueError("bad"))

    result = asyncio.run(main())
    assert result == 0

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "agenthrow.resilient")
    assert "agenthrow.fallback" in deps


def test_async_gen_aclose(tmp_path: Path) -> None:
    """aclose() properly closes the underlying async generator."""
    mod = create_module(
        tmp_path,
        "agenclose",
        (
            "from away import trace\n\n"
            "closed = False\n\n"
            "@trace\n"
            "async def infinite():\n"
            "    global closed\n"
            "    try:\n"
            "        while True:\n"
            "            yield 1\n"
            "    finally:\n"
            "        closed = True\n"
        ),
    )

    async def main() -> None:
        gen = mod.infinite()
        await gen.__anext__()
        await gen.aclose()

    asyncio.run(main())
    assert mod.closed is True


def test_async_gen_wrapper_preserves_metadata(tmp_path: Path) -> None:
    """Async generator wrapper preserves __name__, __qualname__, __wrapped__."""
    mod = create_module(
        tmp_path,
        "agenmeta",
        (
            "import inspect\n\n"
            "from away import trace\n\n"
            "@trace\n"
            "async def my_agen(n):\n"
            "    yield n\n"
        ),
    )
    assert mod.my_agen.__name__ == "my_agen"
    assert mod.my_agen.__qualname__ == "my_agen"
    assert mod.inspect.isasyncgenfunction(mod.my_agen.__wrapped__)


def test_async_gen_nested_chain(tmp_path: Path) -> None:
    """Async gen calling another async gen: both edges tracked."""
    mod = create_module(
        tmp_path,
        "agenchain",
        (
            "from away import trace\n\n"
            "@trace\n"
            "async def inner():\n"
            "    yield 1\n"
            "    yield 2\n\n"
            "@trace\n"
            "async def outer():\n"
            "    async for val in inner():\n"
            "        yield val * 10\n"
        ),
    )

    async def main() -> list[int]:
        return [x async for x in mod.outer()]

    assert asyncio.run(main()) == [10, 20]

    graph = Graph.default()
    graph_json = graph.serialize()
    data = json.loads(graph_json)
    deps = _get_dep_names(data, "agenchain.outer")
    assert "agenchain.inner" in deps


def test_async_gen_reconstruction(tmp_path: Path) -> None:
    """End-to-end: async generator trace/serialize/reconstruct."""
    mod = create_module(
        tmp_path,
        "agenrecon",
        (
            "from away import trace\n\n"
            "@trace\n"
            "def double(x):\n"
            "    return x * 2\n\n"
            "@trace\n"
            "async def gen_doubles(items):\n"
            "    for item in items:\n"
            "        yield double(item)\n"
        ),
    )

    async def main() -> list[int]:
        return [x async for x in mod.gen_doubles([1, 2])]

    asyncio.run(main())

    source = reconstruct(serialize(), "gen_doubles")
    assert "def double(x):" in source
    assert "async def gen_doubles(items):" in source


# ---------------------------------------------------------------------------
# Feature 1: Non-traced callables in closures
# ---------------------------------------------------------------------------


def test_closure_untraced_user_function_auto_registered(tmp_path: Path) -> None:
    """Non-traced user function captured in closure is auto-registered."""
    mod = create_module(
        tmp_path,
        "cvuntraced",
        (
            "from away import trace\n\n"
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
    func = mod.outer()
    source = reconstruct(serialize(), "inner")
    assert "def helper(x):" in source
    assert "def inner(" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["inner"](5) == 6  # type: ignore[operator]


def test_closure_lambda_captured(tmp_path: Path) -> None:
    """Lambda captured in closure has its source extracted."""
    mod = create_module(
        tmp_path,
        "cvlambda",
        (
            "from away import trace\n\n"
            "def outer():\n"
            "    fn = lambda x: x * 2\n"
            "    @trace\n"
            "    def inner(x):\n"
            "        return fn(x)\n"
            "    return inner\n"
        ),
    )
    func = mod.outer()

    node = get_graph().nodes["cvlambda.outer.<locals>.inner"]
    assert "fn" in node.closure_vars
    assert "lambda" in node.closure_vars["fn"]


def test_closure_untraced_func_with_deps(tmp_path: Path) -> None:
    """Non-traced function in closure has its own dependencies discovered."""
    mod = create_module(
        tmp_path,
        "cvuntraceddeps",
        (
            "from away import trace\n\n"
            "def add(a, b):\n"
            "    return a + b\n\n"
            "def double_add(a, b):\n"
            "    return add(a, b) * 2\n\n"
            "def outer():\n"
            "    fn = double_add\n"
            "    @trace\n"
            "    def inner(a, b):\n"
            "        return fn(a, b)\n"
            "    return inner\n"
        ),
    )
    func = mod.outer()
    source = reconstruct(serialize(), "inner")
    assert "def add(a, b):" in source
    assert "def double_add(a, b):" in source
    assert "def inner(" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["inner"](3, 4) == 14  # type: ignore[operator]


def test_closure_builtin_still_warns(tmp_path: Path) -> None:
    """Builtin callables captured in closure still produce warning."""
    mod = create_module(
        tmp_path,
        "cvbuiltin",
        (
            "from away import trace\n\n"
            "class Unpicklable:\n"
            "    def __reduce__(self):\n"
            "        raise TypeError('nope')\n"
            "    def __repr__(self):\n"
            "        return '<Unpicklable>'\n\n"
            "def outer():\n"
            "    obj = Unpicklable()\n"
            "    @trace\n"
            "    def inner():\n"
            "        return str(obj)\n"
            "    return inner\n"
        ),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mod.outer()

    msgs = [str(c.message) for c in caught]
    assert any("cannot be serialized" in m for m in msgs)


# ---------------------------------------------------------------------------
# Feature 2: Invalid repr fallback
# ---------------------------------------------------------------------------


def test_closure_defaultdict_constructor_expr(tmp_path: Path) -> None:
    """defaultdict captured via constructor expression."""
    mod = create_module(
        tmp_path,
        "cvdefdict",
        (
            "from collections import defaultdict\n"
            "from away import trace\n\n"
            "def outer():\n"
            "    d = defaultdict(int, {'a': 1})\n"
            "    @trace\n"
            "    def inner():\n"
            "        return d['a']\n"
            "    return inner\n"
        ),
    )
    func = mod.outer()

    node = get_graph().nodes["cvdefdict.outer.<locals>.inner"]
    assert "d" in node.closure_vars
    assert "defaultdict" in node.closure_vars["d"]

    source = reconstruct(serialize(), "inner")
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["inner"]() == 1  # type: ignore[operator]


def test_closure_counter_constructor_expr(tmp_path: Path) -> None:
    """Counter captured via constructor expression."""
    mod = create_module(
        tmp_path,
        "cvcounter",
        (
            "from collections import Counter\n"
            "from away import trace\n\n"
            "def outer():\n"
            "    c = Counter({'x': 3, 'y': 1})\n"
            "    @trace\n"
            "    def inner():\n"
            "        return c['x']\n"
            "    return inner\n"
        ),
    )
    func = mod.outer()

    node = get_graph().nodes["cvcounter.outer.<locals>.inner"]
    assert "c" in node.closure_vars
    assert "Counter" in node.closure_vars["c"]

    source = reconstruct(serialize(), "inner")
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["inner"]() == 3  # type: ignore[operator]


def test_closure_deque_constructor_expr(tmp_path: Path) -> None:
    """deque captured via constructor expression."""
    mod = create_module(
        tmp_path,
        "cvdeque",
        (
            "from collections import deque\n"
            "from away import trace\n\n"
            "def outer():\n"
            "    q = deque([1, 2, 3], maxlen=5)\n"
            "    @trace\n"
            "    def inner():\n"
            "        return list(q)\n"
            "    return inner\n"
        ),
    )
    func = mod.outer()

    node = get_graph().nodes["cvdeque.outer.<locals>.inner"]
    assert "q" in node.closure_vars
    assert "deque" in node.closure_vars["q"]
    assert "maxlen=5" in node.closure_vars["q"]

    source = reconstruct(serialize(), "inner")
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["inner"]() == [1, 2, 3]  # type: ignore[operator]


def test_closure_pickle_fallback_custom_class(tmp_path: Path) -> None:
    """Custom picklable class captured via pickle fallback."""
    mod = create_module(
        tmp_path,
        "cvpickle",
        (
            "from away import trace\n\n"
            "class Config:\n"
            "    def __init__(self, val):\n"
            "        self.val = val\n\n"
            "def outer():\n"
            "    cfg = Config(42)\n"
            "    @trace\n"
            "    def inner():\n"
            "        return cfg.val\n"
            "    return inner\n"
        ),
    )
    func = mod.outer()

    node = get_graph().nodes["cvpickle.outer.<locals>.inner"]
    assert "cfg" in node.closure_vars
    assert "__import__('pickle')" in node.closure_vars["cfg"]

# ---------------------------------------------------------------------------
# Nested function resolution
# ---------------------------------------------------------------------------


def test_nested_function_chain_via_closure(tmp_path: Path) -> None:
    """Nested functions referencing each other via closures are captured."""
    mod = create_module(
        tmp_path,
        "nested_chain",
        (
            "from away import trace\n\n"
            "def outer():\n"
            "    def step_a(x):\n"
            "        return x + 1\n\n"
            "    def step_b(x):\n"
            "        return step_a(x) * 2\n\n"
            "    @trace\n"
            "    def pipeline(x):\n"
            "        return step_b(x) + 10\n\n"
            "    return pipeline\n"
        ),
    )
    func = mod.outer()
    graph = get_graph()
    node = graph.nodes["nested_chain.outer.<locals>.pipeline"]
    # step_b should be captured as a closure func ref
    assert "step_b" in node.closure_func_refs

    # step_b itself should be auto-registered with step_a as its closure ref
    step_b_qname = node.closure_func_refs["step_b"]
    assert step_b_qname in graph.nodes
    step_b_node = graph.nodes[step_b_qname]
    assert "step_a" in step_b_node.closure_func_refs

    # Reconstructed code should be runnable
    source = reconstruct(serialize(), "pipeline")
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["pipeline"](5) == 22  # step_a(5)=6, step_b(5)=12, pipeline(5)=22


def test_nested_function_in_class_method(tmp_path: Path) -> None:
    """Nested functions inside a class method are resolved via closures."""
    mod = create_module(
        tmp_path,
        "nested_cls",
        (
            "from away import trace\n\n"
            "class MyClass:\n"
            "    def run(self):\n"
            "        def helper(x):\n"
            "            return x * 2\n\n"
            "        @trace\n"
            "        def compute(x):\n"
            "            return helper(x) + 1\n\n"
            "        return compute\n"
        ),
    )
    obj = mod.MyClass()
    func = obj.run()
    graph = get_graph()
    node = graph.nodes["nested_cls.MyClass.run.<locals>.compute"]
    assert "helper" in node.closure_func_refs

    source = reconstruct(serialize(), "compute")
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["compute"](5) == 11


# ---------------------------------------------------------------------------
# Inline import resolution
# ---------------------------------------------------------------------------


def test_inline_import_module_captured(tmp_path: Path) -> None:
    """Module from inline import in closure is captured as import statement."""
    mod = create_module(
        tmp_path,
        "inline_imp",
        (
            "from away import trace\n\n"
            "def outer():\n"
            "    import json as _json\n\n"
            "    @trace\n"
            "    def serialize(data):\n"
            "        return _json.dumps(data)\n\n"
            "    return serialize\n"
        ),
    )
    func = mod.outer()
    graph = get_graph()
    node = graph.nodes["inline_imp.outer.<locals>.serialize"]
    import_stmts = [imp.statement for imp in node.imports]
    assert any("json" in s for s in import_stmts)

    source = reconstruct(serialize(), "serialize")
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["serialize"]({"a": 1}) == '{"a": 1}'


def test_inline_import_aliased_module(tmp_path: Path) -> None:
    """Aliased inline import (import X as Y) generates correct statement."""
    mod = create_module(
        tmp_path,
        "inline_alias",
        (
            "from away import trace\n\n"
            "def outer():\n"
            "    import os.path as _osp\n\n"
            "    @trace\n"
            "    def check(p):\n"
            "        return _osp.exists(p)\n\n"
            "    return check\n"
        ),
    )
    func = mod.outer()
    graph = get_graph()
    node = graph.nodes["inline_alias.outer.<locals>.check"]
    import_stmts = [imp.statement for imp in node.imports]
    assert any("_osp" in s for s in import_stmts)

    source = reconstruct(serialize(), "check")
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["check"]("/") is True


def test_inline_import_same_name_module(tmp_path: Path) -> None:
    """Inline import with no alias (import json) captured correctly."""
    mod = create_module(
        tmp_path,
        "inline_same",
        (
            "from away import trace\n\n"
            "def outer():\n"
            "    import json\n\n"
            "    @trace\n"
            "    def dump(data):\n"
            "        return json.dumps(data)\n\n"
            "    return dump\n"
        ),
    )
    func = mod.outer()
    graph = get_graph()
    node = graph.nodes["inline_same.outer.<locals>.dump"]
    import_stmts = [imp.statement for imp in node.imports]
    assert "import json" in import_stmts

    source = reconstruct(serialize(), "dump")
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["dump"]([1, 2]) == "[1, 2]"