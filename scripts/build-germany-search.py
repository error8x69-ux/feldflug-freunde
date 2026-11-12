"""Build Germany and neighbouring border tiles from Geofabrik extracts.

Run with the existing pyosmium Python. Output is staged locally for validation
before publishing. No source extracts are distributed with the search tiles.
"""
import argparse
from contextlib import nullcontext
import json
import math
import runpy
import sqlite3
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
Importer = runpy.run_path(str(SCRIPTS / 'build-search-pack.py'))['Importer']
splitter = runpy.run_path(str(SCRIPTS / 'split-search-pack.py'))
tile = splitter['tile']


def border_tiles(poly):
    result = set()
    previous = None
    for line in poly.read_text(encoding='utf-8').splitlines():
        if not line.startswith((' ', '\t')):
            previous = None
            continue
        lon, lat = map(float, line.split())
        if previous is not None:
            plon, plat = previous
            steps = max(1, math.ceil(max(abs(lon - plon), abs(lat - plat)) / .03))
            for step in range(steps + 1):
                x, y = tile(plat + (lat - plat) * step / steps, plon + (lon - plon) * step / steps)
                result.update((x + dx, y + dy) for dx in range(-2, 3) for dy in range(-2, 3))
        previous = lon, lat
    if not result:
        raise ValueError('Geofabrik border polygon is empty')
    return result


class TileImporter(Importer):
    def __init__(self, db, allowed):
        super().__init__(db)
        self.allowed = allowed
        self.xy = None

    def insert(self, obj, kind, lat, lon):
        self.xy = tile(lat, lon)
        if self.allowed is None or self.xy in self.allowed:
            super().insert(obj, kind, lat, lon)


def import_search_objects(importer, path, index):
    import osmium

    # The location handler still indexes every node before the native tag
    # filter. Only objects that can contribute a search row reach Python.
    objects = (osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY)
               .with_locations(index)
               .with_filter(osmium.filter.KeyFilter('place', 'addr:housenumber', 'highway')))
    for obj in objects:
        if obj.is_node():
            importer.node(obj)
        else:
            importer.way(obj)


def build(sources, allowed, output, date, *, compact=False, source_path=None, index_kind=None,
          date_from_path=None):
    if not sources:
        raise ValueError('No source extracts')
    if source_path is None:
        source_path = lambda source: nullcontext(Path(source['path']))
    output.mkdir(parents=True, exist_ok=False)
    staging = output / 'staging.sqlite'
    db = sqlite3.connect(staging)
    db.execute('PRAGMA journal_mode=OFF')
    db.execute('CREATE TABLE staging(x INTEGER,y INTEGER,id TEXT,name TEXT,detail TEXT,type TEXT,lat REAL,lon REAL,normalized TEXT,terms TEXT,PRIMARY KEY(x,y,id)) WITHOUT ROWID')
    for index, source in enumerate(sources):
        class Sink:
            def execute(self, _sql, values):
                db.execute('INSERT OR IGNORE INTO staging VALUES(?,?,?,?,?,?,?,?,?,?)', importer.xy + values)
        importer = TileImporter(Sink(), None if index == 0 else allowed)
        location_index = output / f'nodes-{index}.index'
        with source_path(source) as path:
            if index == 0 and date_from_path:
                date = date_from_path(path)
            index_type = index_kind or f'sparse_file_array,{location_index.resolve().as_posix()}'
            if compact:
                import_search_objects(importer, path, index_type)
            else:
                importer.apply_file(str(path), locations=True, idx=index_type)
        del importer
        if compact and index_kind is None:
            location_index.unlink()
        db.commit()
        print(f'Imported {source["url"]}', flush=True)
    metadata = dict(region='Deutschland und Grenzgebiete', date=date,
                    source='\n'.join(s['url'] for s in sources), schema='1', license='ODbL-1.0',
                    attribution='© OpenStreetMap contributors',
                    license_url='https://opendatacommons.org/licenses/odbl/1-0/')
    rows = db.execute('SELECT id,name,detail,type,lat,lon,normalized,terms,x*1024+y FROM staging ORDER BY x,y,id')
    split = runpy.run_path(str(SCRIPTS / 'split-search-addresses.py'))['split'] if compact else None
    prefix = 'suchdaten-de-kompakt' if compact else 'suchdaten-de'
    splitter['write_tiles'](rows, metadata, output, release_prefix=f'{prefix}-{date}', compact=split)
    db.close()
    if compact:
        staging.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', type=Path, help='JSON array of {path, url}; Germany first')
    parser.add_argument('polygon', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--date', required=True)
    args = parser.parse_args()
    sources = json.loads(args.sources.read_text(encoding='utf-8-sig'))
    build(sources, border_tiles(args.polygon), args.output, args.date)


if __name__ == '__main__':
    main()
