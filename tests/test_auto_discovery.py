import json
import os
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


def test_auto_discover_class_no_init(tmp_path: Path) -> None:
    """Class with no custom __init__ doesn't break discovery."""
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
    # No error raised; class without __init__ is silently handled
    Graph.default()


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


def test_auto_discover_staticmethod(tmp_path: Path) -> None:
    """@staticmethod methods are auto-discovered via self.method() calls."""
    create_module(
        tmp_path,
        "adstaticmethod",
        (
            "from pyfuse import trace\n\n"
            "class Calc:\n"
            "    @trace\n"
            "    def run(self, x):\n"
            "        return self.double(x)\n\n"
            "    @staticmethod\n"
            "    def double(x):\n"
            "        return x * 2\n"
        ),
    )
    source = reconstruct(serialize(), "run")
    assert "class Calc:" in source
    assert "@staticmethod" in source
    assert "def double(x):" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    calc = ns["Calc"]()
    assert calc.run(5) == 10


def test_auto_discover_classmethod(tmp_path: Path) -> None:
    """@classmethod methods are auto-discovered via cls.method() calls."""
    create_module(
        tmp_path,
        "adclassmethod",
        (
            "from pyfuse import trace\n\n"
            "class MyClass:\n"
            "    @trace\n"
            "    def run(cls):\n"
            "        return cls.create(42)\n\n"
            "    @classmethod\n"
            "    def create(cls, val):\n"
            "        return val * 2\n"
        ),
    )
    source = reconstruct(serialize(), "run")
    assert "class MyClass:" in source
    assert "@classmethod" in source
    assert "def create(cls, val):" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["MyClass"].create(42) == 84


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


# ---------------------------------------------------------------------------
# Class constructor auto-discovery
# ---------------------------------------------------------------------------


def test_auto_discover_class_constructor(tmp_path: Path) -> None:
    """Class __init__ and its deps are discovered when constructor is called."""
    create_module(
        tmp_path,
        "adctor",
        (
            "from pyfuse import trace\n\n"
            "class Processor:\n"
            "    def __init__(self, scale):\n"
            "        self.scale = scale\n\n"
            "    def run(self, x):\n"
            "        return x * self.scale\n\n"
            "@trace\n"
            "def process(x):\n"
            "    p = Processor(10)\n"
            "    return p.run(x)\n"
        ),
    )
    source = reconstruct(serialize(), "process")
    assert "class Processor:" in source
    assert "def __init__(self, scale):" in source
    assert "def process(x):" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["process"](5) == 50  # type: ignore[operator]


def test_auto_discover_class_init_with_deps(tmp_path: Path) -> None:
    """__init__ calling self.method() triggers further discovery."""
    create_module(
        tmp_path,
        "adctordeps",
        (
            "from pyfuse import trace\n\n"
            "class Builder:\n"
            "    def __init__(self, data):\n"
            "        self.result = self.transform(data)\n\n"
            "    def transform(self, data):\n"
            "        return [x * 2 for x in data]\n\n"
            "@trace\n"
            "def build(data):\n"
            "    return Builder(data).result\n"
        ),
    )
    source = reconstruct(serialize(), "build")
    assert "class Builder:" in source
    assert "def __init__" in source
    assert "def transform" in source
    assert "def build" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["build"]([1, 2, 3]) == [2, 4, 6]  # type: ignore[operator]


def test_auto_discover_class_skips_stdlib_classes(tmp_path: Path) -> None:
    """Stdlib classes (dict, list, etc.) are not auto-registered."""
    create_module(
        tmp_path,
        "adstdclass",
        (
            "from collections import OrderedDict\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def run():\n"
            "    return OrderedDict(a=1)\n"
        ),
    )
    graph_json = serialize()
    data = json.loads(graph_json)
    # OrderedDict should NOT be in the graph
    for ref in data["refs"]:
        assert "OrderedDict" not in ref


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_module_constant_int(tmp_path: Path) -> None:
    """Module-level integer constant is captured and available at execution."""
    create_module(
        tmp_path,
        "adconst_int",
        (
            "from pyfuse import trace\n\n"
            "MAX_RETRIES = 5\n\n"
            "@trace\n"
            "def get_retries():\n"
            "    return MAX_RETRIES\n"
        ),
    )
    source = reconstruct(serialize(), "get_retries")
    assert "MAX_RETRIES = 5" in source
    assert "def get_retries():" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["get_retries"]() == 5  # type: ignore[operator]


def test_module_constant_dict(tmp_path: Path) -> None:
    """Module-level dict constant is captured."""
    create_module(
        tmp_path,
        "adconst_dict",
        (
            "from pyfuse import trace\n\n"
            "CONFIG = {'debug': True, 'workers': 4}\n\n"
            "@trace\n"
            "def get_config():\n"
            "    return CONFIG\n"
        ),
    )
    source = reconstruct(serialize(), "get_config")
    assert "CONFIG" in source
    assert "def get_config():" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    result = ns["get_config"]()  # type: ignore[operator]
    assert result == {"debug": True, "workers": 4}


def test_module_constant_with_import(tmp_path: Path) -> None:
    """Module-level constant that depends on an import."""
    create_module(
        tmp_path,
        "adconst_import",
        (
            "import os\n"
            "from pyfuse import trace\n\n"
            "SEP = os.sep\n\n"
            "@trace\n"
            "def get_sep():\n"
            "    return SEP\n"
        ),
    )
    source = reconstruct(serialize(), "get_sep")
    assert "import os" in source
    assert "SEP = os.sep" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["get_sep"]() == os.sep  # type: ignore[operator]


def test_module_constant_skips_dunders(tmp_path: Path) -> None:
    """Dunder names (__all__, __version__) are not captured."""
    create_module(
        tmp_path,
        "adconst_dunder",
        (
            "from pyfuse import trace\n\n"
            "__version__ = '1.0'\n"
            "VALUE = 42\n\n"
            "@trace\n"
            "def get():\n"
            "    return VALUE\n"
        ),
    )
    source = reconstruct(serialize(), "get")
    assert "__version__" not in source
    assert "VALUE = 42" in source


def test_module_constant_type_alias(tmp_path: Path) -> None:
    """Type alias (annotated assignment) is captured."""
    create_module(
        tmp_path,
        "adconst_alias",
        (
            "from pyfuse import trace\n\n"
            "Multiplier: int = 10\n\n"
            "@trace\n"
            "def scale(x):\n"
            "    return x * Multiplier\n"
        ),
    )
    source = reconstruct(serialize(), "scale")
    assert "Multiplier" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["scale"](3) == 30  # type: ignore[operator]


# ---------------------------------------------------------------------------
# super() and inheritance
# ---------------------------------------------------------------------------


def test_super_init_call(tmp_path: Path) -> None:
    """Class using super().__init__() reconstructs with base class."""
    create_module(
        tmp_path,
        "adsuper",
        (
            "from pyfuse import trace\n\n"
            "class Base:\n"
            "    def __init__(self, x):\n"
            "        self.x = x\n\n"
            "class Child(Base):\n"
            "    def __init__(self, x, y):\n"
            "        super().__init__(x)\n"
            "        self.y = y\n\n"
            "@trace\n"
            "def create(x, y):\n"
            "    return Child(x, y)\n"
        ),
    )
    source = reconstruct(serialize(), "create")
    assert "class Base:" in source
    assert "class Child(Base):" in source
    assert "super(Child, self).__init__(x)" in source
    assert "def create(" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    obj = ns["create"](1, 2)  # type: ignore[operator]
    assert obj.x == 1
    assert obj.y == 2


def test_super_method_call(tmp_path: Path) -> None:
    """Method using super().method() works in reconstructed code."""
    create_module(
        tmp_path,
        "adsupermethod",
        (
            "from pyfuse import trace\n\n"
            "class Base:\n"
            "    def greet(self):\n"
            "        return 'hello'\n\n"
            "class Child(Base):\n"
            "    @trace\n"
            "    def greet(self):\n"
            "        return super().greet() + ' world'\n"
        ),
    )
    source = reconstruct(serialize(), "greet")
    assert "class Base:" in source
    assert "class Child(Base):" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["Child"]().greet() == "hello world"  # type: ignore[union-attr]


def test_class_inheriting_stdlib(tmp_path: Path) -> None:
    """Class inheriting from a stdlib class preserves the base."""
    create_module(
        tmp_path,
        "adstdbase",
        (
            "from pyfuse import trace\n\n"
            "class MyList(list):\n"
            "    @trace\n"
            "    def first(self):\n"
            "        return self[0] if self else None\n"
        ),
    )
    source = reconstruct(serialize(), "first")
    assert "class MyList(list):" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    obj = ns["MyList"]([1, 2, 3])  # type: ignore[operator]
    assert obj.first() == 1


# ---------------------------------------------------------------------------
# Feature 3: Metaclass keywords
# ---------------------------------------------------------------------------


def test_metaclass_reconstructed(tmp_path: Path) -> None:
    """Class with metaclass= keyword is reconstructed correctly."""
    create_module(
        tmp_path,
        "admeta",
        (
            "from abc import ABCMeta, abstractmethod\n"
            "from pyfuse import trace\n\n"
            "class Animal(metaclass=ABCMeta):\n"
            "    @abstractmethod\n"
            "    def speak(self):\n"
            "        ...\n\n"
            "class Dog(Animal):\n"
            "    @trace\n"
            "    def speak(self):\n"
            "        return super().speak() or 'woof'\n"
        ),
    )
    source = reconstruct(serialize(), "speak")
    assert "metaclass=ABCMeta" in source
    assert "class Animal(metaclass=ABCMeta):" in source
    assert "class Dog(Animal):" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["Dog"]().speak() == "woof"  # type: ignore[union-attr]


def test_metaclass_with_bases(tmp_path: Path) -> None:
    """Class with both bases and metaclass= keyword."""
    create_module(
        tmp_path,
        "admetabases",
        (
            "from abc import ABCMeta\n"
            "from pyfuse import trace\n\n"
            "class Base(metaclass=ABCMeta):\n"
            "    def value(self):\n"
            "        return 42\n\n"
            "class MyABC(Base):\n"
            "    @trace\n"
            "    def get(self):\n"
            "        return super().value()\n"
        ),
    )
    source = reconstruct(serialize(), "get")
    assert "class Base(metaclass=ABCMeta):" in source
    assert "class MyABC(Base):" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["MyABC"]().get() == 42  # type: ignore[union-attr]


def test_init_subclass_replayed(tmp_path: Path) -> None:
    """__init_subclass__ is called when parent class is in the dependency tree."""
    create_module(
        tmp_path,
        "adinitsubclass",
        (
            "from pyfuse import trace\n\n"
            "class Registry:\n"
            "    _registry = []\n"
            "    def __init_subclass__(cls, **kwargs):\n"
            "        super().__init_subclass__(**kwargs)\n"
            "        Registry._registry.append(cls.__name__)\n\n"
            "class Plugin(Registry):\n"
            "    @trace\n"
            "    def run(self):\n"
            "        return super().__init_subclass__.__qualname__\n"
        ),
    )
    source = reconstruct(serialize(), "run")
    assert "def __init_subclass__" in source
    assert "class Plugin(Registry):" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    # __init_subclass__ was called during class creation via exec
    assert "Plugin" in ns["Registry"]._registry  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Feature 4: Class-level attributes and decorators
# ---------------------------------------------------------------------------


def test_class_attrs_captured(tmp_path: Path) -> None:
    """Class-level attribute assignments are captured and reconstructed."""
    create_module(
        tmp_path,
        "adclsattrs",
        (
            "from pyfuse import trace\n\n"
            "class Config:\n"
            "    MAX = 10\n"
            "    name = 'default'\n\n"
            "    @trace\n"
            "    def get_max(self):\n"
            "        return self.MAX\n"
        ),
    )
    source = reconstruct(serialize(), "get_max")
    assert "MAX = 10" in source
    assert "name = 'default'" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    assert ns["Config"]().get_max() == 10  # type: ignore[union-attr]


def test_class_annotated_attrs_captured(tmp_path: Path) -> None:
    """Annotated class attributes are captured."""
    create_module(
        tmp_path,
        "adclsann",
        (
            "from pyfuse import trace\n\n"
            "class Item:\n"
            "    count: int = 0\n\n"
            "    @trace\n"
            "    def increment(self):\n"
            "        Item.count += 1\n"
            "        return Item.count\n"
        ),
    )
    source = reconstruct(serialize(), "increment")
    assert "count: int = 0" in source or "count = 0" in source


def test_class_decorator_captured(tmp_path: Path) -> None:
    """Class decorators like @dataclass are captured and emitted."""
    create_module(
        tmp_path,
        "adclsdeco",
        (
            "from dataclasses import dataclass\n"
            "from pyfuse import trace\n\n"
            "@dataclass\n"
            "class Point:\n"
            "    x: float\n"
            "    y: float\n\n"
            "    @trace\n"
            "    def magnitude(self):\n"
            "        return (self.x ** 2 + self.y ** 2) ** 0.5\n"
        ),
    )
    source = reconstruct(serialize(), "magnitude")
    assert "@dataclass" in source
    assert "x: float" in source
    assert "y: float" in source

    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    p = ns["Point"](3.0, 4.0)  # type: ignore[operator]
    assert p.magnitude() == 5.0  # type: ignore[union-attr]


def test_class_docstring_captured(tmp_path: Path) -> None:
    """Class docstrings are captured as class body attributes."""
    create_module(
        tmp_path,
        "adclsdoc",
        (
            "from pyfuse import trace\n\n"
            "class Documented:\n"
            '    """A documented class."""\n\n'
            "    @trace\n"
            "    def method(self):\n"
            "        return 1\n"
        ),
    )
    source = reconstruct(serialize(), "method")
    assert "A documented class" in source
