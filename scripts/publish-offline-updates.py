"""Stage immutable data assets and advance the app catalog only after every job succeeds."""
import argparse
import base64
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import sleep
from urllib.error import HTTPError
from urllib.request import urlopen

DATASETS = ("karte", "routing", "osm", "suche", "postleitzahlen")
ANCHOR = date(2026, 11, 12)
CATALOG = "offline-catalog.json"
SHARD_SIZE = 128 * 1024**2


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True, encoding="utf-8")


def remote_catalog(repo):
    try:
        with urlopen(f"https://raw.githubusercontent.com/{repo}/main/{CATALOG}", timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code != 404:
            raise
        return None


def update_slot(day):
    return (day - ANCHOR).days // 28


def gate(repo):
    previous = remote_catalog(repo)
    slot = update_slot(datetime.now(timezone.utc).date())
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"slot={slot}\ndue={str(slot >= 0 and (previous is None or previous['slot'] < slot)).lower()}\n"
                     f"first={str(previous is None).lower()}\n")


def sources(dataset, root):
    report = read(root / "pruefbericht.json")
    if report["dataset"] != dataset:
        raise ValueError("Wrong data report")
    files = {row["file"]: row for row in report["files"]}
    if dataset == "karte":
        for row in read(root / "shared-map.json"):
            yield row["id"], row["file"], row["installed_bytes"], files[row["file"]]
        for row in read(root / "detail/manifest.json")["packages"]:
            name = "detail/" + row["file"]
            yield row["id"], name, row["installed_bytes"], files[name]
    elif dataset == "suche":
        for row in read(root / "manifest.json")["packages"]:
            yield row["id"], row["file"], row["installed_bytes"], files[row["file"]]
    else:
        names = {
            "osm": {"railway": "bahnhoefe/offline-stations.tsv", "cycle": "radrouten/radrouten-de.geojson.gz"},
            "postleitzahlen": {"postal": "de-postal-places.tsv"},
            "routing": {name[:-4]: name for name in files if re.fullmatch(r"[EW]\d{1,3}_[NS]\d{1,2}\.rd5", name)},
        }[dataset]
        if dataset == "routing" and len(names) != 7:
            raise ValueError("Incomplete routing data")
        for identifier, name in names.items():
            yield identifier, name, files[name]["bytes"], files[name]


def stage(repo, dataset, root, output, slot):
    tag = f"offline-data-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}-{dataset}"
    output.mkdir()
    with TemporaryDirectory(prefix="shards-", dir=output) as temporary:
        directory = Path(temporary)
        shards, entries, offset = [], [], 0
        for identifier, name, installed, expected in sources(dataset, root):
            source = root / name
            size = source.stat().st_size
            if size != expected["bytes"] or sha256(source) != expected["sha256"] or not 0 < size < 2**31:
                raise ValueError(f"Data changed after verification: {name}")
            if not shards or (offset and offset + size > SHARD_SIZE):
                shards.append(directory / f"data-{len(shards)+1:02d}.bin")
                offset = 0
            with source.open("rb") as original, shards[-1].open("ab") as shard:
                shutil.copyfileobj(original, shard, 1024**2)
            entries.append(dict(id=identifier, shard=shards[-1].name, offset=offset,
                                bytes=size, installed=installed, sha256=expected["sha256"]))
            offset += size
        if not entries:
            raise ValueError("No update data")
        notices = directory / "NOTICE.txt"
        notices.write_text(
            "Feldflug offline data. Sources and attribution in data-index.json.\n"
            "© OpenStreetMap contributors · ODbL 1.0 · https://www.openstreetmap.org/copyright\n"
            "Maps: OpenFreeMap / OpenMapTiles. Routing: BRouter. Postal data: GeoNames, CC BY 4.0.\n",
            encoding="utf-8")
        index = directory / "data-index.json"
        payload = dict(dataset=dataset, tag=tag, slot=slot, entries=entries,
                       source=read(root / "pruefbericht.json")["details"])
        write(index, payload)
        assets = [*shards, notices, index]
        expected = {asset.name: "sha256:" + sha256(asset) for asset in assets}
        gh("release", "create", tag, "--repo", repo, "--draft", "--prerelease", "--latest=false",
           "--title", f"Feldflug Offlinedaten · {dataset}", "--notes",
           f"Verified offline data from {os.environ['GITHUB_SHA']}.", *(str(asset) for asset in assets))
        for _ in range(8):
            release = json.loads(gh("release", "view", tag, "--repo", repo, "--json", "isDraft,assets"))
            actual = {a["name"]: a.get("digest") for a in release["assets"] if a["state"] == "uploaded"}
            if release["isDraft"] and expected == actual:
                break
            sleep(5)
        else:
            raise ValueError(f"GitHub asset verification failed for {tag}")
        write(output / f"{dataset}.json", payload)


def activate(repo, directory, slot):
    groups = {dataset: read(directory / f"{dataset}.json") for dataset in DATASETS}
    revision = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    if any(group["slot"] != slot or group["dataset"] != dataset or
           group["tag"] != f"offline-data-{revision}-{dataset}" for dataset, group in groups.items()):
        raise ValueError("Data groups belong to different runs")
    endpoint = f"repos/{repo}/contents/{CATALOG}"
    response = subprocess.run(["gh", "api", endpoint], text=True, encoding="utf-8", capture_output=True)
    sha = None
    if response.returncode == 0:
        previous = json.loads(response.stdout)
        old_slot = json.loads(base64.b64decode(previous["content"]))["slot"]
        if old_slot >= slot:
            return
        sha = previous["sha"]
    elif "HTTP 404" not in response.stderr:
        raise RuntimeError(response.stderr.strip())
    body = dict(schema=1, slot=slot, updated=datetime.now(timezone.utc).isoformat(), revision=revision, groups=groups)
    path = directory / CATALOG
    write(path, body)
    if path.stat().st_size > 2_000_000:
        raise ValueError("Catalog exceeds the app's download limit")
    for group in groups.values():
        gh("release", "edit", group["tag"], "--repo", repo, "--draft=false", "--prerelease", "--latest=false")
    message = dict(message=f"Activate Feldflug offline data slot {slot}", branch="main",
                   content=base64.b64encode(path.read_bytes()).decode("ascii"))
    if sha:
        message["sha"] = sha
    request = directory / "activate.json"
    write(request, message)
    gh("api", "--method", "PUT", endpoint, "--input", str(request), "--jq", ".content.sha")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("gate", "stage", "activate"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--slot", type=int)
    parser.add_argument("--dataset", choices=DATASETS)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "gate":
        gate(args.repo)
    elif args.action == "stage":
        stage(args.repo, args.dataset, args.data, args.output, args.slot)
    else:
        activate(args.repo, args.data, args.slot)


if __name__ == "__main__":
    main()
