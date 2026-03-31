"""Serialize only a subgraph instead of the full registry."""
import asyncio
import csv
import json
import logging

from pyfuse import FuseGraph, reconstruct, serialize, trace

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, format="%(levelname)-5s - %(name)s - %(message)s")


@trace
async def load_to_json(path: str) -> dict:
    raw_csv = await load_csv(path)
    dict_csv = {i: l for i, l in enumerate(raw_csv)}
    return to_json(dict_csv)

def to_json(data: object) -> str:
    """Serialize data to JSON."""
    return json.dumps(data, indent=2)

async def read_file(path: str) -> str:
    """Read a file from disk."""
    return await asyncio.to_thread(_read_file_sync, path)

def _read_file_sync(path: str) -> str:
    with open(path) as f:
        return f.read()

@trace
async def load_csv(path: str) -> list[list[str]]:
    """Parse CSV text into rows."""
    data = await read_file(path)
    return list(csv.reader(data.splitlines()))


async def main() -> None:
    # Serialize the full graph (all 4 functions)
    full_graph = serialize()
    import pprint
    pprint.pprint(json.loads(full_graph), width=140)

    print("\n\n--------\n")
    # Serialize only parse_csv and its dependencies (just parse_csv itself)
    sub_graph = serialize(load_csv)
    pprint.pprint(json.loads(sub_graph), width=140)

    # You can also pass the function by name
    # sub_graph2 = FuseGraph.default().serialize("to_json")
    source = reconstruct(sub_graph, "load_csv")
    print("=== Reconstructed: load_csv (from subgraph) ===")
    print(source)


if __name__ == "__main__":
    asyncio.run(main())
