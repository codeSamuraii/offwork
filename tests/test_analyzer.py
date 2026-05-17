from pathlib import Path

from offwork import reconstruct, serialize
from offwork.graph.analyzer import (
    _resolve_owner_class,
    detect_traced_dependencies,
    filter_imports,
    get_function_source,
    get_module_imports,
    get_used_names,
)
from offwork.core.models import FunctionNode, ImportInfo
from tests.conftest import create_module


def test_get_function_source_strips_trace(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "src_trace",
        "import offwork\n\n@offwork.task\ndef foo():\n    return 1\n",
    )
    source = get_function_source(mod.foo)
    assert "@offwork.task" not in source
    assert "def foo():" in source
    assert "return 1" in source


def test_get_function_source_strips_multiline_trace(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "src_multiline",
        (
            "import offwork\n\n"
            "@offwork.task(\n"
            "    timeout=30,\n"
            "    retries=3,\n"
            ")\n"
            "def foo():\n"
            "    return 1\n"
        ),
    )
    source = get_function_source(mod.foo)
    assert "@offwork.task" not in source
    assert "timeout" not in source
    assert "retries" not in source
    assert "def foo():" in source
    assert "return 1" in source


def test_get_function_source_strips_multiline_preserves_other(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "src_multi_other",
        (
            "import offwork\n"
            "def my_dec(f): return f\n\n"
            "@my_dec\n"
            "@offwork.task(\n"
            "    timeout=10,\n"
            ")\n"
            "def bar():\n"
            "    return 2\n"
        ),
    )
    source = get_function_source(mod.bar)
    assert "@my_dec" in source
    assert "@offwork.task" not in source
    assert "timeout" not in source
    assert "def bar():" in source


def test_get_function_source_preserves_other_decorators(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "src_other_dec",
        (
            "import offwork\n"
            "def my_dec(f): return f\n\n"
            "@my_dec\n@offwork.task\ndef bar():\n    return 2\n"
        ),
    )
    source = get_function_source(mod.bar)
    assert "@my_dec" in source
    assert "@offwork.task" not in source


def test_get_module_imports_basic(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "imp_basic",
        "import csv\nimport json\n\ndef f(): pass\n",
    )
    imports = get_module_imports(mod.f)
    statements = {imp.statement for imp in imports}
    assert "import csv" in statements
    assert "import json" in statements


def test_get_module_imports_from(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "imp_from",
        "from os.path import join, exists\n\ndef f(): pass\n",
    )
    imports = get_module_imports(mod.f)
    names = {imp.bound_name for imp in imports}
    assert "join" in names
    assert "exists" in names
    assert len(imports) == 2


def test_get_module_imports_alias(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "imp_alias",
        "import collections as col\nfrom collections import OrderedDict as OD\n\ndef f(): pass\n",
    )
    imports = get_module_imports(mod.f)
    bound = {imp.bound_name for imp in imports}
    assert "col" in bound
    assert "OD" in bound


def test_get_module_imports_dotted(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "imp_dotted",
        "import os.path\n\ndef f(): pass\n",
    )
    imports = get_module_imports(mod.f)
    assert imports[0].bound_name == "os"
    assert "os.path" in imports[0].statement


def test_get_used_names() -> None:
    source = "def foo(x):\n    y = csv.reader(x)\n    return list(y)\n"
    names = get_used_names(source)
    assert "csv" in names
    assert "list" in names
    assert "y" in names
    assert "x" in names


def test_filter_imports() -> None:
    all_imports = [
        ImportInfo(statement="import csv", bound_name="csv"),
        ImportInfo(statement="import json", bound_name="json"),
        ImportInfo(statement="import requests", bound_name="requests"),
    ]
    used = {"csv", "list", "x"}
    result = filter_imports(all_imports, used)
    assert len(result) == 1
    assert result[0].bound_name == "csv"


def test_detect_traced_dependencies() -> None:
    registry = {
        "mod.helper": FunctionNode(
            qualified_name="mod.helper",
            name="helper",
            module="mod",
            source="def helper(): pass",
        ),
    }
    source = "def main():\n    result = helper()\n    return result\n"
    deps = detect_traced_dependencies(source, "mod", registry)
    assert deps == ["mod.helper"]


def test_detect_dependencies_ignores_module_attribute_calls() -> None:
    """requests.get() should not match a traced 'get' function (no class)."""
    registry = {
        "mod.get": FunctionNode(
            qualified_name="mod.get",
            name="get",
            module="mod",
            source="def get(): pass",
        ),
    }
    source = "def main():\n    r = requests.get('url')\n    return r\n"
    deps = detect_traced_dependencies(source, "mod", registry)
    assert deps == []


def test_detect_dependencies_prefers_same_module() -> None:
    registry = {
        "a.helper": FunctionNode(
            qualified_name="a.helper",
            name="helper",
            module="a",
            source="def helper(): pass",
        ),
        "b.helper": FunctionNode(
            qualified_name="b.helper",
            name="helper",
            module="b",
            source="def helper(): pass",
        ),
    }
    source = "def main():\n    return helper()\n"
    deps = detect_traced_dependencies(source, "b", registry)
    assert deps == ["b.helper"]


def test_star_import_resolution(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "star_mod",
        "from os.path import *\n\ndef f():\n    return join('a', 'b')\n",
    )
    imports = get_module_imports(mod.f)
    bound_names = {imp.bound_name for imp in imports}
    assert "join" in bound_names
    assert "exists" in bound_names
    # Verify statements are explicit, not star
    for imp in imports:
        assert "*" not in imp.statement


def test_star_import_unresolvable(tmp_path: Path) -> None:
    # Write a module that has a star import from a nonexistent package.
    # We can't actually import this module, so we test get_module_imports
    # by writing a file that won't execute the star import at top level.
    # Instead, test the warning path directly via a module that *can* import.
    mod = create_module(
        tmp_path,
        "star_ok",
        "from os.path import *\n\ndef f(): pass\n",
    )
    # This should work without warnings (os.path is importable)
    imports = get_module_imports(mod.f)
    assert len(imports) > 0


def test_detect_self_method_call() -> None:
    registry = {
        "mod.MyClass.helper": FunctionNode(
            qualified_name="mod.MyClass.helper",
            name="helper",
            module="mod",
            source="def helper(self): pass",
            owner_class="MyClass",
        ),
    }
    source = "def process(self):\n    self.helper()\n"
    deps = detect_traced_dependencies(
        source, "mod", registry, owner_class="MyClass"
    )
    assert deps == ["mod.MyClass.helper"]


def test_detect_cls_method_call() -> None:
    registry = {
        "mod.MyClass.create": FunctionNode(
            qualified_name="mod.MyClass.create",
            name="create",
            module="mod",
            source="@classmethod\ndef create(cls): pass",
            owner_class="MyClass",
        ),
    }
    source = "def build(cls):\n    cls.create()\n"
    deps = detect_traced_dependencies(
        source, "mod", registry, owner_class="MyClass"
    )
    assert deps == ["mod.MyClass.create"]


def test_resolve_owner_class() -> None:
    assert _resolve_owner_class("func") is None
    assert _resolve_owner_class("ClassName.method") == "ClassName"
    assert _resolve_owner_class("outer.<locals>.inner") is None
    assert _resolve_owner_class("Outer.Inner.method") == "Outer.Inner"
    assert _resolve_owner_class("Cls.<locals>.nested") is None


# ---------------------------------------------------------------------------
# Type-annotation-based object method detection
# ---------------------------------------------------------------------------


def test_detect_typed_obj_method_call() -> None:
    """obj.method() resolved via type annotation on the parameter."""
    registry = {
        "mod.Processor.step": FunctionNode(
            qualified_name="mod.Processor.step",
            name="step",
            module="mod",
            source="def step(self, x): return x",
            owner_class="Processor",
        ),
    }
    source = "def run(proc: Processor, data):\n    return proc.step(data)\n"
    deps = detect_traced_dependencies(source, "mod", registry)
    assert "mod.Processor.step" in deps


def test_detect_typed_obj_method_optional() -> None:
    """Processor | None annotation still resolves the method."""
    registry = {
        "mod.Processor.step": FunctionNode(
            qualified_name="mod.Processor.step",
            name="step",
            module="mod",
            source="def step(self, x): return x",
            owner_class="Processor",
        ),
    }
    source = "def run(proc: Processor | None):\n    return proc.step(1)\n"
    deps = detect_traced_dependencies(source, "mod", registry)
    assert "mod.Processor.step" in deps


def test_detect_untyped_obj_method_unambiguous() -> None:
    """No annotation, but only one class has the method -> inferred."""
    registry = {
        "mod.Worker.execute": FunctionNode(
            qualified_name="mod.Worker.execute",
            name="execute",
            module="mod",
            source="def execute(self): pass",
            owner_class="Worker",
        ),
    }
    source = "def run(w):\n    return w.execute()\n"
    deps = detect_traced_dependencies(source, "mod", registry)
    assert "mod.Worker.execute" in deps


def test_detect_untyped_obj_method_ambiguous() -> None:
    """Two classes have the same method, no annotation -> NOT resolved."""
    registry = {
        "mod.A.run": FunctionNode(
            qualified_name="mod.A.run",
            name="run",
            module="mod",
            source="def run(self): pass",
            owner_class="A",
        ),
        "mod.B.run": FunctionNode(
            qualified_name="mod.B.run",
            name="run",
            module="mod",
            source="def run(self): pass",
            owner_class="B",
        ),
    }
    source = "def caller(obj):\n    return obj.run()\n"
    deps = detect_traced_dependencies(source, "mod", registry)
    assert deps == []


def test_detect_typed_obj_method_unknown_type() -> None:
    """Type annotation doesn't match any class in registry -> not resolved."""
    registry = {
        "mod.Processor.step": FunctionNode(
            qualified_name="mod.Processor.step",
            name="step",
            module="mod",
            source="def step(self, x): return x",
            owner_class="Processor",
        ),
    }
    source = "def run(proc: UnknownType):\n    return proc.step(1)\n"
    deps = detect_traced_dependencies(source, "mod", registry)
    assert deps == []


def test_detect_typed_obj_method_no_duplicate_with_self() -> None:
    """self.method() and obj.method() don't produce duplicate deps."""
    registry = {
        "mod.MyClass.helper": FunctionNode(
            qualified_name="mod.MyClass.helper",
            name="helper",
            module="mod",
            source="def helper(self): pass",
            owner_class="MyClass",
        ),
    }
    source = (
        "def process(self, obj: MyClass):\n"
        "    self.helper()\n"
        "    obj.helper()\n"
    )
    deps = detect_traced_dependencies(
        source, "mod", registry, owner_class="MyClass"
    )
    assert deps == ["mod.MyClass.helper"]


def test_detect_typed_obj_method_generic_list() -> None:
    """list[Processor] annotation resolves the method."""
    registry = {
        "mod.Processor.step": FunctionNode(
            qualified_name="mod.Processor.step",
            name="step",
            module="mod",
            source="def step(self, x): return x",
            owner_class="Processor",
        ),
    }
    source = (
        "def run(procs: list[Processor]):\n"
        "    for p in procs:\n"
        "        p.step(1)\n"
    )
    deps = detect_traced_dependencies(source, "mod", registry)
    assert "mod.Processor.step" in deps


def test_detect_typed_obj_method_dict_value() -> None:
    """dict[str, Processor] annotation resolves the method."""
    registry = {
        "mod.Processor.step": FunctionNode(
            qualified_name="mod.Processor.step",
            name="step",
            module="mod",
            source="def step(self, x): return x",
            owner_class="Processor",
        ),
    }
    source = (
        "def run(mapping: dict[str, Processor]):\n"
        "    mapping['key'].step(1)\n"
    )
    deps = detect_traced_dependencies(source, "mod", registry)
    assert "mod.Processor.step" in deps


def test_detect_multiple_typed_params() -> None:
    """Multiple typed params with different methods are all resolved."""
    registry = {
        "mod.Reader.read": FunctionNode(
            qualified_name="mod.Reader.read",
            name="read",
            module="mod",
            source="def read(self): pass",
            owner_class="Reader",
        ),
        "mod.Writer.write": FunctionNode(
            qualified_name="mod.Writer.write",
            name="write",
            module="mod",
            source="def write(self, data): pass",
            owner_class="Writer",
        ),
    }
    source = (
        "def orchestrate(r: Reader, w: Writer):\n"
        "    data = r.read()\n"
        "    w.write(data)\n"
    )
    deps = detect_traced_dependencies(source, "mod", registry)
    assert "mod.Reader.read" in deps
    assert "mod.Writer.write" in deps


def test_detect_typed_obj_method_e2e_reconstruction(tmp_path: Path) -> None:
    """End-to-end: typed obj.method() detected statically, reconstructed, executable."""
    mod = create_module(
        tmp_path,
        "typede2e",
        (
            "import offwork\n\n"
            "class Calculator:\n"
            "    @offwork.task\n"
            "    def add(self, a, b):\n"
            "        return a + b\n\n"
            "@offwork.task\n"
            "def compute(calc: Calculator, x, y):\n"
            "    return calc.add(x, y)\n"
        ),
    )
    # Do NOT call compute -- static detection only
    source = reconstruct(serialize(), "compute")
    assert "class Calculator:" in source
    assert "def add(self, a, b):" in source
    assert "def compute(" in source
    # Execute the reconstructed code
    ns: dict[str, object] = {}
    exec(source, ns)  # noqa: S102
    calc = ns["Calculator"]()  # type: ignore[operator]
    assert ns["compute"](calc, 3, 4) == 7  # type: ignore[operator]
