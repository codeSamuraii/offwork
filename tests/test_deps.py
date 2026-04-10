from __future__ import annotations

import pytest

from pyfuse.worker.deps import (
    DEFAULT_IMPORT_TO_PACKAGE,
    InstallResult,
    _collect_package_hints,
    _extract_top_module,
    extract_third_party_modules,
    install_package_as,
    is_installed,
)
from pyfuse.core.models import FunctionNode, ImportInfo
from pyfuse.graph.store import Store


# -- Helpers -----------------------------------------------------------------

def _node(
    name: str,
    imports: list[ImportInfo] | None = None,
) -> FunctionNode:
    return FunctionNode(
        qualified_name=f"m.{name}",
        name=name,
        module="m",
        source=f"def {name}():\n    pass\n",
        imports=imports or [],
        dependencies=[],
        owner_class=None,
        closure_vars={},
        closure_func_refs={},
    )


def _store_with_imports(*import_statements: str) -> Store:
    """Build a single-function store with the given imports."""
    imports = [
        ImportInfo(statement=s, bound_name=s.split()[-1])
        for s in import_statements
    ]
    node = _node("f", imports=imports)
    store = Store()
    h = store.put(node)
    store.set_ref("m.f", h)
    return store


# -- _extract_top_module -----------------------------------------------------

class TestExtractTopModule:
    def test_simple_import(self) -> None:
        assert _extract_top_module("import os") == "os"

    def test_dotted_import(self) -> None:
        assert _extract_top_module("import os.path") == "os"

    def test_from_import(self) -> None:
        assert _extract_top_module("from collections import OrderedDict") == "collections"

    def test_from_submodule_import(self) -> None:
        assert _extract_top_module("from collections.abc import Callable") == "collections"

    def test_import_as(self) -> None:
        assert _extract_top_module("import numpy as np") == "numpy"

    def test_from_import_as(self) -> None:
        assert _extract_top_module("from datetime import datetime as dt") == "datetime"

    def test_invalid_raises(self) -> None:
        with pytest.raises((ValueError, IndexError)):
            _extract_top_module("x = 1")


# -- extract_third_party_modules --------------------------------------------

class TestExtractThirdPartyModules:
    def test_stdlib_filtered(self) -> None:
        store = _store_with_imports("import os", "import json", "import sys")
        modules = extract_third_party_modules(store, "f")
        assert modules == set()

    def test_third_party_kept(self) -> None:
        store = _store_with_imports("import os", "import requests")
        modules = extract_third_party_modules(store, "f")
        assert modules == {"requests"}

    def test_from_third_party(self) -> None:
        store = _store_with_imports("from numpy import array")
        modules = extract_third_party_modules(store, "f")
        assert modules == {"numpy"}

    def test_deduplication_across_deps(self) -> None:
        """Same module imported by two functions in the subgraph."""
        a = _node("a", imports=[ImportInfo("import requests", "requests")])
        b = _node("b", imports=[ImportInfo("import requests", "requests")])
        store = Store()
        ha, hb = store.put(a), store.put(b)
        store.set_ref("m.a", ha)
        store.set_ref("m.b", hb)
        store.set_deps(ha, [hb])
        modules = extract_third_party_modules(store, "a")
        assert modules == {"requests"}


# -- is_installed ------------------------------------------------------------

class TestIsInstalled:
    def test_stdlib_installed(self) -> None:
        assert is_installed("os") is True

    def test_nonexistent(self) -> None:
        assert is_installed("nonexistent_package_xyz_123") is False

    def test_pytest_installed(self) -> None:
        assert is_installed("pytest") is True


# -- DEFAULT_IMPORT_TO_PACKAGE -----------------------------------------------

class TestDefaultMapping:
    def test_known_mappings(self) -> None:
        assert DEFAULT_IMPORT_TO_PACKAGE["PIL"] == "Pillow"
        assert DEFAULT_IMPORT_TO_PACKAGE["sklearn"] == "scikit-learn"
        assert DEFAULT_IMPORT_TO_PACKAGE["cv2"] == "opencv-python"
        assert DEFAULT_IMPORT_TO_PACKAGE["yaml"] == "PyYAML"
        assert DEFAULT_IMPORT_TO_PACKAGE["bs4"] == "beautifulsoup4"


# -- install_package_as -----------------------------------------------------

class TestInstallPackageAs:
    def test_context_manager_is_noop(self) -> None:
        """The context manager should not interfere with the import."""
        with install_package_as("opencv-python"):
            import json  # noqa: F811
        assert json is not None

    def test_package_stored_in_import_info(self) -> None:
        imp = ImportInfo("import cv2", "cv2", package="opencv-python")
        assert imp.package == "opencv-python"

    def test_package_roundtrips_through_dict(self) -> None:
        imp = ImportInfo("import cv2", "cv2", package="opencv-python")
        d = imp.to_dict()
        assert d["package"] == "opencv-python"
        restored = ImportInfo.from_dict(d)
        assert restored.package == "opencv-python"

    def test_package_none_omitted_from_dict(self) -> None:
        imp = ImportInfo("import os", "os")
        d = imp.to_dict()
        assert "package" not in d

    def test_package_none_from_dict_without_key(self) -> None:
        restored = ImportInfo.from_dict({"statement": "import os", "bound_name": "os"})
        assert restored.package is None


# -- _collect_package_hints --------------------------------------------------

class TestCollectPackageHints:
    def test_collects_hints(self) -> None:
        node = _node("f", imports=[
            ImportInfo("import cv2", "cv2", package="opencv-python"),
            ImportInfo("import os", "os"),
        ])
        store = Store()
        h = store.put(node)
        store.set_ref("m.f", h)
        hints = _collect_package_hints(store, "f")
        assert hints == {"cv2": "opencv-python"}

    def test_no_hints_when_no_packages(self) -> None:
        store = _store_with_imports("import os", "import json")
        hints = _collect_package_hints(store, "f")
        assert hints == {}

    def test_hints_from_transitive_deps(self) -> None:
        a = _node("a", imports=[ImportInfo("import requests", "requests")])
        b = _node("b", imports=[
            ImportInfo("import cv2", "cv2", package="opencv-python"),
        ])
        store = Store()
        ha, hb = store.put(a), store.put(b)
        store.set_ref("m.a", ha)
        store.set_ref("m.b", hb)
        store.set_deps(ha, [hb])
        hints = _collect_package_hints(store, "a")
        assert hints == {"cv2": "opencv-python"}
