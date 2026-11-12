"""Split a finished search database into downloadable geographic tiles (zoom 10).
The manifest records download AND installed sizes; no arbitrary byte-sized chunks.
"""
import argparse
import gzip
import hashlib
import json
import math
import shutil
import sqlite3
from itertools import groupby
from pathlib import Path


def tile(lat, lon):
    lat = max(-85.05112878, min(85.05112878, lat))
    return (min(1023, max(0, math.floor((lon + 180) / 360 * 1024))),
            min(1023, max(0, math.floor((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 1024))))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    p.add_argument('output', type=Path)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f'file:{args.input.resolve().as_posix()}?mode=ro', uri=True)
    assert src.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    metadata = dict(src.execute('SELECT key,value FROM metadata'))
    # The nationwide import must not keep the whole database in Python memory.
    src.execute('PRAGMA temp_store=FILE')
    src.create_function('tile_id', 2, lambda lat, lon: tile(lat, lon)[0] * 1024 + tile(lat, lon)[1])
    ordered = src.execute('SELECT *,tile_id(lat,lon) AS tile_key FROM places ORDER BY tile_key,id')
    write_tiles(ordered, metadata, args.output)


def write_tiles(ordered, metadata, output, release_prefix=None, compact=None):
    packages = []
    for key, group in groupby(ordered, key=lambda row: row[-1]):
        x, y = divmod(key, 1024)
        rows = [row[:-1] for row in group]
        name = f'search-10-{x}-{y}'
        dest = output / f'{name}.sqlite'
        if dest.exists():
            raise RuntimeError(f'Refusing overwrite: {dest}')
        db = sqlite3.connect(dest)
        db.execute('CREATE TABLE places(id TEXT PRIMARY KEY,name TEXT,detail TEXT,type TEXT,lat REAL,lon REAL,normalized TEXT,terms TEXT)')
        db.executemany('INSERT INTO places VALUES(?,?,?,?,?,?,?,?)', rows)
        db.execute('CREATE INDEX place_name ON places(normalized)')
        db.execute('CREATE VIRTUAL TABLE search USING fts4(terms)')
        db.execute('INSERT INTO search(docid,terms) SELECT rowid,terms FROM places')
        db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
        db.executemany('INSERT INTO metadata VALUES(?,?)', metadata.items())
        db.execute('PRAGMA user_version=1')
        db.commit()
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        db.close()
        if compact:
            packages.extend(compact(output, output, dict(id=name, zoom=10, x=x, y=y, entries=len(rows))))
            dest.unlink()
            continue
        compressed = dest.with_suffix('.sqlite.gz')
        with dest.open('rb') as source, compressed.open('wb') as target:
            with gzip.GzipFile(filename='', fileobj=target, mode='wb', mtime=0) as archive:
                shutil.copyfileobj(source, archive)
        with compressed.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        packages.append(dict(id=name, x=x, y=y, zoom=10, file=compressed.name,
                             bytes=compressed.stat().st_size, installed_bytes=dest.stat().st_size,
                             sha256=digest, entries=len(rows)))
        if release_prefix:
            packages[-1]['release'] = f'{release_prefix}-{(len(packages)-1)//900+1}'
    if compact and release_prefix:
        for index, package in enumerate(packages):
            package['release'] = f'{release_prefix}-{index // 900 + 1}'
    manifest = dict(schema=2 if compact else 1, region=metadata['region'], date=metadata['date'],
                    source=metadata['source'], license='ODbL-1.0',
                    attribution='© OpenStreetMap contributors',
                    coverage='Only features present in the source extract; edge tiles may be incomplete.',
                    packages=packages)
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(packages=len(packages), entries=sum(p['entries'] for p in packages),
                         download_bytes=sum(p['bytes'] for p in packages),
                         largest_download=max((p['bytes'] for p in packages), default=0))))


if __name__ == '__main__':
    main()
