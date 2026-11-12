"""Build an offline GeoJSON dataset of German OSM bicycle route relations."""
import argparse
from contextlib import closing
import gzip
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import osmium


def bicycle_routes(source, target, date):
    with TemporaryDirectory(prefix="radrouten-", dir=target.parent) as scratch:
        extract = Path(scratch) / "bicycle.osm.pbf"
        with osmium.BackReferenceWriter(extract, source, remove_tags=False, relation_depth=0) as writer:
            for relation in osmium.FileProcessor(source, osmium.osm.RELATION):
                if relation.tags.get("type") == "route" and relation.tags.get("route") == "bicycle":
                    writer.add(relation)

        with closing(sqlite3.connect(Path(scratch) / "geometry.sqlite")) as db, target.open("xb") as raw:
            db.execute("CREATE TABLE ways(id INTEGER PRIMARY KEY, geometry TEXT NOT NULL)")
            db.execute("PRAGMA journal_mode=OFF")
            counts = {"routes": 0, "missing_way_members": 0, "ways_with_gaps": 0}
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as archive:
                archive.write(b'{"type":"FeatureCollection","features":[')
                for obj in osmium.FileProcessor(extract).with_locations("sparse_mem_array"):
                    if obj.is_way():
                        lines, line = [], []
                        has_gap = False
                        for node in obj.nodes:
                            if node.location.valid():
                                line.append([round(node.lon, 6), round(node.lat, 6)])
                            else:
                                if len(line) >= 2:
                                    lines.append(line)
                                line = []
                                has_gap = True
                        if len(line) >= 2:
                            lines.append(line)
                        counts["ways_with_gaps"] += has_gap
                        if lines:
                            db.execute("INSERT INTO ways VALUES(?,?)", (obj.id, json.dumps(lines)))
                    elif obj.is_relation() and obj.tags.get("route") == "bicycle":
                        lines = []
                        for member in obj.members:
                            if member.type != "w":
                                continue
                            row = db.execute("SELECT geometry FROM ways WHERE id=?", (member.ref,)).fetchone()
                            if row:
                                lines.extend(json.loads(row[0]))
                            else:
                                counts["missing_way_members"] += 1
                        if not lines:
                            continue
                        properties = {key: obj.tags[key] for key in ("name", "ref", "network") if key in obj.tags}
                        properties["relation"] = obj.id
                        feature = dict(type="Feature", properties=properties,
                                       geometry=dict(type="MultiLineString", coordinates=lines))
                        if counts["routes"]:
                            archive.write(b",")
                        archive.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                        counts["routes"] += 1
                archive.write(b"]}\n")
    if not counts["routes"]:
        raise ValueError("No bicycle routes extracted")
    with gzip.open(target, "rb") as archive:
        while archive.read(1024 * 1024):
            pass  # Check the complete gzip stream, including the trailer.
    (target.parent / "NOTICE.txt").write_text(
        f"OSM bicycle route relations in the German extract, {date}. Border routes can be incomplete.\n"
        "© OpenStreetMap contributors · ODbL 1.0\nhttps://www.openstreetmap.org/copyright\n"
        "https://opendatacommons.org/licenses/odbl/1-0/\n", encoding="utf-8")
    print(json.dumps(counts), flush=True)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bicycle_routes(args.source, args.output, args.date)


if __name__ == "__main__":
    main()
