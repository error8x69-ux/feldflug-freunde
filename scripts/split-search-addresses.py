"""Split published SQLite search tiles into places/streets and optional addresses."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def split(source, output, package):
    original = (source / (package['id'] + '.sqlite')).resolve()
    results = []
    for kind, condition in [('places', "type <> 'house'"), ('addresses', "type = 'house'")]:
        with closing(sqlite3.connect(original.as_uri() + '?mode=ro', uri=True)) as db:
            count = db.execute(f'SELECT count(*) FROM places WHERE {condition}').fetchone()[0]
        if not count:
            continue
        identifier = f"{package['id']}-{kind}"
        database = output / f'{identifier}.sqlite'
        if database.exists():
            raise FileExistsError(database)
        with closing(sqlite3.connect(database, uri=True)) as db:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('ATTACH DATABASE ? AS original', (original.as_uri() + '?mode=ro',))
            db.execute('CREATE TABLE places(id TEXT,name TEXT,detail TEXT,type TEXT,lat REAL,lon REAL,normalized TEXT,terms TEXT)')
            db.execute(f'INSERT INTO places(rowid,id,name,detail,type,lat,lon,normalized,terms) SELECT rowid,* FROM original.places WHERE {condition}')
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('INSERT INTO metadata SELECT * FROM original.metadata')
            db.execute("CREATE VIRTUAL TABLE search USING fts4(terms, content='places', matchinfo=fts3)")
            db.execute('INSERT INTO search(docid,terms) SELECT rowid,terms FROM places')
            db.execute("INSERT INTO search(search) VALUES('optimize')")
            db.execute('PRAGMA user_version=1')
            db.commit()
            assert db.execute('PRAGMA main.integrity_check').fetchone()[0] == 'ok'
            assert db.execute('SELECT count(*) FROM places').fetchone()[0] == count
            assert db.execute(f'SELECT rowid,* FROM original.places WHERE {condition} EXCEPT SELECT rowid,* FROM places').fetchone() is None
        archive = output / f'{identifier}.sqlite.gz'
        with database.open('rb') as raw, archive.open('xb') as compressed:
            with gzip.GzipFile(filename='', mode='wb', fileobj=compressed, compresslevel=6, mtime=0) as zipped:
                shutil.copyfileobj(raw, zipped)
        with archive.open('rb') as file:
            digest = hashlib.file_digest(file, 'sha256').hexdigest()
        results.append(dict(id=identifier, kind=kind, zoom=package['zoom'], x=package['x'], y=package['y'],
                            file=archive.name, bytes=archive.stat().st_size, installed_bytes=database.stat().st_size,
                            sha256=digest, entries=count))
        database.unlink()
    assert sum(item['entries'] for item in results) == package['entries']
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    catalog = json.loads((args.source / 'manifest.json').read_text(encoding='utf-8'))
    args.output.mkdir(parents=True, exist_ok=False)
    packages = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for index, parts in enumerate(pool.map(lambda package: split(args.source, args.output, package), catalog['packages']), 1):
            packages.extend(parts)
            if index % 100 == 0:
                print(f'{index}/{len(catalog["packages"])} tiles verified', flush=True)
    for index, package in enumerate(packages):
        package['release'] = f"suchdaten-de-kompakt-{catalog['date']}-{index // 900 + 1}"
    catalog.update(schema=2, packages=packages)
    (args.output / 'manifest.json').write_text(json.dumps(catalog, ensure_ascii=False), encoding='utf-8')
    for kind in ('places', 'addresses'):
        selected = [p for p in packages if p['kind'] == kind]
        print(kind, len(selected), sum(p['entries'] for p in selected),
              sum(p['bytes'] for p in selected), sum(p['installed_bytes'] for p in selected), flush=True)


if __name__ == '__main__':
    main()
