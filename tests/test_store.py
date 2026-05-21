import json
from pathlib import Path

import pytest

from offwork.graph.graph import Graph
from offwork.core.models import FunctionNode, ImportInfo
from offwork.graph.store import Store, MergeResult
from tests.conftest import create_module


# -- Helpers -----------------------------------------------------------------

def _node(name: str, module: str = "m", source: str | None = None,
          imports: list[ImportInfo] | None = None,
          owner_class: str | None = None,
          closure_vars: dict[str, str] | None = None,
          closure_func_refs: dict[str, str] | None = None) -> FunctionNode:
    return FunctionNode(
        qualified_name=f"{module}.{name}",
        name=name,
        module=module,
        source=source or f"def {name}():\n    pass\n",
        imports=imports or [],
        dependencies=[],
        owner_class=owner_class,
        closure_vars=closure_vars or {},
        closure_func_refs=closure_func_refs or {},
    )


def _chain_store() -> Store:
    """Build a store with A -> B -> C dependency chain."""
    a = _node("a", source="def a():\n    return b()\n")
    b = _node("b", source="def b():\n    return c()\n")
    c = _node("c", source="def c():\n    return 42\n")
    store = Store()
    ha, hb, hc = store.put(a), store.put(b), store.put(c)
    store.set_ref("m.a", ha)
    store.set_ref("m.b", hb)
    store.set_ref("m.c", hc)
    store.set_deps(ha, [hb])
    store.set_deps(hb, [hc])
    return store


def _diamond_store() -> Store:
    """Build A -> B, A -> C, B -> D, C -> D."""
    a = _node("a", source="def a():\n    return b() + c()\n")
    b = _node("b", source="def b():\n    return d()\n")
    c = _node("c", source="def c():\n    return d()\n")
    d = _node("d", source="def d():\n    return 1\n")
    store = Store()
    ha, hb, hc, hd = store.put(a), store.put(b), store.put(c), store.put(d)
    store.set_ref("m.a", ha)
    store.set_ref("m.b", hb)
    store.set_ref("m.c", hc)
    store.set_ref("m.d", hd)
    store.set_deps(ha, [hb, hc])
    store.set_deps(hb, [hd])
    store.set_deps(hc, [hd])
    return store


# -- Object operations ------------------------------------------------------

class TestObjectOps:
    def test_put_and_get(self) -> None:
        store = Store()
        node = _node("f")
        h = store.put(node)
        blob = store.get(h)
        assert blob is not None
        assert blob["name"] == "f"
        assert blob["module"] == "m"

    def test_put_returns_content_hash(self) -> None:
        node = _node("f")
        store = Store()
        assert store.put(node) == node.content_hash()

    def test_put_deduplicates(self) -> None:
        store = Store()
        node = _node("f")
        h1 = store.put(node)
        h2 = store.put(node)
        assert h1 == h2
        assert len(store.object_hashes) == 1

    def test_has(self) -> None:
        store = Store()
        node = _node("f")
        h = store.put(node)
        assert store.has(h)
        assert not store.has("nonexistent")

    def test_get_nonexistent(self) -> None:
        store = Store()
        assert store.get("nonexistent") is None

    def test_object_hashes(self) -> None:
        store = Store()
        a, b = _node("a"), _node("b")
        ha, hb = store.put(a), store.put(b)
        assert store.object_hashes == {ha, hb}


# -- Dep operations ----------------------------------------------------------

class TestDepOps:
    def test_set_get_deps(self) -> None:
        store = Store()
        store.set_deps("h1", ["h2", "h3"])
        assert store.get_deps("h1") == ["h2", "h3"]

    def test_get_deps_no_entry(self) -> None:
        store = Store()
        assert store.get_deps("nonexistent") == []

    def test_set_empty_deps_removes_entry(self) -> None:
        store = Store()
        store.set_deps("h1", ["h2"])
        store.set_deps("h1", [])
        assert store.get_deps("h1") == []


# -- Ref operations ----------------------------------------------------------

class TestRefOps:
    def test_set_get_ref(self) -> None:
        store = Store()
        store.set_ref("m.f", "h1")
        assert store.get_ref("m.f") == "h1"

    def test_get_ref_nonexistent(self) -> None:
        store = Store()
        assert store.get_ref("m.f") is None

    def test_del_ref(self) -> None:
        store = Store()
        store.set_ref("m.f", "h1")
        store.del_ref("m.f")
        assert store.get_ref("m.f") is None

    def test_del_ref_nonexistent_is_noop(self) -> None:
        store = Store()
        store.del_ref("m.f")  # should not raise

    def test_refs_property(self) -> None:
        store = Store()
        store.set_ref("m.a", "h1")
        store.set_ref("m.b", "h2")
        assert store.refs == {"m.a": "h1", "m.b": "h2"}


# -- Graph operations -------------------------------------------------------

class TestWalk:
    def test_single_node(self) -> None:
        store = Store()
        node = _node("f")
        h = store.put(node)
        assert store.walk(h) == [h]

    def test_chain(self) -> None:
        store = _chain_store()
        refs = store.refs
        ha = refs["m.a"]
        reachable = set(store.walk(ha))
        assert reachable == {refs["m.a"], refs["m.b"], refs["m.c"]}

    def test_diamond(self) -> None:
        store = _diamond_store()
        refs = store.refs
        ha = refs["m.a"]
        reachable = set(store.walk(ha))
        assert reachable == {refs[f"m.{n}"] for n in "abcd"}

    def test_walk_leaf(self) -> None:
        store = _chain_store()
        hc = store.refs["m.c"]
        assert store.walk(hc) == [hc]


class TestSubgraph:
    def test_extracts_closure(self) -> None:
        store = _chain_store()
        refs = store.refs
        sub = store.subgraph(refs["m.a"])
        assert sub.object_hashes == {refs["m.a"], refs["m.b"], refs["m.c"]}
        assert sub.refs == refs

    def test_excludes_unrelated(self) -> None:
        store = _chain_store()
        extra = _node("extra", source="def extra():\n    return 99\n")
        he = store.put(extra)
        store.set_ref("m.extra", he)

        sub = store.subgraph(store.refs["m.a"])
        assert he not in sub.object_hashes
        assert "m.extra" not in sub.refs

    def test_partial_subgraph(self) -> None:
        store = _chain_store()
        refs = store.refs
        sub = store.subgraph(refs["m.b"])
        assert sub.object_hashes == {refs["m.b"], refs["m.c"]}
        assert "m.a" not in sub.refs

    def test_diamond_subgraph(self) -> None:
        store = _diamond_store()
        refs = store.refs
        sub = store.subgraph(refs["m.b"])
        assert sub.object_hashes == {refs["m.b"], refs["m.d"]}


class TestMissing:
    def test_returns_absent_hashes(self) -> None:
        store = Store()
        node = _node("f")
        h = store.put(node)
        assert store.missing({"x", "y", h}) == {"x", "y"}

    def test_empty_set(self) -> None:
        store = Store()
        assert store.missing(set()) == set()

    def test_all_present(self) -> None:
        store = Store()
        h = store.put(_node("f"))
        assert store.missing({h}) == set()


# -- Merge -------------------------------------------------------------------

class TestMerge:
    def test_disjoint_stores(self) -> None:
        s1, s2 = Store(), Store()
        n1, n2 = _node("a"), _node("b")
        h1, h2 = s1.put(n1), s2.put(n2)
        s1.set_ref("m.a", h1)
        s2.set_ref("m.b", h2)

        result = s1.merge(s2)
        assert result.added_objects == 1
        assert result.added_refs == 1
        assert not result.conflicts
        assert s1.has(h2)
        assert s1.get_ref("m.b") == h2

    def test_overlapping_objects(self) -> None:
        s1, s2 = Store(), Store()
        node = _node("shared")
        h1 = s1.put(node)
        h2 = s2.put(node)
        assert h1 == h2

        result = s1.merge(s2)
        assert result.added_objects == 0

    def test_ref_conflict(self) -> None:
        s1, s2 = Store(), Store()
        n1 = _node("f", source="def f():\n    return 1\n")
        n2 = _node("f", source="def f():\n    return 2\n")
        h1, h2 = s1.put(n1), s2.put(n2)
        s1.set_ref("m.f", h1)
        s2.set_ref("m.f", h2)

        result = s1.merge(s2)
        assert "m.f" in result.conflicts
        assert result.conflicts["m.f"] == (h1, h2)
        # Existing ref is kept
        assert s1.get_ref("m.f") == h1

    def test_edges_unioned(self) -> None:
        s1, s2 = Store(), Store()
        s1.set_deps("h1", ["h2"])
        s2.set_deps("h1", ["h3"])

        s1.merge(s2)
        assert set(s1.get_deps("h1")) == {"h2", "h3"}

    def test_merge_empty_store(self) -> None:
        s1 = Store()
        s1.put(_node("f"))
        result = s1.merge(Store())
        assert result.added_objects == 0
        assert result.added_refs == 0


# -- GC ---------------------------------------------------------------------

class TestGC:
    def test_removes_unreachable(self) -> None:
        store = Store()
        orphan = _node("orphan")
        alive = _node("alive")
        ho = store.put(orphan)
        ha = store.put(alive)
        store.set_ref("m.alive", ha)

        removed = store.gc()
        assert ho in removed
        assert not store.has(ho)
        assert store.has(ha)

    def test_keeps_transitive_deps(self) -> None:
        store = _chain_store()
        removed = store.gc()
        assert removed == set()
        refs = store.refs
        for h in refs.values():
            assert store.has(h)

    def test_gc_empty_store(self) -> None:
        store = Store()
        assert store.gc() == set()


# -- Serialization -----------------------------------------------------------

class TestSerialization:
    def test_json_roundtrip(self) -> None:
        store = _chain_store()
        json_str = store.to_json()
        restored = Store.from_json(json_str)
        assert restored.refs == store.refs
        assert restored.object_hashes == store.object_hashes
        for h in store.object_hashes:
            assert restored.get_deps(h) == store.get_deps(h)

    def test_to_dict_format(self) -> None:
        store = _chain_store()
        data = store.to_dict()
        assert data["version"].startswith("0.1")
        assert "objects" in data
        assert "deps" in data
        assert "refs" in data
        # Objects should not have hash or deps fields
        for obj in data["objects"].values():
            assert "hash" not in obj
            assert "deps" not in obj

    def test_to_json_closure_func_refs_become_hashes(self) -> None:
        """In serialized JSON, closure_func_refs values become hashes."""
        store = Store()
        helper = _node("helper", source="def helper():\n    return 1\n")
        wrapper = _node(
            "wrapper",
            source="def wrapper():\n    return fn()\n",
            closure_func_refs={"fn": "m.helper"},
        )
        hh = store.put(helper)
        hw = store.put(wrapper)
        store.set_ref("m.helper", hh)
        store.set_ref("m.wrapper", hw)
        store.set_deps(hw, [hh])

        data = store.to_dict()
        obj = data["objects"][hw]
        assert obj["closure_func_refs"]["fn"] == hh

    def test_roundtrip_preserves_closure_func_refs(self) -> None:
        """closure_func_refs survive qname -> hash -> qname roundtrip."""
        store = Store()
        helper = _node("helper", source="def helper():\n    return 1\n")
        wrapper = _node(
            "wrapper",
            source="def wrapper():\n    return fn()\n",
            closure_func_refs={"fn": "m.helper"},
        )
        hh = store.put(helper)
        hw = store.put(wrapper)
        store.set_ref("m.helper", hh)
        store.set_ref("m.wrapper", hw)

        restored = Store.from_json(store.to_json())
        blob = restored.get(hw)
        assert blob is not None
        assert blob["closure_func_refs"] == {"fn": "m.helper"}


# -- Reconstruction ---------------------------------------------------------

class TestReconstruction:
    def test_reconstruct_single(self) -> None:
        store = Store()
        node = _node("f", source="def f():\n    return 42\n")
        h = store.put(node)
        store.set_ref("m.f", h)

        source = store.reconstruct("f")
        assert "def f():" in source
        assert "return 42" in source

    def test_reconstruct_with_deps(self) -> None:
        store = _chain_store()
        source = store.reconstruct("a")
        assert "def a():" in source
        assert "def b():" in source
        assert "def c():" in source
        # Dependencies appear before dependents
        assert source.index("def c") < source.index("def b")
        assert source.index("def b") < source.index("def a")

    def test_reconstruct_unknown_raises(self) -> None:
        store = Store()
        with pytest.raises(KeyError):
            store.reconstruct("nonexistent")

    def test_reconstruct_matches_graph(self, tmp_path: Path) -> None:
        """Store.reconstruct produces same output as Graph.reconstruct."""
        create_module(
            tmp_path,
            "store_recon",
            (
                "import csv\nimport json as js\n\n"
                "import offwork\n\n"
                "@offwork.task\n"
                "def parse(data):\n"
                "    return csv.reader(data)\n\n"
                "@offwork.task\n"
                "def transform(data):\n"
                "    rows = parse(data)\n"
                "    return js.dumps(rows)\n"
            ),
        )
        graph = Graph.default()
        json_str = graph.serialize()
        graph_source = Graph.reconstruct(json_str, "transform")
        store_source = Store.from_json(json_str).reconstruct("transform")
        assert graph_source == store_source


# -- Integration with Graph.to_store ------------------------------------

class TestToStore:
    def test_to_store_full(self, tmp_path: Path) -> None:
        create_module(
            tmp_path,
            "tostore",
            (
                "import offwork\n\n"
                "@offwork.task\n"
                "def leaf():\n    return 1\n\n"
                "@offwork.task\n"
                "def root():\n    return leaf()\n"
            ),
        )
        graph = Graph.default()
        store = graph.to_store()
        assert "tostore.leaf" in store.refs
        assert "tostore.root" in store.refs
        root_h = store.get_ref("tostore.root")
        assert root_h is not None
        leaf_h = store.get_ref("tostore.leaf")
        assert leaf_h is not None
        assert store.get_deps(root_h) == [leaf_h]

    def test_to_store_subgraph(self, tmp_path: Path) -> None:
        create_module(
            tmp_path,
            "tostoresub",
            (
                "import offwork\n\n"
                "@offwork.task\n"
                "def a():\n    return 1\n\n"
                "@offwork.task\n"
                "def b():\n    return a()\n\n"
                "@offwork.task\n"
                "def c():\n    return 2\n"
            ),
        )
        graph = Graph.default()
        store = graph.to_store("b")
        assert "tostoresub.a" in store.refs
        assert "tostoresub.b" in store.refs
        assert "tostoresub.c" not in store.refs


# -- Node insertion / replacement -------------------------------------------

class TestMutation:
    def test_insert_node(self) -> None:
        store = _chain_store()
        new = _node("new_func", source="def new_func():\n    return 99\n")
        h = store.put(new)
        store.set_ref("m.new_func", h)
        assert store.has(h)
        assert store.get_ref("m.new_func") == h

    def test_replace_node(self) -> None:
        store = Store()
        v1 = _node("f", source="def f():\n    return 1\n")
        v2 = _node("f", source="def f():\n    return 2\n")
        h1 = store.put(v1)
        store.set_ref("m.f", h1)

        h2 = store.put(v2)
        store.set_ref("m.f", h2)
        assert h1 != h2
        assert store.get_ref("m.f") == h2
        # Old object is still in store (might be referenced elsewhere)
        assert store.has(h1)
        # GC cleans it up
        removed = store.gc()
        assert h1 in removed

    def test_insert_subgraph_via_merge(self) -> None:
        main_store = _chain_store()
        sub = Store()
        new = _node("extra", source="def extra():\n    return 0\n")
        h = sub.put(new)
        sub.set_ref("m.extra", h)

        result = main_store.merge(sub)
        assert result.added_objects == 1
        assert main_store.has(h)
