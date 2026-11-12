"""Build and verify test-only Feldflug offline data from upstreams or local fixtures."""
import argparse
from datetime import datetime
import gzip
import hashlib
import json
import math
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from time import monotonic
from urllib.request import Request, urlopen
from zipfile import ZIP_STORED, ZipFile

SCRIPTS = Path(__file__).resolve().parent
BBOX = "7.10,50.10,7.30,50.23"
PLANET = "https://tiles.openfreemap.org/planet"
GEOFABRIK = "https://download.geofabrik.de/europe/germany/rheinland-pfalz-latest.osm.pbf"
RD5 = "https://brouter.de/brouter/segments4/E5_N50.rd5"
GEONAMES = "https://download.geonames.org/export/zip/DE.zip"
RELATIONS = (4281, 113708)
MAX_ASSET = 2 * 1024**3


def request(url):
    return urlopen(Request(url, headers={"User-Agent": "Feldflug offline data probe"}), timeout=60)


def download(url, target, limit):
    with request(url) as response, target.open("xb") as output:
        expected = response.headers.get("Content-Length")
        if expected is not None and not 0 < int(expected) <= limit:
            raise ValueError(f"Unexpected Content-Length for {url}: {expected}")
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError(f"Download limit exceeded: {url}")
            output.write(chunk)
        if size == 0 or (expected is not None and size != int(expected)):
            raise ValueError(f"Incomplete download: {url}")


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def script(name, *args):
    run(sys.executable, SCRIPTS / name, *args)


def map_packages(args, work, output):
    if args.pmtiles_input:
        source = args.pmtiles_input.resolve()
        label = f"local fixture: {source.name}"
    else:
        with request(PLANET) as response:
            tilejson = json.load(response)
        template = next((tile for tile in tilejson["tiles"] if re.fullmatch(
            r"https://tiles\.openfreemap\.org/planet/\d{8}_\d{6}_pt/\{z\}/\{x\}/\{y\}\.pbf", tile)), None)
        if not template:
            raise ValueError("OpenFreeMap returned no recognized dataset")
        dataset = template.split("/")[4]
        source = f"https://btrfs.openfreemap.com/areas/planet/{dataset}/tiles.pmtiles"
        label = f"OpenFreeMap {dataset}"
    extract = work / "cochem.pmtiles"
    run(args.pmtiles_tool, "extract", source, extract, f"--bbox={BBOX}", "--minzoom=9", "--maxzoom=14")
    run(args.pmtiles_tool, "verify", extract)
    tools = runpy.run_path(str(SCRIPTS / "repack-cochem-pmtiles.py"))
    _, tiles, zooms, _ = tools["archive"](extract)
    if zooms != bytes((9, 14)):
        raise ValueError(f"Unexpected map zooms: {list(zooms)}")
    xyz = work / "xyz.zip"
    with ZipFile(xyz, "x", ZIP_STORED) as archive:
        for tile, data in tiles:
            z, x, y = tools["zxy"](tile)
            archive.writestr(f"tiles/{z}/{x}/{y}.pbf", data)
    script("pack-fine-grid-maps.py", output / "karte", xyz, "--source", label)
    return str(source) if not args.pmtiles_input else label


def routing_package(args, output):
    output.mkdir()
    segment = output / "E5_N50.rd5"
    if args.rd5_input:
        shutil.copyfile(args.rd5_input, segment)
    else:
        download(RD5, segment, MAX_ASSET - 1)
    run("java", "-cp", SCRIPTS.parent / "libs/brouter-core-1.7.9.jar",
        SCRIPTS / "verify-map-update-rd5.java", segment)
    return "local fixture" if args.rd5_input else RD5


def cycle_packages(args, work, output):
    sources = [f"https://api.openstreetmap.org/api/0.6/relation/{relation}/full" for relation in RELATIONS]
    source = args.cycle_input
    if source is None:
        source = work / "osm-relations"
        source.mkdir()
        for relation, url in zip(RELATIONS, sources):
            download(url, source / f"{relation}.osm", 25 * 1024**2)
    script("build-cycle-route-pack.py", source, work / "cycle-assets", output,
           "--date", args.run_date)
    return "local fixtures" if args.cycle_input else sources


def osm_packages(args, work, output):
    import osmium

    pbf = args.osm_input or work / "rheinland-pfalz.osm.pbf"
    if args.osm_input is None:
        download(GEOFABRIK, pbf, 1024**3)
    with osmium.io.Reader(str(pbf)) as reader:
        timestamp = reader.header().get("osmosis_replication_timestamp")
    date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
    source = f"local fixture: {pbf.name}" if args.osm_input else GEOFABRIK
    sqlite = work / "search.sqlite"
    script("build-search-pack.py", pbf, sqlite, "--region", "Rheinland-Pfalz", "--date", date, "--source", source)
    search_manifest = json.loads(sqlite.with_suffix(".json").read_text(encoding="utf-8"))
    if search_manifest["entries"] <= 0:
        raise ValueError("Empty search database")
    search = output / "suche"
    script("split-search-pack.py", sqlite, search)
    for database in search.glob("*.sqlite"):
        database.unlink()
    stations = output / "bahnhoefe" / "stations.tsv"
    script("build-offline-stations.py", pbf, stations)
    if not stations.stat().st_size:
        raise ValueError("No stations extracted")
    charging = output / "ladepunkte" / "charging.json.gz"
    script("build-charging-pack.py", pbf, charging, "--date", date)
    with gzip.open(charging, "rt", encoding="utf-8") as data:
        if not json.load(data)["stations"]:
            raise ValueError("No charging stations extracted")
    return {"source": source, "osm_timestamp": timestamp}


def postal_package(args, work, output):
    source = args.postal_input or work / "DE.zip"
    if args.postal_input is None:
        download(GEONAMES, source, 5 * 1024**2)
    entries = set()
    with ZipFile(source) as archive, archive.open("DE.txt") as rows:
        for row in rows:
            country, postcode, name, state, _, _, _, _, _, lat, lon, _ = (
                row.decode("utf-8").rstrip("\r\n").split("\t"))
            if country != "DE" or not re.fullmatch(r"\d{5}", postcode) or not name or not state:
                continue
            if not lat or not lon:
                continue
            latitude, longitude = float(lat), float(lon)
            if not (math.isfinite(latitude) and 47 <= latitude <= 56 and
                    math.isfinite(longitude) and 5 <= longitude <= 16):
                continue
            entries.add((name, postcode, state, lat, lon))
    if not entries:
        raise ValueError("No German postal places extracted")
    output.mkdir()
    (output / "de-postal-places.tsv").write_text(
        "".join("\t".join(entry) + "\n" for entry in sorted(entries)), encoding="utf-8")
    (output / "NOTICE.txt").write_text(
        "GeoNames · CC BY 4.0 · reduced to German name, postcode, state and coordinates.\n"
        f"{GEONAMES}\nhttps://creativecommons.org/licenses/by/4.0/\n", encoding="utf-8")
    print(f"{len(entries)} German postal places", flush=True)
    return f"local fixture: {source.name}" if args.postal_input else GEONAMES


def write_report(output, run_date, sources):
    assets = []
    (output / "NOTICE.txt").write_text(
        "Feldflug test data. This is a regional sample, not an app update.\n"
        "© OpenStreetMap contributors · ODbL 1.0\n"
        "https://www.openstreetmap.org/copyright\n"
        "https://opendatacommons.org/licenses/odbl/1-0/\n"
        "Map: OpenFreeMap / OpenMapTiles. Routing: BRouter. Regional OSM extract: Geofabrik.\n"
        "See testbericht.json for input sources and file hashes.\n", encoding="utf-8")
    for file in sorted(output.rglob("*")):
        if not file.is_file() or file == output / "testbericht.json":
            continue
        size = file.stat().st_size
        if not 0 < size < MAX_ASSET:
            raise ValueError(f"Invalid test file size: {file} ({size})")
        with file.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        assets.append(dict(file=file.relative_to(output).as_posix(), bytes=size, sha256=digest))
    if len(assets) + 1 > 1000:
        raise ValueError("Test release exceeds the GitHub Release asset limit")
    report = {"schema": 1, "test_only": True, "run_date": run_date,
              "map_bbox": BBOX, "sources": sources, "files": assets}
    (output / "testbericht.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    print(f"{len(assets)} verified test files, {sum(item['bytes'] for item in assets):,} bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path, help="New work directory")
    parser.add_argument("--pmtiles-tool", default="pmtiles")
    parser.add_argument("--pmtiles-input", type=Path)
    parser.add_argument("--rd5-input", type=Path)
    parser.add_argument("--cycle-input", type=Path)
    parser.add_argument("--osm-input", type=Path)
    parser.add_argument("--postal-input", type=Path)
    parser.add_argument("--run-date", required=True, help="Run date in YYYY-MM-DD")
    args = parser.parse_args()
    datetime.strptime(args.run_date, "%Y-%m-%d")
    args.work.mkdir(parents=True, exist_ok=False)
    output = args.work / "testpakete"
    output.mkdir()

    phases = {
        "karte": lambda: map_packages(args, args.work, output),
        "routing": lambda: routing_package(args, output / "routing"),
        "radrouten": lambda: cycle_packages(args, args.work, output / "radrouten"),
        "postleitzahlen": lambda: postal_package(args, args.work, output / "postleitzahlen"),
        "osm-extrakt": lambda: osm_packages(args, args.work, output),
    }
    sources = {}
    for name, build in phases.items():
        start = monotonic()
        sources[name] = {"source": build(), "seconds": round(monotonic() - start, 1)}
        print(f"{name}: {sources[name]['seconds']} s, free disk {shutil.disk_usage(args.work).free:,} bytes", flush=True)
    write_report(output, args.run_date, sources)


if __name__ == "__main__":
    main()
