"""Recompress a local PMTiles v3 MVT extract and verify identical decoded tiles."""
import gzip
import hashlib
import json
import mmap
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path


def directory(data):
    def integers():
        value = shift = 0
        for byte in gzip.decompress(data):
            value |= (byte & 127) << shift
            if byte < 128:
                yield value
                value = shift = 0
            else:
                shift += 7
    values = integers()
    count = next(values)
    ids = []
    tile = 0
    for _ in range(count):
        tile += next(values)
        ids.append(tile)
    runs = [next(values) for _ in ids]
    sizes = [next(values) for _ in ids]
    end = 0
    for tile, run, size in zip(ids, runs, sizes):
        encoded = next(values)
        offset = encoded - 1 if encoded else end
        end = offset + size
        yield tile, run, size, offset


def archive(path):
    with path.open('rb') as handle:
        raw = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
    assert raw[:8] == b'PMTiles\x03' and raw[97:100] == bytes((2, 2, 1))
    root, root_size, meta, meta_size, leaf, _, data = struct.unpack_from('<7Q', raw, 8)

    def tiles(offset, length):
        for first, run, size, at in directory(raw[offset:offset + length]):
            if not run:
                yield from tiles(leaf + at, size)
            else:
                blob = raw[data + at:data + at + size]
                for tile_id in range(first, first + run):
                    yield tile_id, blob

    bounds = [struct.unpack_from('<i', raw, offset)[0] / 10_000_000 for offset in (102, 106, 110, 114)]
    def read_tiles():
        try:
            yield from tiles(root, root_size)
        finally:
            raw.close()

    return json.loads(gzip.decompress(raw[meta:meta + meta_size])), read_tiles(), raw[100:102], bounds


def zxy(tile_id):
    zoom = 0
    while tile_id >= 1 << (2 * zoom):
        tile_id -= 1 << (2 * zoom)
        zoom += 1
    x = y = 0
    for bit in range(zoom):
        side = 1 << bit
        rx, ry = (tile_id >> 1) & 1, (tile_id ^ (tile_id >> 1)) & 1
        if not ry:
            if rx:
                x, y = side - 1 - x, side - 1 - y
            x, y = y, x
        x += side * rx
        y += side * ry
        tile_id >>= 2
    return zoom, x, y


def main():
    tool, source, target = map(Path, sys.argv[1:4])
    resume = len(sys.argv) == 5
    intermediate = Path(sys.argv[4]) if resume else target.with_suffix('.mbtiles')
    if target.exists() or intermediate.exists() != resume:
        raise FileExistsError(target)
    metadata, original, zooms, bounds = archive(source)
    metadata.update(format='pbf', minzoom=str(zooms[0]), maxzoom=str(zooms[1]), bounds=','.join(map(str, bounds)))
    db = sqlite3.connect(intermediate)
    if not resume:
        db.execute('CREATE TABLE metadata(name TEXT PRIMARY KEY,value TEXT)')
        db.executemany('INSERT INTO metadata VALUES(?,?)',
                       ((key, value if isinstance(value, str) else json.dumps(value))
                        for key, value in metadata.items()))
        db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',
                   ('json', json.dumps({'vector_layers': metadata['vector_layers']})))
        db.execute('CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB)')
    checksums = {}
    for tile_id, blob in original:
        decoded = gzip.decompress(blob)
        checksums[tile_id] = hashlib.sha256(decoded).digest()
        if not resume:
            compressed = gzip.compress(decoded, compresslevel=9, mtime=0)
            z, x, y = zxy(tile_id)
            db.execute('INSERT INTO tiles VALUES(?,?,?,?)', (z, x, (1 << z) - 1 - y, min((blob, compressed), key=len)))
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS tiles_coordinate ON tiles(zoom_level,tile_column,tile_row)')
    db.commit()
    db.close()
    subprocess.run([str(tool), 'convert', str(intermediate), str(target)], check=True)
    _, repacked, output_zooms, output_bounds = archive(target)
    assert output_zooms == zooms and output_bounds == bounds
    assert checksums == {tile: hashlib.sha256(gzip.decompress(blob)).digest() for tile, blob in repacked}
    print(f'{len(checksums)} tiles verified byte-identical after decoding: {source.stat().st_size} -> {target.stat().st_size} bytes')


if __name__ == '__main__':
    main()
