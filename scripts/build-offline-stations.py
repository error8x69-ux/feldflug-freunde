"""Extract named railway station and halt nodes from an ODbL OpenStreetMap PBF."""
import argparse
from pathlib import Path

import osmium


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    stations = osmium.FileProcessor(args.source, osmium.osm.NODE).with_filter(
        osmium.filter.TagFilter(("railway", "station"), ("railway", "halt"))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        for node in stations:
            name = node.tags.get("name:de") or node.tags.get("name")
            if name and node.location.valid():
                city = node.tags.get("addr:city", "")
                fields = [" ".join(value.split()) for value in (name, city)]
                stream.write("\t".join((*fields, f"{node.location.lat:.6f}", f"{node.location.lon:.6f}")) + "\n")


if __name__ == "__main__":
    main()
