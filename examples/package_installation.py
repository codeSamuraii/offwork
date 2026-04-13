"""Run functions that depend on third-party packages on a bare worker.

The worker has no packages pre-installed (besides the standard library).
pyfuse detects the imports, installs the packages via pip, and executes
the function -- all automatically.

Requires Redis on localhost:6379.  Install: pip install redis

Usage:
    # Terminal 1 -- start a worker
    python -m pyfuse worker --backend redis://localhost:6379

    # Terminal 2 -- run this script
    python examples/package_installation.py
"""

import asyncio
from html.parser import HTMLParser

import requests
import markdown

import pyfuse
from pyfuse import trace
from pyfuse import install_package_as

# Some packages have different import and pip names:
#   import yaml       -> pip install PyYAML
#   import cv2        -> pip install opencv-python
#   import PIL        -> pip install Pillow
#
# Common mappings are built-in. For others, use install_package_as:
with install_package_as("PyYAML"):
    import yaml

with install_package_as("python-dateutil"):
    from dateutil import parser as date_parser


# -- Simple case: import name matches package name --------------------------

# When the import name matches the pip package (e.g. `import requests`),
# no extra configuration is needed. The worker runs `pip install requests`.


@trace
def fetch_title(url: str) -> str:
    """Fetch a web page and extract the <title> tag."""
    class TitleParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.in_title = False
            self.title = ""

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.in_title = tag == "title"

        def handle_data(self, data: str) -> None:
            if self.in_title:
                self.title += data

        def handle_endtag(self, tag: str) -> None:
            if tag == "title":
                self.in_title = False

    resp = requests.get(url, timeout=10)
    parser = TitleParser()
    parser.feed(resp.text)
    return parser.title.strip()


# -- Mismatched names: install_package_as -----------------------------------


@trace
def to_yaml(data: object) -> str:
    """Convert a Python object to YAML. Worker installs PyYAML automatically."""
    return yaml.dump(data, default_flow_style=False)


@trace
def parse_date(text: str) -> str:
    """Parse a human-readable date string. Worker installs python-dateutil."""
    dt = date_parser.parse(text)
    return dt.isoformat()


# -- Multiple packages in one function -------------------------------------


@trace
def markdown_word_freq(md_text: str) -> str:
    """Strip markdown, count word frequencies, return as YAML.

    Requires both 'markdown' and 'PyYAML' -- the worker installs both.
    """
    class TextExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

    html = markdown.markdown(md_text)
    extractor = TextExtractor()
    extractor.feed(html)
    plain = " ".join(extractor.parts).lower()

    words = plain.split()
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1

    return yaml.dump(dict(sorted(freq.items(), key=lambda x: x[1], reverse=True)))


# ---------------------------------------------------------------------------

async def main() -> None:
    pyfuse.connect("local://localhost:9748")

    # Simple package (import name == pip name)
    print("--- requests (auto-detected) ---")
    title = await fetch_title.run("http://example.com")
    print(f"  Page title: {title}")

    # Mismatched package names
    print("\n--- PyYAML (install_package_as) ---")
    result = await to_yaml.run({"framework": "pyfuse", "version": "0.4.0"})
    print(f"  YAML output:\n{result}")

    print("--- python-dateutil (install_package_as) ---")
    iso = await parse_date.run("January 5th, 2024 at 3pm")
    print(f"  Parsed date: {iso}")

    # Multiple packages in one function
    print("\n--- Multiple packages (markdown + PyYAML) ---")
    md = "# Hello\n\nThis is a **test** of the word frequency counter."
    freq = await markdown_word_freq.run(md)
    print(f"  Word frequencies:\n{freq}")


if __name__ == "__main__":
    asyncio.run(main())
