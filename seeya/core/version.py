from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

_FALLBACK_VERSION = "0.4.0"

try:
    _VERSION: str = _pkg_version("seeya")
except PackageNotFoundError:
    # Not installed as a package (e.g. running from source checkout).
    _VERSION = _FALLBACK_VERSION
