"""Create bounded GitHub draft assets from a verified national dataset."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile

PART_LIMIT = 1536 * 1024**2


def digest(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report_path = args.dataset / "pruefbericht.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=False)
    parts, part, size = [], [], 0
    for entry in report["files"]:
        path = args.dataset / entry["file"]
        if not path.resolve().is_relative_to(args.dataset.resolve()):
            raise ValueError(f"Unexpected asset path: {path}")
        with path.open("rb") as source:
            if path.stat().st_size != entry["bytes"] or digest(source) != entry["sha256"]:
                raise ValueError(f"Dataset changed after verification: {path}")
        if part and size + entry["bytes"] > PART_LIMIT:
            parts.append(part)
            part, size = [], 0
        part.append(entry)
        size += entry["bytes"]
    if part:
        parts.append(part)
    assets = []
    for index, group in enumerate(parts, 1):
        path = args.output / f"part-{index:02d}.tar"
        with tarfile.open(path, "x") as archive:
            for entry in group:
                archive.add(args.dataset / entry["file"], arcname=entry["file"], recursive=False)
        if path.stat().st_size >= 2 * 1024**3:
            raise ValueError(f"Archive exceeds GitHub's limit: {path}")
        with tarfile.open(path) as archive:
            for entry in group:
                with archive.extractfile(entry["file"]) as source:
                    if digest(source) != entry["sha256"]:
                        raise ValueError(f"Archive verification failed: {entry['file']}")
        with path.open("rb") as source:
            assets.append(dict(file=path.name, bytes=path.stat().st_size, sha256=digest(source)))
    (args.output / "pruefbericht.json").write_bytes(report_path.read_bytes())
    with report_path.open("rb") as source:
        assets.append(dict(file=report_path.name, bytes=report_path.stat().st_size, sha256=digest(source)))
    (args.output / "SHA256SUMS").write_text(
        "".join(f"{asset['sha256']}  {asset['file']}\n" for asset in assets), encoding="ascii")
    print(f"{report['dataset']}: {len(parts)} verified archives")


if __name__ == "__main__":
    main()
