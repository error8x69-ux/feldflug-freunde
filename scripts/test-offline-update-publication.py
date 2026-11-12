"""Tests for the 28-day schedule, ranged assets and atomic app catalog switch."""
import base64
from datetime import date
import importlib.util
import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("publication", Path(__file__).with_name("publish-offline-updates.py"))
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)
ENV = dict(GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1", GITHUB_SHA="test")


class PublicationTest(unittest.TestCase):
    def test_slots_stay_28_days_apart(self):
        self.assertEqual(-1, publication.update_slot(date(2026, 11, 11)))
        self.assertEqual(0, publication.update_slot(date(2026, 11, 12)))
        self.assertEqual(0, publication.update_slot(date(2026, 12, 9)))
        self.assertEqual(1, publication.update_slot(date(2026, 12, 10)))
        self.assertEqual(2, publication.update_slot(date(2027, 1, 7)))

    def test_ranges_recover_verified_payloads_and_exclude_charging(self):
        with TemporaryDirectory() as tmp, patch.dict("os.environ", ENV):
            root = Path(tmp)
            source = root / "source"
            expected = {
                "bahnhoefe/offline-stations.tsv": b"station data",
                "radrouten/radrouten-de.geojson.gz": b"cycle data",
                "erweiterungen/ladestationen/charging-osm-de.json.gz": b"extension data",
            }
            records = []
            for name, data in expected.items():
                file = source / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)
                records.append(dict(file=name, bytes=len(data), sha256=publication.sha256(file)))
            publication.write(source / "pruefbericht.json", dict(dataset="osm", details={}, files=records))
            uploaded = {}

            def github(*args):
                if args[:2] == ("release", "create"):
                    for arg in args:
                        path = Path(arg)
                        if path.is_file():
                            uploaded[path.name] = path.read_bytes()
                    return ""
                self.assertEqual(("release", "view"), args[:2])
                import hashlib
                return json.dumps(dict(isDraft=True, assets=[
                    dict(name=name, digest="sha256:" + hashlib.sha256(data).hexdigest(), state="uploaded")
                    for name, data in uploaded.items()
                ]))

            with patch.object(publication, "gh", github):
                publication.stage("example/test", "osm", source, root / "result", 0)
            index = publication.read(root / "result/osm.json")
            self.assertEqual(["railway", "cycle"], [row["id"] for row in index["entries"]])
            for record, name in zip(index["entries"], list(expected)[:2]):
                data = uploaded[record["shard"]]
                self.assertEqual(expected[name], data[record["offset"]:record["offset"] + record["bytes"]])

    def test_activation_needs_a_complete_single_run_and_writes_pointer_last(self):
        with TemporaryDirectory() as tmp, patch.dict("os.environ", ENV):
            directory = Path(tmp)
            for name in publication.DATASETS:
                publication.write(directory / f"{name}.json",
                                  dict(dataset=name, slot=1, tag=f"offline-data-42-1-{name}", entries=[]))
            calls = []

            def github(*args):
                calls.append(args)
                if args[0] == "api":
                    body = publication.read(Path(args[args.index("--input") + 1]))
                    catalog = json.loads(base64.b64decode(body["content"]))
                    self.assertEqual(set(publication.DATASETS), set(catalog["groups"]))
                    self.assertEqual(1, catalog["slot"])
                return ""

            result = CompletedProcess([], 1, "", "HTTP 404")
            with patch.object(publication, "gh", github), patch.object(publication.subprocess, "run", return_value=result):
                publication.activate("example/test", directory, 1)
                self.assertEqual(["release"] * 5 + ["api"], [call[0] for call in calls])
                publication.write(directory / "osm.json", dict(dataset="osm", slot=1, tag="offline-data-41-1-osm"))
                with self.assertRaises(ValueError):
                    publication.activate("example/test", directory, 1)
                self.assertEqual(6, len(calls))


if __name__ == "__main__":
    unittest.main()
