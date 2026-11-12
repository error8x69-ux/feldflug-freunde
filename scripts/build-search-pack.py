"""Build an ODbL offline search database from an official OSM PBF extract.

Requires the project's existing pyosmium, no Android dependencies.
"""
import argparse
import gzip
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
import osmium


def normalize(value):
    value = value.lower().replace('ß', 'ss').replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue')
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c))).strip()


class Importer(osmium.SimpleHandler):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.count = 0

    def insert(self, obj, kind, lat, lon):
        tags = dict(obj.tags)
        place = tags.get('place', '')
        street = tags.get('addr:street', '')
        house = tags.get('addr:housenumber', '')
        if street and house:
            name, category = f'{street} {house}', 'house'
        elif place in ('city', 'town', 'village', 'hamlet', 'suburb', 'quarter', 'neighbourhood') and tags.get('name'):
            name, category = tags['name'], 'city'
        elif kind == 'w' and tags.get('highway') and tags.get('name'):
            name, category = tags['name'], 'street'
        else:
            return
        city = tags.get('addr:city', '')
        detail = ', '.join(x for x in (tags.get('addr:postcode', ''), city) if x)
        aliases = ' '.join(tags.get(k, '') for k in ('alt_name', 'official_name', 'short_name', 'name:de'))
        terms = normalize(' '.join((name, detail, aliases)))
        self.db.execute('INSERT INTO places VALUES(?,?,?,?,?,?,?,?)',
                        (f'{kind}{obj.id}', name, detail, category, lat, lon, normalize(name), terms))
        self.count += 1

    def node(self, n):
        if (n.tags.get('place') or n.tags.get('addr:housenumber')) and n.location.valid():
            self.insert(n, 'n', n.location.lat, n.location.lon)

    def way(self, w):
        if not ((w.tags.get('highway') and w.tags.get('name')) or w.tags.get('addr:housenumber')):
            return
        nodes = [n for n in w.nodes if n.location.valid()]
        if nodes:
            # A real geometry point, not a centroid that might lie off the street.
            p = nodes[len(nodes) // 2].location
            self.insert(w, 'w', p.lat, p.lon)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--region', required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--source', required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit('Refusing to overwrite an existing package')
    db = sqlite3.connect(args.output)
    db.execute('CREATE TABLE places(id TEXT PRIMARY KEY,name TEXT,detail TEXT,type TEXT,lat REAL,lon REAL,normalized TEXT,terms TEXT)')
    db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
    for k, v in {'schema': '1', 'region': args.region, 'date': args.date, 'source': args.source,
                 'license': 'ODbL-1.0', 'attribution': '© OpenStreetMap contributors',
                 'license_url': 'https://opendatacommons.org/licenses/odbl/1-0/'}.items():
        db.execute('INSERT INTO metadata VALUES(?,?)', (k, v))
    importer = Importer(db)
    importer.apply_file(str(args.input), locations=True, idx='flex_mem')
    db.execute('CREATE INDEX place_name ON places(normalized)')
    db.execute('CREATE VIRTUAL TABLE search USING fts4(terms)')
    db.execute('INSERT INTO search(docid,terms) SELECT rowid,terms FROM places')
    db.execute('PRAGMA user_version=1')
    db.commit()
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    db.close()
    compressed = args.output.with_suffix('.sqlite.gz')
    with args.output.open('rb') as source, compressed.open('wb') as destination:
        with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as zipped:
            import shutil
            shutil.copyfileobj(source, zipped)
    result = {'schema': 1, 'region': args.region, 'date': args.date, 'file': args.output.name,
              'bytes': args.output.stat().st_size, 'sha256': hashlib.file_digest(args.output.open('rb'), 'sha256').hexdigest(),
              'entries': importer.count, 'source': args.source, 'license': 'ODbL-1.0',
              'attribution': '© OpenStreetMap contributors'}
    result.update(download_file=compressed.name, download_bytes=compressed.stat().st_size,
                  download_sha256=hashlib.file_digest(compressed.open('rb'), 'sha256').hexdigest())
    args.output.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
