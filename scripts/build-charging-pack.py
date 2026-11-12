"""Build Feldflug's offline charging-station data from an OpenStreetMap PBF (ODbL)."""
import argparse
import gzip
import json
from pathlib import Path

import osmium


def has_socket(tags, key):
    value = tags.get(f"socket:{key}", "").lower()
    return value == "yes" or (value.isdecimal() and int(value) > 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--date", required=True, help="Date of the source extract (ISO 8601)")
    args = parser.parse_args()
    nodes = osmium.FileProcessor(args.source, osmium.osm.NODE).with_filter(
        osmium.filter.TagFilter(("amenity", "charging_station"))
    )
    stations = []
    for node in nodes:
        tags = node.tags
        if not node.location.valid() or tags.get("access", "") not in ("", "yes", "public", "permissive"):
            continue
        schuko = has_socket(tags, "schuko") and tags.get("bicycle") not in ("no", "private")
        type2 = has_socket(tags, "type2")
        bike = tags.get("bicycle") in ("yes", "designated", "permissive")
        if not schuko and not type2 and not bike:
            continue
        stations.append(dict(
            id=node.id, lat=round(node.location.lat, 6), lon=round(node.location.lon, 6),
            schuko=schuko, type2=type2, bike=bike, type2Cable=has_socket(tags, "type2_cable"),
            name=(tags.get("name") or tags.get("operator") or "")[:160],
            hours=tags.get("opening_hours", "")[:160],
            fee=tags.get("fee", "") if tags.get("fee", "") in ("yes", "no") else "",
        ))
    if not stations:
        parser.error("no compatible charging stations found")
    payload = dict(schema=1, date=args.date, license="ODbL-1.0",
                   attribution="© OpenStreetMap contributors",
                   licenseUrl="https://opendatacommons.org/licenses/odbl/1-0/",
                   source="https://download.geofabrik.de/europe/germany.html", stations=stations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    args.output.write_bytes(gzip.compress(data, mtime=0) if args.output.suffix == ".gz" else data)
    print(f"{len(stations)} stations, {sum(s['schuko'] for s in stations)} Schuko, "
          f"{sum(s['type2'] for s in stations)} Type 2, {args.output.stat().st_size} bytes")


if __name__ == "__main__":
    main()
