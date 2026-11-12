"""Split existing OpenFreeMap XYZ archives into small, lossless map downloads."""
import argparse
from collections import defaultdict
from contextlib import ExitStack
import gzip
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("archives", type=Path, nargs="+")
    parser.add_argument("--cell", action="append", help="Only produce this z10 cell, written as x-y")
    parser.add_argument("--source", default="OpenFreeMap 20261101_080013_pt", help="Source dataset label for the manifest")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    selected = set(args.cell or [])
    with ExitStack() as stack:
        sources = [stack.enter_context(ZipFile(path)) for path in args.archives]
        tiles = defaultdict(dict)
        parents = {}
        for source in sources:
            for entry in source.infolist():
                if not entry.filename.startswith("tiles/") or not entry.filename.endswith(".pbf"):
                    continue
                z, x, y = map(int, entry.filename.removesuffix(".pbf").split("/")[1:])
                if z < 9:
                    continue
                if z == 9:
                    group = parents
                else:
                    cell = f"{x >> (z - 10)}-{y >> (z - 10)}"
                    if selected and cell not in selected:
                        continue
                    group = tiles[cell]
                previous = group.setdefault(entry.filename, (source, entry))
                if previous[1] is not entry:
                    original = previous[0].read(previous[1])
                    candidate = source.read(entry)
                    if original != candidate and gzip.decompress(original) != gzip.decompress(candidate):
                        raise ValueError(f"Conflicting source tile: {entry.filename}")

        packages = []
        for cell, entries in sorted(tiles.items()):
            x, y = map(int, cell.split("-"))
            parent = f"tiles/9/{x >> 1}/{y >> 1}.pbf"
            if parent in parents:
                entries[parent] = parents[parent]
            path = args.output / f"map-z10-{cell}.zip"
            installed = 0
            with ZipFile(path, "x", ZIP_STORED) as output:
                for name, (source, entry) in sorted(entries.items()):
                    payload = source.read(entry)
                    output.writestr(name, payload)
                    raw_size = len(gzip.decompress(payload))
                    installed += (raw_size + 4095) // 4096 * 4096
            with ZipFile(path) as result:
                if result.testzip() is not None or len(result.infolist()) != len(entries):
                    raise ValueError(f"Invalid map package: {path}")
                for name, (source, entry) in entries.items():
                    if result.read(name) != source.read(entry):
                        raise ValueError(f"Tile bytes changed: {name}")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            packages.append(dict(id=path.stem, file=path.name, bytes=path.stat().st_size,
                                 installed_bytes=installed, sha256=digest, tiles=len(entries)))

    if not packages:
        raise ValueError("No detail tiles found for the selected area")
    manifest = dict(schema=1, cell_zoom=10, min_detail_zoom=9, maxzoom=14,
                    source=args.source,
                    attribution="© OpenStreetMap contributors · © OpenMapTiles · OpenFreeMap",
                    license="ODbL-1.0", packages=packages)
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(packages)} verified packages, {sum(p['bytes'] for p in packages):,} bytes")


if __name__ == "__main__":
    main()
