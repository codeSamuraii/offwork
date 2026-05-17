import importlib
import json
import warnings
from pathlib import Path

import pytest

from offwork.core.errors import Error
from offwork.core.models import FunctionNode
from offwork.graph.graph import Graph
from tests.conftest import create_module


def _make_graph(tmp_path: Path) -> Graph:
    mod = create_module(
        tmp_path,
        "gmod",
        (
            "import csv\nimport json as js\n\n"
            "from offwork import trace\n\n"
            "@trace\n"
            "def parse(data):\n"
            "    return csv.reader(data)\n\n"
            "@trace\n"
            "def transform(data):\n"
            "    rows = parse(data)\n"
            "    return js.dumps(rows)\n"
        ),
    )
    return Graph.default()


def test_register_populates_graph(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    assert "gmod.parse" in graph.nodes
    assert "gmod.transform" in graph.nodes


def test_node_imports(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    parse_node = graph.nodes["gmod.parse"]
    assert any(imp.bound_name == "csv" for imp in parse_node.imports)

    transform_node = graph.nodes["gmod.transform"]
    assert any(imp.bound_name == "js" for imp in transform_node.imports)


def test_node_dependencies(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    transform_node = graph.nodes["gmod.transform"]
    assert "gmod.parse" in transform_node.dependencies


def test_serialize_full_graph(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    data = json.loads(graph.serialize())
    assert "version" in data
    assert "gmod.parse" in data["refs"]
    assert "gmod.transform" in data["refs"]
    # All refs point to existing objects
    for h in data["refs"].values():
        assert h in data["objects"]


def test_serialize_subgraph(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    data = json.loads(graph.serialize("parse"))
    assert "gmod.parse" in data["refs"]
    assert "gmod.transform" not in data["refs"]


def test_serialize_subgraph_with_deps(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    data = json.loads(graph.serialize("transform"))
    assert "gmod.parse" in data["refs"]
    assert "gmod.transform" in data["refs"]


def test_deserialize_roundtrip(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    json_str = graph.serialize()
    restored = Graph.deserialize_graph(json_str)
    assert set(restored.nodes.keys()) == set(graph.nodes.keys())
    for qn in graph.nodes:
        assert restored.nodes[qn].source == graph.nodes[qn].source
        assert restored.nodes[qn].dependencies == graph.nodes[qn].dependencies


def test_reconstruct_single_function(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    json_str = graph.serialize()
    source = Graph.reconstruct(json_str, "parse")
    assert "import csv" in source
    assert "def parse(data):" in source
    assert "def transform" not in source


def test_reconstruct_with_dependencies(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    json_str = graph.serialize()
    source = Graph.reconstruct(json_str, "transform")
    assert "import csv" in source
    assert "import json as js" in source
    assert "def parse(data):" in source
    assert "def transform(data):" in source
    # parse must appear before transform
    assert source.index("def parse") < source.index("def transform")


def test_reconstruct_deduplicates_imports(tmp_path: Path) -> None:
    mod = create_module(
        tmp_path,
        "dedup",
        (
            "import csv\n\n"
            "from offwork import trace\n\n"
            "@trace\n"
            "def a(x):\n    return csv.reader(x)\n\n"
            "@trace\n"
            "def b(x):\n    return csv.writer(a(x))\n"
        ),
    )
    graph = Graph.default()
    json_str = graph.serialize()
    source = Graph.reconstruct(json_str, "b")
    assert source.count("import csv") == 1


def test_reconstruct_unknown_function_raises(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    json_str = graph.serialize()
    try:
        Graph.reconstruct(json_str, "nonexistent")
        assert False, "Expected KeyError"
    except KeyError:
        pass


def test_register_sourceless_function_raises() -> None:
    graph = Graph()
    with pytest.raises(Error, match="source code unavailable"):
        graph.register(len)


def test_register_exec_function_raises() -> None:
    ns: dict[str, object] = {}
    exec("def dynamic(): return 1", ns)  # noqa: S102
    graph = Graph()
    with pytest.raises(Error, match="source code unavailable"):
        graph.register(ns["dynamic"])  # type: ignore[arg-type]


def test_auto_refresh_on_register(tmp_path: Path) -> None:
    create_module(
        tmp_path,
        "autoref",
        (
            "from offwork import trace\n\n"
            "@trace\n"
            "def caller():\n    return callee()\n\n"
            "@trace\n"
            "def callee():\n    return 42\n"
        ),
    )
    graph = Graph.default()
    assert "autoref.callee" in graph.nodes["autoref.caller"].dependencies


def test_class_method_registration(tmp_path: Path) -> None:
    create_module(
        tmp_path,
        "clsreg",
        (
            "from offwork import trace\n\n"
            "class Parser:\n"
            "    @trace\n"
            "    def parse(self, data):\n"
            "        return data.split(',')\n"
        ),
    )
    graph = Graph.default()
    node = graph.nodes["clsreg.Parser.parse"]
    assert node.name == "parse"
    assert node.owner_class == "Parser"


def test_class_method_reconstruction(tmp_path: Path) -> None:
    create_module(
        tmp_path,
        "clsrecon",
        (
            "import csv\n\n"
            "from offwork import trace\n\n"
            "class Parser:\n"
            "    @trace\n"
            "    def helper(self, data):\n"
            "        return csv.reader(data)\n\n"
            "    @trace\n"
            "    def parse(self, data):\n"
            "        return self.helper(data)\n"
        ),
    )
    graph = Graph.default()
    json_str = graph.serialize()
    source = Graph.reconstruct(json_str, "parse")
    assert "class Parser:" in source
    assert "    def helper(self, data):" in source
    assert "    def parse(self, data):" in source
    assert "import csv" in source
    # helper should appear before parse within the class
    assert source.index("def helper") < source.index("def parse")


def test_nested_function_closure_captured(tmp_path: Path) -> None:
    create_module(
        tmp_path,
        "closure_mod",
        (
            "from offwork import trace\n\n"
            "def outer():\n"
            "    x = 10\n"
            "    @trace\n"
            "    def inner():\n"
            "        return x\n"
            "    return inner\n"
        ),
    )
    mod = importlib.import_module("closure_mod")
    mod.outer()
    graph = Graph.default()
    node = graph.nodes["closure_mod.outer.<locals>.inner"]
    assert node.closure_vars == {"x": "10"}


# -- to_mermaid tests --


def test_to_mermaid_full_graph(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    mermaid = graph.to_mermaid()
    assert mermaid.startswith("graph TD\n")
    assert '"parse"' in mermaid
    assert '"transform"' in mermaid
    assert "gmod_transform --> gmod_parse" in mermaid


def test_to_mermaid_subgraph(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    mermaid = graph.to_mermaid("parse")
    assert '"parse"' in mermaid
    assert '"transform"' not in mermaid


def test_to_mermaid_class_methods(tmp_path: Path) -> None:
    create_module(
        tmp_path,
        "mermcls",
        (
            "from offwork import trace\n\n"
            "class Pipeline:\n"
            "    @trace\n"
            "    def step_a(self, x):\n"
            "        return x.strip()\n\n"
            "    @trace\n"
            "    def step_b(self, x):\n"
            "        return self.step_a(x).lower()\n"
        ),
    )
    graph = Graph.default()
    mermaid = graph.to_mermaid()
    assert "subgraph Pipeline" in mermaid
    assert '"step_a"' in mermaid
    assert '"step_b"' in mermaid
    assert "end" in mermaid
    assert "mermcls_Pipeline_step_b --> mermcls_Pipeline_step_a" in mermaid


def test_to_mermaid_direction(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    mermaid = graph.to_mermaid(direction="LR")
    assert mermaid.startswith("graph LR\n")


def test_to_mermaid_empty_graph() -> None:
    graph = Graph()
    mermaid = graph.to_mermaid()
    assert mermaid == "graph TD\n"


# -- Content-addressable store tests --


def test_content_hash_deterministic(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    node = graph.nodes["gmod.parse"]
    assert node.content_hash() == node.content_hash()


def test_content_hash_changes_on_source_change(tmp_path: Path) -> None:
    node_a = FunctionNode(
        qualified_name="m.f", name="f", module="m",
        source="def f(): return 1",
    )
    node_b = FunctionNode(
        qualified_name="m.f", name="f", module="m",
        source="def f(): return 2",
    )
    assert node_a.content_hash() != node_b.content_hash()


def test_content_hash_stable_on_dep_change(tmp_path: Path) -> None:
    node_a = FunctionNode(
        qualified_name="m.f", name="f", module="m",
        source="def f(): pass", dependencies=[],
    )
    node_b = FunctionNode(
        qualified_name="m.f", name="f", module="m",
        source="def f(): pass", dependencies=["m.other"],
    )
    assert node_a.content_hash() == node_b.content_hash()


def test_deduplication_shared_dep(tmp_path: Path) -> None:
    create_module(
        tmp_path,
        "dedup_shared",
        (
            "from offwork import trace\n\n"
            "@trace\n"
            "def shared():\n    return 1\n\n"
            "@trace\n"
            "def caller_a():\n    return shared()\n\n"
            "@trace\n"
            "def caller_b():\n    return shared()\n"
        ),
    )
    graph = Graph.default()
    data = json.loads(graph.serialize())
    # shared() appears once in objects, referenced by both callers
    shared_hash = data["refs"]["dedup_shared.shared"]
    caller_a_hash = data["refs"]["dedup_shared.caller_a"]
    caller_b_hash = data["refs"]["dedup_shared.caller_b"]
    assert shared_hash in data["deps"][caller_a_hash]
    assert shared_hash in data["deps"][caller_b_hash]
    # Only one object entry for shared
    shared_hashes = [
        h for h, obj in data["objects"].items() if obj["name"] == "shared"
    ]
    assert len(shared_hashes) == 1


def test_serialize_format_structure(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    data = json.loads(graph.serialize())
    assert data["version"] == "0.4.0"
    assert "objects" in data
    assert "deps" in data
    assert "refs" in data
    # Each object has required content fields but no hash or deps
    for h, obj in data["objects"].items():
        assert "hash" not in obj
        assert "deps" not in obj
        assert "name" in obj
        assert "module" in obj
        assert "source" in obj
        assert "imports" in obj
