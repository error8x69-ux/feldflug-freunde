"""Stage versioned Feldflug datasets for Germany and the existing border search area."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import gzip
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import runpy
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import urljoin, urlsplit
from zipfile import ZIP_STORED, ZipFile

SCRIPTS = Path(__file__).resolve().parent
PROBE = runpy.run_path(str(SCRIPTS / "probe-offline-data.py"))
download, request, run, script = (PROBE[key] for key in ("download", "request", "run", "script"))
GEOFABRIK = "https://download.geofabrik.de"
INDEX = f"{GEOFABRIK}/index-v1.json"
REGIONS = ("germany", "austria", "switzerland", "netherlands", "belgium", "luxembourg",
           "denmark", "poland", "czech-republic", "france/alsace", "france/lorraine")
ROUTING = ("E5_N45", "E5_N50", "E5_N55", "E10_N45", "E10_N50", "E10_N55", "E15_N50")
RELEASES = "https://github.com/error8x69-ux/feldflug-freunde/releases/download"
MAP_ASSETS = f"{RELEASES}/karte-de-rest-20261101/common.zip"
MAP_ASSETS_HASH = "ebd773ed724a6d06fa57a94285348ed457ad033a0707348324b8849ae069c635"
MAP_BASELINE = f"{RELEASES}/karte-de-fein-20261101/manifest.json"
SEARCH_BASELINE = f"{RELEASES}/suchdaten-de-kompakt-2026-11-10-1/manifest.json"
REGISTER_PAGE = "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/DownloadundKontakt.html"
OSM_LICENSE = ("© OpenStreetMap contributors · ODbL 1.0\n"
               "https://www.openstreetmap.org/copyright\n"
               "https://opendatacommons.org/licenses/odbl/1-0/\n")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def remote_json(url):
    with request(url) as response:
        return json.load(response)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def country_geometry(args):
    index = read_json(args.geofabrik_index) if args.geofabrik_index else remote_json(INDEX)
    return next(feature["geometry"] for feature in index["features"] if feature["properties"]["id"] == "germany")


def polygons(geometry):
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    raise ValueError("Expected polygonal Geofabrik coverage")


def map_build(args, work, output):
    if args.map_extract:
        inputs, dataset = args.map_extract, args.map_dataset
        if not dataset or not re.fullmatch(r"\d{8}_\d{6}_pt", dataset):
            raise ValueError("Provide --map-dataset for the local extracts")
        source = [path.name for path in inputs]
    else:
        templates = remote_json(PROBE["PLANET"])["tiles"]
        dataset = next((match[1] for template in templates if (match := re.fullmatch(
            r"https://tiles\.openfreemap\.org/planet/(\d{8}_\d{6}_pt)/\{z\}/\{x\}/\{y\}\.pbf", template))), None)
        if dataset is None:
            raise ValueError("OpenFreeMap returned no recognized dataset")
        source = f"https://btrfs.openfreemap.com/areas/planet/{dataset}/tiles.pmtiles"
        germany = work / "germany.geojson"
        write_json(germany, country_geometry(args))
        inputs = [work / "germany.pmtiles", work / "rlp.pmtiles"]
        run(args.pmtiles_tool, "extract", source, inputs[0], f"--region={germany}", "--minzoom=0", "--maxzoom=14")
        run(args.pmtiles_tool, "extract", source, inputs[1], "--bbox=6.10,48.94,8.55,51.00", "--minzoom=0", "--maxzoom=14")

    common = args.common_map or work / "common-source.zip"
    if not args.common_map:
        download(MAP_ASSETS, common, 100 * 1024**2)
    if digest(common) != MAP_ASSETS_HASH:
        raise ValueError("Shared OpenFreeMap assets do not match the verified release")
    output.mkdir()
    with ZipFile(common) as source_assets, ZipFile(output / "common.zip", "x", ZIP_STORED) as dest:
        for entry in source_assets.infolist():
            if not entry.is_dir() and not entry.filename.startswith("tiles/"):
                dest.writestr(entry, source_assets.read(entry), compress_type=entry.compress_type)

    tools = runpy.run_path(str(SCRIPTS / "repack-cochem-pmtiles.py"))
    xyz_files = []
    overview = {}
    overview_path = output / "de-overview-de-rest.zip"
    with ZipFile(overview_path, "x", ZIP_STORED) as dest:
        for index, path in enumerate(inputs):
            run(args.pmtiles_tool, "verify", path)
            _, tiles, zooms, _ = tools["archive"](path)
            if zooms != bytes((0, 14)):
                raise ValueError(f"Unexpected map zooms in {path}: {list(zooms)}")
            xyz = work / f"xyz-{index}.zip"
            xyz_files.append(xyz)
            with ZipFile(xyz, "x", ZIP_STORED) as archive:
                for tile, data in tiles:
                    z, x, y = tools["zxy"](tile)
                    name = f"tiles/{z}/{x}/{y}.pbf"
                    if z >= 9:
                        archive.writestr(name, data)
                    else:
                        sha = hashlib.sha256(gzip.decompress(data)).digest()
                        previous = overview.setdefault(name, sha)
                        if previous != sha:
                            raise ValueError(f"Conflicting overview tile: {name}")
                        if name not in dest.NameToInfo:
                            dest.writestr(name, data)
            if not args.map_extract:
                path.unlink()

    shared = []
    for file in (output / "common.zip", overview_path):
        with ZipFile(file) as package:
            if package.testzip() is not None:
                raise ValueError(f"Invalid archive: {file}")
            sizes = (len(gzip.decompress(package.read(entry))) if entry.filename.startswith("tiles/")
                     else entry.file_size for entry in package.infolist())
            installed = sum((size + 4095) // 4096 * 4096 for size in sizes)
        shared.append(dict(id=file.stem, file=file.name, bytes=file.stat().st_size,
                           installed_bytes=installed, sha256=digest(file)))
    if not overview:
        raise ValueError("No overview tiles")
    write_json(output / "shared-map.json", shared)
    script("pack-fine-grid-maps.py", output / "detail", *xyz_files, "--source", f"OpenFreeMap {dataset}")
    baseline = read_json(args.map_baseline) if args.map_baseline else remote_json(MAP_BASELINE)
    details = read_json(output / "detail/manifest.json")["packages"]
    expected = {row["id"] for row in baseline["packages"]}
    actual = {row["id"] for row in details}
    if len(expected) < 700 or expected - actual:
        raise ValueError(f"Offline map coverage regressed: {sorted(expected - actual)}")
    if any(row["bytes"] > 50_000_000 or row["installed_bytes"] > 100_000_000 for row in details):
        raise ValueError("At least one detail package exceeds the app's size limit")
    (output / "NOTICE.txt").write_text(
        f"OpenFreeMap / OpenMapTiles · {dataset}\n{OSM_LICENSE}"
        "Style, fonts and sprites reuse the verified common assets and their licenses.\n", encoding="utf-8")
    return dict(source=source, dataset=dataset, detail_packages=len(details), overview_tiles=len(overview))


def routing_build(args, work, output):
    output.mkdir()
    source = PROBE["RD5"].rsplit("/", 1)[0]
    for name in ROUTING:
        segment = output / f"{name}.rd5"
        if args.routing_dir:
            shutil.copyfile(args.routing_dir / segment.name, segment)
        else:
            download(f"{source}/{segment.name}", segment, PROBE["MAX_ASSET"] - 1)
        run("java", "-cp", SCRIPTS.parent / "libs/brouter-core-1.7.9.jar",
            SCRIPTS / "verify-map-update-rd5.java", segment)
    (output / "NOTICE.txt").write_text(f"BRouter segments4 · {source}\n{OSM_LICENSE}", encoding="utf-8")
    return dict(source=source if not args.routing_dir else "local copies", segments=list(ROUTING))


def region_url(region):
    return f"{GEOFABRIK}/europe/{region}-latest.osm.pbf"


def germany_url():
    url = f"{GEOFABRIK}/europe/germany-updates/state.txt"
    with request(url) as response:
        state = response.read().decode("ascii").replace("\\:", ":")
    match = re.search(r"^timestamp=(\d{4}-\d{2}-\d{2}T[^\r\n]+)$", state, re.MULTILINE)
    if match is None:
        raise ValueError("Geofabrik Germany state has no extract timestamp")
    date = datetime.fromisoformat(match[1].replace("Z", "+00:00")).strftime("%y%m%d")
    return f"{GEOFABRIK}/europe/germany-{date}.osm.pbf"


def osm_timestamp(path):
    import osmium

    with osmium.io.Reader(str(path)) as reader:
        timestamp = reader.header().get("osmosis_replication_timestamp")
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


@contextmanager
def extract_for(source, work):
    if "path" in source:
        yield Path(source["path"])
        return
    with TemporaryDirectory(prefix="pbf-", dir=work) as temp:
        path = Path(temp) / "source.osm.pbf"
        download(source["url"], path, 10 * 1024**3)
        yield path


def osm_build(args, work, output):
    source = dict(path=str(args.osm_input)) if args.osm_input else dict(url=germany_url())
    with extract_for(source, work) as pbf:
        timestamp = osm_timestamp(pbf)
        date = timestamp.date().isoformat()
        script("build-national-cycle-routes.py", pbf, output / "radrouten/radrouten-de.geojson.gz", "--date", date)
        stations = output / "bahnhoefe/offline-stations.tsv"
        script("build-offline-stations.py", pbf, stations)
        station_count = len(stations.read_bytes().splitlines())
        extension = output / "erweiterungen/ladestationen/charging-osm-de.json.gz"
        script("build-charging-pack.py", pbf, extension, "--date", date)
        with gzip.open(extension, "rt", encoding="utf-8") as raw:
            charging = json.load(raw)
        if station_count < 5000 or len(charging["stations"]) < 10000:
            raise ValueError("Nationwide station data is incomplete")
        write_json(extension.with_name("manifest.json"), dict(
            schema=1, id="charging-osm", date=date, file=extension.name, bytes=extension.stat().st_size,
            sha256=digest(extension), source=source.get("url", pbf.name),
            license="ODbL-1.0", attribution="© OpenStreetMap contributors"))
        (output / "NOTICE.txt").write_text(
            f"Geofabrik OSM extract · {timestamp.isoformat()}\n{OSM_LICENSE}", encoding="utf-8")
    return dict(source=source.get("url", pbf.name), osm_timestamp=timestamp.isoformat(),
                railway_stations=station_count, charging_stations=len(charging["stations"]))


def register_source():
    class Links(HTMLParser):
        files = []

        def handle_starttag(self, tag, attrs):
            href = dict(attrs).get("href")
            if tag != "a" or not href:
                return
            url = urljoin(REGISTER_PAGE, href)
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.netloc != "data.bundesnetzagentur.de":
                return
            match = re.search(r"/Ladesaeulenregister_BNetzA_(\d{4}-\d{2}-\d{2})\.csv$", parts.path)
            if match:
                self.files.append((datetime.fromisoformat(match[1]).date().isoformat(), url))

    links = Links()
    with request(REGISTER_PAGE) as response:
        links.feed(response.read().decode(response.headers.get_content_charset() or "utf-8"))
    if not links.files:
        raise ValueError("The Bundesnetzagentur download page has no dated CSV link")
    return max(links.files)


def register_build(args, work, output):
    date, url = register_source()
    csv_file = work / "register.csv"
    download(url, csv_file, 128 * 1024**2)
    tools = runpy.run_path(str(SCRIPTS / "combine-charging-sources.py"))
    stations = tools["register"](csv_file)
    if len(stations) < 50_000:
        raise ValueError("The nationwide charging register is incomplete")
    output.mkdir()
    payload = dict(date=date, license="CC-BY-4.0", attribution="Bundesnetzagentur.de",
                   licenseUrl="https://creativecommons.org/licenses/by/4.0/", source=url, stations=stations)
    file = output / "charging-register-de.json.gz"
    file.write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                                   mtime=0))
    with gzip.open(file, "rt", encoding="utf-8") as raw:
        if json.load(raw) != payload:
            raise ValueError("Charging register archive does not match the verified data")
    write_json(output / "manifest.json", dict(
        schema=1, id="charging-register", date=date, file=file.name, bytes=file.stat().st_size,
        sha256=digest(file), source=url, license=payload["license"], attribution=payload["attribution"],
        license_url=payload["licenseUrl"]))
    return dict(source=url, date=date, charging_stations=len(stations))


def verify_search_samples(output, work, packages):
    tile = runpy.run_path(str(SCRIPTS / "split-search-pack.py"))["tile"]
    samples = (("berlin", 52.52, 13.405), ("hamburg", 53.55, 9.99), ("muenchen", 48.137, 11.575),
               ("venlo", 51.37, 6.17), ("strasbourg", 48.58, 7.75), ("basel", 47.56, 7.59),
               ("salzburg", 47.80, 13.04), ("cheb", 50.08, 12.37))
    places = {(row["x"], row["y"]): row for row in packages if row["kind"] == "places"}
    for name, lat, lon in samples:
        x, y = tile(lat, lon)
        found = False
        for dx, dy in ((0, 0), (0, -1), (0, 1), (-1, 0), (1, 0), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            package = places.get((x + dx, y + dy))
            if package is None:
                continue
            path = work / "query.sqlite"
            with gzip.open(output / package["file"], "rb") as source, path.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                found = connection.execute(
                    "SELECT 1 FROM places WHERE type='city' AND abs(lat-?)<0.15 AND abs(lon-?)<0.2 "
                    "AND rowid IN (SELECT docid FROM search WHERE search MATCH ?) LIMIT 1",
                    (lat, lon, name + "*")).fetchone() is not None
            finally:
                connection.close()
            if found:
                break
        if not found:
            raise ValueError(f"Offline search did not find {name} near its known coordinates")
    return [name for name, _, _ in samples]


def search_build(args, work, output):
    baseline = read_json(args.search_baseline) if args.search_baseline else remote_json(SEARCH_BASELINE)
    if args.prepared_search:
        output.mkdir()
        manifest = read_json(args.prepared_search / "manifest.json")
        for package in manifest["packages"]:
            name = package["file"]
            if not re.fullmatch(r"search-10-\d{1,4}-\d{1,4}-(places|addresses)\.sqlite\.gz", name):
                raise ValueError(f"Unexpected package name: {name}")
            shutil.copyfile(args.prepared_search / name, output / name)
        write_json(output / "manifest.json", manifest)
        source = f"verified existing catalog: {args.prepared_search.name}"
    else:
        tools = runpy.run_path(str(SCRIPTS / "build-germany-search.py"))
        poly = work / "germany.poly"
        lines = ["germany"]
        for number, ring in enumerate(ring for polygon in polygons(country_geometry(args)) for ring in polygon):
            lines.extend([str(number), *(f" {lon} {lat}" for lon, lat in ring), "END"])
        poly.write_text("\n".join([*lines, "END", ""]), encoding="ascii")
        sources = (read_json(args.search_sources) if args.search_sources else
                   [dict(url=germany_url()), *(dict(url=region_url(region)) for region in REGIONS[1:])])
        if len(sources) != len(REGIONS):
            raise ValueError("Germany and all ten border sources are required")
        tools["build"](sources, tools["border_tiles"](poly), output, None, compact=True,
                       source_path=lambda item: extract_for(item, work), index_kind="sparse_mem_array",
                       date_from_path=lambda path: osm_timestamp(path).date().isoformat())
        manifest = read_json(output / "manifest.json")
        source = [item["url"] for item in sources]
    packages = manifest["packages"]
    if (manifest["schema"] != 2 or manifest["region"] != "Deutschland und Grenzgebiete" or
            manifest["license"] != "ODbL-1.0" or not 1800 <= len(packages) <= 4096 or
            (output / "manifest.json").stat().st_size > 2_000_000 or
            len({package["id"] for package in packages}) != len(packages)):
        raise ValueError("Nationwide search catalog is incomplete")
    baseline_places = {row["id"] for row in baseline["packages"] if row["kind"] == "places"}
    actual_places = {row["id"] for row in packages if row["kind"] == "places"}
    if len(baseline_places) < 900 or baseline_places - actual_places:
        raise ValueError(f"Offline search coverage regressed: {sorted(baseline_places - actual_places)}")
    for package in packages:
        expected_id = f"search-10-{package['x']}-{package['y']}-{package['kind']}"
        if (package["zoom"] != 10 or package["id"] != expected_id or
                package["file"] != expected_id + ".sqlite.gz" or package["kind"] not in ("places", "addresses") or
                not (0 <= package["x"] < 1024 and 0 <= package["y"] < 1024) or
                not 1 <= package["bytes"] <= 50_000_000 or not 1 <= package["installed_bytes"] <= 256_000_000 or
                package["entries"] <= 0):
            raise ValueError(f"Invalid search catalog entry: {expected_id}")
        file = output / package["file"]
        if file.stat().st_size != package["bytes"] or digest(file) != package["sha256"]:
            raise ValueError(f"Search package integrity check failed: {file.name}")
    (output / "NOTICE.txt").write_text(
        f"Geofabrik OSM extract · {manifest['date']}\n{OSM_LICENSE}", encoding="utf-8")
    return dict(source=source, date=manifest["date"], packages=len(packages),
                entries=sum(row["entries"] for row in packages),
                search_samples=verify_search_samples(output, work, packages))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", choices=("karte", "routing", "osm", "suche", "postleitzahlen", "laderegister"))
    parser.add_argument("output", type=Path, help="New output directory")
    parser.add_argument("--pmtiles-tool", default="pmtiles")
    parser.add_argument("--map-extract", action="append", type=Path)
    parser.add_argument("--map-dataset")
    parser.add_argument("--common-map", type=Path)
    parser.add_argument("--map-baseline", type=Path)
    parser.add_argument("--geofabrik-index", type=Path)
    parser.add_argument("--osm-input", type=Path)
    parser.add_argument("--routing-dir", type=Path)
    parser.add_argument("--prepared-search", type=Path)
    parser.add_argument("--search-baseline", type=Path)
    parser.add_argument("--search-sources", type=Path, help="Local sources manifest, Germany first")
    parser.add_argument("--postal-input", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(args.output)
    build = dict(karte=map_build, routing=routing_build, osm=osm_build, suche=search_build,
                 postleitzahlen=PROBE["postal_package"], laderegister=register_build)[args.dataset]
    started = monotonic()
    with TemporaryDirectory(prefix=f"{args.dataset}-", dir=args.output.parent) as scratch:
        details = build(args, Path(scratch), args.output)
    files = []
    for file in sorted(args.output.rglob("*")):
        if file.is_file():
            size = file.stat().st_size
            if not 0 < size < PROBE["MAX_ASSET"]:
                raise ValueError(f"Invalid asset size: {file} ({size})")
            files.append(dict(file=file.relative_to(args.output).as_posix(), bytes=size, sha256=digest(file)))
    report = dict(schema=1, dataset=args.dataset, details=details, seconds=round(monotonic() - started, 1),
                  files=files, bytes=sum(file["bytes"] for file in files))
    write_json(args.output / "pruefbericht.json", report)
    print(f"{args.dataset}: {len(files)} verified files, {report['bytes']:,} bytes in {report['seconds']} s", flush=True)


if __name__ == "__main__":
    main()
