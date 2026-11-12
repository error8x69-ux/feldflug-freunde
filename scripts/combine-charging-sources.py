"""Package independent OSM and Bundesnetzagentur charging datasets for Feldflug."""
import argparse
import csv
import gzip
import json
import re
from pathlib import Path


DAYS = dict(zip(("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
                ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")))


def opening_hours(entry):
    if entry["Öffnungszeiten"] == "247":
        return "24/7"
    days = entry["Öffnungszeiten: Wochentage"].split(";")
    times = entry["Öffnungszeiten: Tageszeiten"].split(";")
    if len(days) != len(times):
        return ""
    periods = []
    for day, time in zip(days, times):
        code = DAYS.get(day.strip())
        hours = time.strip()
        if not code or not re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", hours):
            return ""
        # The register uses 23:59 as the inclusive last minute of the day.
        if hours.endswith("-23:59"):
            hours = hours[:-5] + "24:00"
        periods.append(f"{code} {hours}")
    return "; ".join(periods)


def register(source):
    rows = []
    with source.open(encoding="utf-8-sig", newline="") as stream:
        for line in stream:
            if line.startswith("Ladeeinrichtungs-ID;"):
                entries = csv.DictReader(stream, fieldnames=line.rstrip().split(";"), delimiter=";")
                break
        else:
            raise ValueError("Ladesäulenregister header missing")
        for entry in entries:
            if entry["Status"] != "In Betrieb":
                continue
            connectors = {
                label.strip() for key, cell in entry.items()
                if key and key.startswith("Steckertypen") and cell
                for label in cell.split(";")
            }
            schuko = "AC Schuko" in connectors
            type2 = "AC Typ 2 Steckdose" in connectors
            if not schuko and not type2:
                continue
            try:
                lat, lon = (float(entry[key].replace(",", ".")) for key in ("Breitengrad", "Längengrad"))
                station_id = -int(entry["Ladeeinrichtungs-ID"])
            except (ValueError, AttributeError):
                continue
            if not (46 < lat < 56 and 5 < lon < 16):
                continue
            rows.append(dict(id=station_id, lat=round(lat, 6), lon=round(lon, 6),
                             schuko=schuko, type2=type2, type2Cable="AC Typ 2 Fahrzeugkupplung" in connectors,
                             name=(entry["Anzeigename (Karte)"] or entry["Betreiber"] or "")[:160],
                             hours=opening_hours(entry), fee="",
                             address=", ".join(filter(None, (
                                 " ".join(filter(None, (entry["Straße"], entry["Hausnummer"]))),
                                 " ".join(filter(None, (entry["Postleitzahl"], entry["Ort"]))),
                             )))[:200]))
    if not rows or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Empty register or duplicate IDs")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("osm", type=Path)
    parser.add_argument("register", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--register-date", required=True)
    args = parser.parse_args()
    with gzip.open(args.osm, "rt", encoding="utf-8") as stream:
        osm = json.load(stream)
    notes = Path(__file__).with_name("data") / "charging-site-notes.json"
    osm["stations"].extend(json.loads(notes.read_text(encoding="utf-8")))
    stations = register(args.register)
    payload = dict(schema=2, osm=osm, register=dict(
        date=args.register_date, license="CC-BY-4.0", attribution="Bundesnetzagentur.de",
        source="https://www.bundesnetzagentur.de/ladesaeulenkarte",
        licenseUrl="https://creativecommons.org/licenses/by/4.0/", stations=stations))
    args.output.write_bytes(gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(osm['stations'])} OSM, {len(stations)} BNetzA, {args.output.stat().st_size} bytes")


if __name__ == "__main__":
    main()
