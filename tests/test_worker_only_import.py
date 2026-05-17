"""Tests for ``worker_only_import``: client-side stubs + AST detection."""

import sys
from pathlib import Path

import pytest

import pyfuse
from pyfuse import WorkerOnlyError, worker_only_import
from pyfuse.core.models import ImportInfo
from pyfuse.graph.analyzer import (
    _parse_worker_only_import,
    get_module_imports,
)
from pyfuse.graph.store import Store
from pyfuse.worker.deps import (
    _StubAttr,
    _WorkerOnlyFinder,
    _WorkerOnlyStub,
    _collect_package_hints,
    extract_third_party_modules,
)

from tests.conftest import create_module


# -- ImportInfo.worker_only --------------------------------------------------

class TestImportInfoWorkerOnly:
    def test_default_false(self) -> None:
        imp = ImportInfo("import os", "os")
        assert imp.worker_only is False

    def test_roundtrip_dict(self) -> None:
        imp = ImportInfo("import requests", "requests", worker_only=True)
        d = imp.to_dict()
        assert d["worker_only"] is True
        assert ImportInfo.from_dict(d).worker_only is True

    def test_false_omitted_from_dict(self) -> None:
        imp = ImportInfo("import os", "os")
        assert "worker_only" not in imp.to_dict()


# -- worker_only_import context manager -------------------------------------

class TestWorkerOnlyImport:
    def test_missing_package_imports_as_stub(self) -> None:
        with worker_only_import():
            import some_missing_pkg_aaa  # type: ignore[import-not-found]
        assert isinstance(some_missing_pkg_aaa, _WorkerOnlyStub)
        assert some_missing_pkg_aaa.__pyfuse_stub__ is True

    def test_from_import_yields_stub_attr(self) -> None:
        with worker_only_import():
            from some_missing_pkg_bbb import thing  # type: ignore[import-not-found]
        assert isinstance(thing, _StubAttr)

    def test_call_on_stub_module_raises(self) -> None:
        with worker_only_import():
            import some_missing_pkg_ccc  # type: ignore[import-not-found]
        with pytest.raises(WorkerOnlyError):
            some_missing_pkg_ccc.do_thing()

    def test_call_on_stub_attr_raises(self) -> None:
        with worker_only_import():
            from some_missing_pkg_ddd import helper  # type: ignore[import-not-found]
        with pytest.raises(WorkerOnlyError):
            helper(1, 2)

    def test_chained_attribute_access_raises_on_call(self) -> None:
        with worker_only_import():
            import some_missing_pkg_eee  # type: ignore[import-not-found]
        with pytest.raises(WorkerOnlyError):
            some_missing_pkg_eee.api.client.get("/foo")

    def test_real_packages_still_resolve(self) -> None:
        """The finder must run last so installed packages win."""
        with worker_only_import():
            import json  # stdlib, definitely real
        assert not isinstance(json, _WorkerOnlyStub)
        assert json.dumps([1]) == "[1]"

    def test_finder_removed_after_block(self) -> None:
        before = list(sys.meta_path)
        with worker_only_import():
            pass
        assert not any(isinstance(f, _WorkerOnlyFinder) for f in sys.meta_path)
        assert sys.meta_path == before

    def test_finder_removed_after_exception(self) -> None:
        with pytest.raises(RuntimeError):
            with worker_only_import():
                raise RuntimeError("boom")
        assert not any(isinstance(f, _WorkerOnlyFinder) for f in sys.meta_path)

    def test_nested_blocks(self) -> None:
        with worker_only_import():
            with worker_only_import("foo"):
                import some_missing_pkg_fff  # type: ignore[import-not-found]
            # Still inside outer block: finder must remain
            assert any(isinstance(f, _WorkerOnlyFinder) for f in sys.meta_path)
        assert not any(isinstance(f, _WorkerOnlyFinder) for f in sys.meta_path)
        assert isinstance(some_missing_pkg_fff, _WorkerOnlyStub)

    def test_submodule_import(self) -> None:
        with worker_only_import():
            import some_missing_pkg_ggg.submodule  # type: ignore[import-not-found]
        assert isinstance(some_missing_pkg_ggg, _WorkerOnlyStub)
        assert isinstance(some_missing_pkg_ggg.submodule, _WorkerOnlyStub)

    def test_transitive_missing_imports_still_raise(self) -> None:
        """Transitive imports made by a real package are NOT stubbed.

        Only modules listed in the user's ``with`` block source get
        stubbed.  A missing import made via ``importlib.import_module``
        from inside a real installed package (or any other code path the
        user did not literally write in the block) must raise the normal
        ``ModuleNotFoundError``, not be silently stubbed.
        """
        import importlib
        with worker_only_import():
            import declared_in_block  # type: ignore[import-not-found]
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module("some_other_missing_pkg_qqq")
        assert isinstance(declared_in_block, _WorkerOnlyStub)


# -- AST analyzer detection -------------------------------------------------

class TestParseWorkerOnlyImport:
    def _parse_with(self, source: str) -> object:
        import ast
        tree = ast.parse(source)
        with_node = tree.body[0]
        assert isinstance(with_node, ast.With)
        return _parse_worker_only_import(with_node)

    def test_no_args(self) -> None:
        assert self._parse_with("with worker_only_import():\n    pass\n") is True

    def test_with_package(self) -> None:
        assert (
            self._parse_with("with worker_only_import('opencv-python'):\n    pass\n")
            == "opencv-python"
        )

    def test_attribute_form(self) -> None:
        assert (
            self._parse_with("with pyfuse.worker_only_import():\n    pass\n") is True
        )

    def test_unrelated_with_block(self) -> None:
        assert self._parse_with("with open('x') as f:\n    pass\n") is False

    def test_install_package_as_does_not_match(self) -> None:
        assert (
            self._parse_with("with install_package_as('foo'):\n    pass\n") is False
        )


class TestGetModuleImports:
    def test_worker_only_flag_on_imports(self, tmp_path: Path) -> None:
        mod = create_module(
            tmp_path,
            "wo_basic",
            (
                "from pyfuse import worker_only_import\n"
                "import os\n"
                "with worker_only_import():\n"
                "    import requests\n"
                "    from urllib3 import PoolManager\n"
                "\n"
                "def f(): pass\n"
            ),
        )
        imports = get_module_imports(mod.f)
        by_stmt = {imp.statement: imp for imp in imports}
        assert by_stmt["import os"].worker_only is False
        assert by_stmt["import requests"].worker_only is True
        assert by_stmt["from urllib3 import PoolManager"].worker_only is True

    def test_worker_only_with_explicit_package(self, tmp_path: Path) -> None:
        mod = create_module(
            tmp_path,
            "wo_pkg",
            (
                "from pyfuse import worker_only_import\n"
                "with worker_only_import('opencv-python-headless'):\n"
                "    import cv2\n"
                "\n"
                "def f(): pass\n"
            ),
        )
        imports = get_module_imports(mod.f)
        cv2_imp = next(i for i in imports if i.bound_name == "cv2")
        assert cv2_imp.worker_only is True
        assert cv2_imp.package == "opencv-python-headless"


# -- End-to-end through the dependency-resolution pipeline ------------------

def _node_with(imports: list[ImportInfo]) -> "tuple[Store, str]":
    from pyfuse.core.models import FunctionNode
    node = FunctionNode(
        qualified_name="m.f",
        name="f",
        module="m",
        source="def f():\n    pass\n",
        imports=imports,
        dependencies=[],
    )
    store = Store()
    h = store.put(node)
    store.set_ref("m.f", h)
    return store, "f"


class TestPipeline:
    def test_worker_only_imports_treated_as_third_party(self) -> None:
        store, name = _node_with([
            ImportInfo("import requests", "requests", worker_only=True),
            ImportInfo("import os", "os"),
        ])
        modules = extract_third_party_modules(store, name)
        assert modules == {"requests"}

    def test_worker_only_package_hint_used(self) -> None:
        store, name = _node_with([
            ImportInfo(
                "import cv2", "cv2",
                package="opencv-python-headless",
                worker_only=True,
            ),
        ])
        hints = _collect_package_hints(store, name)
        assert hints == {"cv2": "opencv-python-headless"}


class TestEndToEnd:
    def test_serialize_marks_worker_only(self, tmp_path: Path) -> None:
        create_module(
            tmp_path,
            "wo_e2e",
            (
                "from pyfuse import trace, worker_only_import\n"
                "with worker_only_import():\n"
                "    import some_missing_pkg_e2e\n"
                "\n"
                "@trace\n"
                "def go():\n"
                "    return some_missing_pkg_e2e.compute()\n"
            ),
        )
        graph_json = pyfuse.serialize()
        store = Store.from_json(graph_json)
        modules = extract_third_party_modules(store, "go")
        assert "some_missing_pkg_e2e" in modules
