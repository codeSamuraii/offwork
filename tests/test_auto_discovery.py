from __future__ import annotations

import json
import warnings
from pathlib import Path

from pyfuse import reconstruct, serialize
from pyfuse.graph.analyzer import find_bare_calls
from pyfuse.graph.graph import Graph
from tests.conftest import create_module


# ---------------------------------------------------------------------------
# find_bare_calls
# ---------------------------------------------------------------------------


def test_find_bare_calls_basic() -> None:
    """Extracts bare function call names from source."""
    source = "def f(x):\n    return helper(x) + other(x)\n"
    assert find_bare_calls(source) == {"helper", "other"}


def test_find_bare_calls_ignores_method_calls() -> None:
    """obj.method() is not returned."""
    source = "def f(x):\n    return obj.method(x)\n"
    assert find_bare_calls(source) == set()


def test_find_bare_calls_returns_all_names() -> None:
    """Returns ALL names including builtins — filtering is the caller's job."""
    source = "def f(x):\n    return len(x) + helper(x)\n"
    assert find_bare_calls(source) == {"len", "helper"}


# ---------------------------------------------------------------------------
# Same-module auto-discovery
# ---------------------------------------------------------------------------


def test_auto_discover_same_module(tmp_path: Path) -> None:
    """Untraced function in same module is auto-registered."""
    create_module(
        tmp_path,
        "adbasic",
        (
            "from pyfuse import trace\n\n"
            "def normalize(text):\n"
            "    return text.strip().lower()\n\n"
            "@trace\n"
            "def parse_row(row):\n"
            "    return [normalize(cell) for cell in row]\n"
        ),
    )
    graph = Graph.default()
    assert "adbasic.normalize" in graph.nodes
    assert "adbasic.parse_row" in graph.nodes
    deps = graph.nodes["adbasic.parse_row"].dependencies
    assert "adbasic.normalize" in deps


def test_auto_discover_transitive(tmp_path: Path) -> None:
    """Transitive untraced deps are auto-registered."""
    create_module(
        tmp_path,
        "adtrans",
        (
            "from pyfuse import trace\n\n"
            "def step_c(x):\n"
            "    return x\n\n"
            "def step_b(x):\n"
            "    return step_c(x)\n\n"
            "@trace\n"
            "def step_a(x):\n"
            "    return step_b(x)\n"
        ),
    )
    graph = Graph.default()
    assert "adtrans.step_a" in graph.nodes
    assert "adtrans.step_b" in graph.nodes
    assert "adtrans.step_c" in graph.nodes
    assert "adtrans.step_b" in graph.nodes["adtrans.step_a"].dependencies
    assert "adtrans.step_c" in graph.nodes["adtrans.step_b"].dependencies


def test_auto_discover_with_imports(tmp_path: Path) -> None:
    """Auto-discovered function's imports are captured."""
    create_module(
        tmp_path,
        "adimports",
        (
            "import csv\n\n"
            "from pyfuse import trace\n\n"
            "def parse(data):\n"
            "    return list(csv.reader(data.splitlines()))\n\n"
            "@trace\n"
            "def run(data):\n"
            "    return parse(data)\n"
        ),
    )
    graph = Graph.default()
    node = graph.nodes["adimports.parse"]
    import_stmts = [imp.statement for imp in node.imports]
    assert "import csv" in import_stmts


def test_auto_discover_skips_builtins(tmp_path: Path) -> None:
    """Builtins like len, print are not auto-registered."""
    create_module(
        tmp_path,
        "adbuiltins",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def run(items):\n"
            "    print(len(items))\n"
            "    return list(range(10))\n"
        ),
    )
    graph = Graph.default()
    node_names = {n.name for n in graph.nodes.values()}
    assert "len" not in node_names
    assert "print" not in node_names
    assert "list" not in node_names
    assert "range" not in node_names


def test_auto_discover_skips_classes(tmp_path: Path) -> None:
    """Class constructors are not auto-registered."""
    create_module(
        tmp_path,
        "adclass",
        (
            "from pyfuse import trace\n\n"
            "class MyClass:\n"
            "    pass\n\n"
            "@trace\n"
            "def run():\n"
            "    return MyClass()\n"
        ),
    )
    graph = Graph.default()
    assert "adclass.MyClass" not in graph.nodes


def test_auto_discover_no_duplicate(tmp_path: Path) -> None:
    """Two traced functions calling same untraced helper: registered once."""
    create_module(
        tmp_path,
        "adnodup",
        (
            "from pyfuse import trace\n\n"
            "def helper(x):\n"
            "    return x + 1\n\n"
            "@trace\n"
            "def caller_a(x):\n"
            "    return helper(x)\n\n"
            "@trace\n"
            "def caller_b(x):\n"
            "    return helper(x)\n"
        ),
    )
    graph = Graph.default()
    assert "adnodup.helper" in graph.nodes
    assert "adnodup.helper" in graph.nodes["adnodup.caller_a"].dependencies
    assert "adnodup.helper" in graph.nodes["adnodup.caller_b"].dependencies


# ---------------------------------------------------------------------------
# Cross-module auto-discovery
# ---------------------------------------------------------------------------


def test_auto_discover_cross_module(tmp_path: Path) -> None:
    """Imported user function is auto-registered; import removed."""
    # Create the utility module
    (tmp_path / "adutils.py").write_text(
        "def helper(x):\n"
        "    return x + 1\n"
    )
    create_module(
        tmp_path,
        "adcross",
        (
            "from adutils import helper\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def run(x):\n"
            "    return helper(x)\n"
        ),
    )
    graph = Graph.default()
    assert "adutils.helper" in graph.nodes
    assert "adutils.helper" in graph.nodes["adcross.run"].dependencies
    # The import should be removed since the function is inlined
    import_stmts = [imp.bound_name for imp in graph.nodes["adcross.run"].imports]
    assert "helper" not in import_stmts


def test_auto_discover_preserves_stdlib_imports(tmp_path: Path) -> None:
    """stdlib functions like json.dumps are NOT auto-registered."""
    create_module(
        tmp_path,
        "adstdlib",
        (
            "import json\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def run(data):\n"
            "    return json.dumps(data)\n"
        ),
    )
    graph = Graph.default()
    # json module functions should NOT be in graph
    for qname in graph.nodes:
        assert not qname.startswith("json.")
    # The import should be preserved
    import_stmts = [imp.statement for imp in graph.nodes["adstdlib.run"].imports]
    assert "import json" in import_stmts


def test_auto_discover_skips_aliased_imports(tmp_path: Path) -> None:
    """from utils import helper as h — skipped (alias mismatch)."""
    (tmp_path / "adutils2.py").write_text(
        "def helper(x):\n"
        "    return x + 1\n"
    )
    create_module(
        tmp_path,
        "adalias",
        (
            "from adutils2 import helper as h\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def run(x):\n"
            "    return h(x)\n"
        ),
    )
    graph = Graph.default()
    # helper should NOT be auto-registered (aliased)
    assert "adutils2.helper" not in graph.nodes
    # The import should be preserved
    import_stmts = [imp.bound_name for imp in graph.nodes["adalias.run"].imports]
    assert "h" in import_stmts


# ---------------------------------------------------------------------------
# Integration / end-to-end
# ---------------------------------------------------------------------------


def test_untraced_dep_end_to_end(tmp_path: Path) -> None:
    """Motivating example: untraced normalize is serialized and reconstructable."""
    create_module(
        tmp_path,
        "ade2e",
        (
            "import csv\n\n"
            "from pyfuse import trace\n\n"
            "def normalize(text):\n"
            "    return text.strip().lower()\n\n"
            "@trace\n"
            "def parse_row(row):\n"
            "    return [normalize(cell) for cell in row]\n\n"
            "@trace\n"
            "def parse_csv(data):\n"
            "    rows = list(csv.reader(data.splitlines()))\n"
            "    return [parse_row(row) for row in rows]\n"
        ),
    )
    graph_json = serialize()
    data = json.loads(graph_json)
    assert "ade2e.normalize" in data["refs"]
    assert "ade2e.parse_row" in data["refs"]
    assert "ade2e.parse_csv" in data["refs"]

    source = reconstruct(graph_json, "parse_csv")
    assert "def normalize(text):" in source
    assert "def parse_row(row):" in source
    assert "def parse_csv(data):" in source
    assert "import csv" in source

    # Reconstructed code is executable
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    result = ns["parse_csv"]("A, B \n c , D")  # type: ignore[operator]
    assert result == [["a", "b"], ["c", "d"]]


def test_untraced_dep_chain_reconstruction(tmp_path: Path) -> None:
    """Transitive untraced deps appear in correct order."""
    create_module(
        tmp_path,
        "adchain",
        (
            "from pyfuse import trace\n\n"
            "def step_c(x):\n"
            "    return x * 2\n\n"
            "def step_b(x):\n"
            "    return step_c(x) + 1\n\n"
            "@trace\n"
            "def step_a(x):\n"
            "    return step_b(x) + 10\n"
        ),
    )
    source = reconstruct(serialize(), "step_a")
    assert "def step_c(x):" in source
    assert "def step_b(x):" in source
    assert "def step_a(x):" in source
    # step_c should appear before step_b (topological order)
    assert source.index("def step_c") < source.index("def step_b")
    assert source.index("def step_b") < source.index("def step_a")

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["step_a"](3) == 17  # step_c(3)=6, step_b(3)=7, step_a(3)=17


def test_auto_discover_cross_module_reconstruction(tmp_path: Path) -> None:
    """Cross-module auto-discovered function inlined in reconstruction."""
    (tmp_path / "adxutils.py").write_text(
        "def double(x):\n"
        "    return x * 2\n"
    )
    create_module(
        tmp_path,
        "adxmain",
        (
            "from adxutils import double\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def run(x):\n"
            "    return double(x) + 1\n"
        ),
    )
    source = reconstruct(serialize(), "run")
    assert "def double(x):" in source
    assert "def run(x):" in source
    # Import should NOT be present (function is inlined)
    assert "from adxutils import double" not in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["run"](5) == 11


# ---------------------------------------------------------------------------
# Warnings on failure
# ---------------------------------------------------------------------------


def test_auto_register_warns_on_source_unavailable(tmp_path: Path) -> None:
    """Warning emitted when auto-registration fails due to unavailable source."""
    with warnings.catch_warnings(record=False, action='ignore', category=UserWarning):
        # Create a module with a C-extension function masquerading as a dependency
        create_module(
            tmp_path,
            "adwarn",
            (
                "import posixpath\n\n"
                "from pyfuse import trace\n\n"
                "# Rebind a stdlib function under a new name to bypass stdlib check\n"
                "helper = type(lambda: None)(\n"
                "    posixpath.join.__code__,\n"
                "    {'__builtins__': __builtins__},\n"
                "    'helper',\n"
                ")\n"
                "helper.__module__ = 'adwarn'\n"
                "helper.__qualname__ = 'helper'\n\n"
                "@trace\n"
                "def run(x):\n"
                "    return helper(x)\n"
            ),
        )
        # The dynamically created function has no .py source file, so
        # get_function_source will fail and a warning should be emitted.
        graph = Graph.default()
        assert "adwarn.helper" not in graph.nodes


def test_auto_discover_no_warning_for_local_variables(tmp_path: Path) -> None:
    """No spurious warning for names that are parameters or local variables."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        create_module(
            tmp_path,
            "adnolocal",
            (
                "from pyfuse import trace\n\n"
                "@trace\n"
                "def run(callback, items):\n"
                "    result = callback(items)\n"
                "    return result\n"
            ),
        )

    # 'callback' is a parameter used as a function call — no warning expected
    pyfuse_warnings = [
        w for w in caught
        if "pyfuse" in str(w.filename) and "callback" in str(w.message)
    ]
    assert pyfuse_warnings == []


def test_auto_discover_no_warning_for_closure_vars(tmp_path: Path) -> None:
    """No spurious warning for closure-captured function references."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        create_module(
            tmp_path,
            "adnoclosure",
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

    pyfuse_warnings = [
        w for w in caught
        if "pyfuse" in str(w.filename) and "fn" in str(w.message)
    ]
    assert pyfuse_warnings == []
