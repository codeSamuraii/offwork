from __future__ import annotations

from pathlib import Path

from pyfuse import reconstruct, serialize
from pyfuse._graph import FuseGraph
from tests.conftest import create_module


def test_csv_requests_example(tmp_path: Path) -> None:
    """End-to-end test with the motivating example from the requirements."""
    # Create a stub 'requests' module so the test module can import it
    (tmp_path / "requests.py").write_text("def get(url): pass\n")
    create_module(
        tmp_path,
        "example",
        (
            "import csv\nimport requests\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def parse_csv(csv_data: str) -> dict:\n"
            '    """Parses CSV data."""\n'
            "    reader = csv.reader(csv_data)\n"
            "    rows = list(reader)\n"
            "    if not rows:\n"
            "        return {}\n"
            "    headers = rows[0]\n"
            "    return {i: dict(zip(headers, row)) for i, row in enumerate(rows[1:])}\n\n"
            "@trace\n"
            "def fetch_table(url: str) -> dict:\n"
            '    """Fetches CSV data from the given URL."""\n'
            "    csv_data = requests.get(url)\n"
            "    table_data = parse_csv(csv_data.text)\n"
            "    return table_data\n"
        ),
    )

    graph_json = serialize()

    # Reconstruct parse_csv
    source_parse = reconstruct(graph_json, "parse_csv")
    assert "import csv" in source_parse
    assert "import requests" not in source_parse
    assert "def parse_csv" in source_parse
    assert "def fetch_table" not in source_parse

    # Reconstruct fetch_table
    source_fetch = reconstruct(graph_json, "fetch_table")
    assert "import csv" in source_fetch
    assert "import requests" in source_fetch
    assert "def parse_csv" in source_fetch
    assert "def fetch_table" in source_fetch
    assert source_fetch.index("def parse_csv") < source_fetch.index("def fetch_table")


def test_three_level_chain(tmp_path: Path) -> None:
    create_module(
        tmp_path,
        "chain",
        (
            "import csv\nimport json\nimport os\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def step_a(x):\n    return csv.reader(x)\n\n"
            "@trace\n"
            "def step_b(x):\n    return json.dumps(step_a(x))\n\n"
            "@trace\n"
            "def step_c(x):\n    os.getenv('X')\n    return step_b(x)\n"
        ),
    )

    source = reconstruct(serialize(), "step_c")
    assert "import csv" in source
    assert "import json" in source
    assert "import os" in source
    lines = source.splitlines()
    func_lines = [l for l in lines if l.startswith("def ")]
    assert [f.split("(")[0] for f in func_lines] == [
        "def step_a",
        "def step_b",
        "def step_c",
    ]


def test_diamond_dependency(tmp_path: Path) -> None:
    create_module(
        tmp_path,
        "diamond",
        (
            "import csv\nimport json\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def base(x):\n    return csv.reader(x)\n\n"
            "@trace\n"
            "def left(x):\n    return json.dumps(base(x))\n\n"
            "@trace\n"
            "def right(x):\n    return list(base(x))\n\n"
            "@trace\n"
            "def top(x):\n    return left(x), right(x)\n"
        ),
    )

    source = reconstruct(serialize(), "top")
    assert source.count("def base") == 1
    assert "def left" in source
    assert "def right" in source
    assert "def top" in source
    # base must be before both left and right
    assert source.index("def base") < source.index("def left")
    assert source.index("def base") < source.index("def right")
    # both left and right must be before top
    assert source.index("def left") < source.index("def top")
    assert source.index("def right") < source.index("def top")


def test_independent_functions(tmp_path: Path) -> None:
    create_module(
        tmp_path,
        "indep",
        (
            "import csv\nimport json\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def func_a(x):\n    return csv.reader(x)\n\n"
            "@trace\n"
            "def func_b(x):\n    return json.dumps(x)\n"
        ),
    )

    graph_json = serialize()
    source_a = reconstruct(graph_json, "func_a")
    assert "def func_a" in source_a
    assert "def func_b" not in source_a
    assert "import json" not in source_a

    source_b = reconstruct(graph_json, "func_b")
    assert "def func_b" in source_b
    assert "def func_a" not in source_b
    assert "import csv" not in source_b


def test_order_independent_registration(tmp_path: Path) -> None:
    """Dependencies are resolved regardless of registration order (auto-refresh)."""
    create_module(
        tmp_path,
        "late",
        (
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def caller():\n    return callee()\n\n"
            "@trace\n"
            "def callee():\n    return 42\n"
        ),
    )

    graph = FuseGraph.default()
    # Auto-refresh means dependencies are resolved regardless of order
    assert "late.callee" in graph.nodes["late.caller"].dependencies


def test_class_methods_end_to_end(tmp_path: Path) -> None:
    """Full pipeline with class methods including self.method() dependencies."""
    create_module(
        tmp_path,
        "clse2e",
        (
            "import csv\n\n"
            "from pyfuse import trace\n\n"
            "class DataProcessor:\n"
            "    @trace\n"
            "    def read(self, data):\n"
            "        return list(csv.reader(data))\n\n"
            "    @trace\n"
            "    def process(self, data):\n"
            "        rows = self.read(data)\n"
            "        return rows\n"
        ),
    )

    graph_json = serialize()
    source = reconstruct(graph_json, "process")
    assert "import csv" in source
    assert "class DataProcessor:" in source
    assert "    def read(self, data):" in source
    assert "    def process(self, data):" in source
    assert source.index("def read") < source.index("def process")


def test_star_import_end_to_end(tmp_path: Path) -> None:
    """Functions using names from star imports get the correct explicit imports."""
    create_module(
        tmp_path,
        "stare2e",
        (
            "from os.path import *\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def build_path():\n"
            "    return join('a', 'b')\n"
        ),
    )

    graph_json = serialize()
    source = reconstruct(graph_json, "build_path")
    assert "from os.path import join" in source
    assert "*" not in source
    assert "def build_path" in source


def test_mixed_class_and_standalone(tmp_path: Path) -> None:
    """Reconstruction handles both class methods and standalone functions."""
    create_module(
        tmp_path,
        "mixed",
        (
            "import csv\n\n"
            "from pyfuse import trace\n\n"
            "@trace\n"
            "def helper(data):\n"
            "    return csv.reader(data)\n\n"
            "class Worker:\n"
            "    @trace\n"
            "    def work(self, data):\n"
            "        return helper(data)\n"
        ),
    )

    graph_json = serialize()
    source = reconstruct(graph_json, "work")
    assert "import csv" in source
    assert "def helper(data):" in source
    assert "class Worker:" in source
    assert "    def work(self, data):" in source
    # helper (standalone) should appear before the Worker class
    assert source.index("def helper") < source.index("class Worker")
