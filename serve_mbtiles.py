#!/usr/bin/env python3
"""
Simple HTTP tile server for .mbtiles files with feature editing support.

Usage:
    python3 serve_mbtiles.py <file1.mbtiles> [file2.mbtiles ...] [--port PORT] [--geojson PATH]

Serves multiple tilesets, each under a name derived from the filename:

    http://localhost:8081/                       - index listing all tilesets
    http://localhost:8081/{name}/metadata.json   - metadata for a tileset
    http://localhost:8081/{name}/{z}/{x}/{y}.pbf - tiles for a tileset
    POST http://localhost:8081/{name}/edit       - edit a feature's properties

Example:
    python3 serve_mbtiles.py west.mbtiles --geojson west.geojson --port 8081

    Tile URLs:
        http://localhost:8081/west/{z}/{x}/{y}.pbf

    Edit endpoint:
        POST http://localhost:8081/west/edit
        Body: {"_merge_id": "abc123", "properties": {"county": "New Value"}, "tile_hint": {"z": 14, "x": 4567, "y": 8901}}
"""

import gzip
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import mapbox_vector_tile


# { name: { path, connection, metadata, geojson } }
TILESETS = {}


def get_connection(name):
    ts = TILESETS.get(name)
    if not ts:
        return None
    if ts.get('connection') is None:
        ts['connection'] = sqlite3.connect(ts['path'], check_same_thread=False)
    return ts['connection']


def tile_at_zoom(z_click, x_click, y_click, target_z):
    """Convert a tile coordinate at one zoom to the equivalent at another zoom."""
    dz = target_z - z_click
    if dz == 0:
        return (target_z, x_click, y_click)
    elif dz > 0:
        # Zooming in: multiply by 2^dz (pick the top-left child)
        factor = 1 << dz
        return (target_z, x_click * factor, y_click * factor)
    else:
        # Zooming out: divide by 2^|dz|
        factor = 1 << (-dz)
        return (target_z, x_click // factor, y_click // factor)


def candidate_tiles(z_click, x_click, y_click, min_zoom, max_zoom):
    """Get candidate tiles to check across all zoom levels.

    At each zoom level, calculate the equivalent tile + its 8 neighbors.
    Returns list of (z, x, y) tuples in XYZ (not TMS) coordinates.
    """
    candidates = []
    for z in range(min_zoom, max_zoom + 1):
        _, cx, cy = tile_at_zoom(z_click, x_click, y_click, z)
        max_tile = (1 << z) - 1
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nx, ny = cx + dx, cy + dy
                if 0 <= nx <= max_tile and 0 <= ny <= max_tile:
                    candidates.append((z, nx, ny))
    return candidates


def update_tiles(conn, merge_id, new_properties, z_click, x_click, y_click, min_zoom, max_zoom):
    """Update all tiles containing a feature with the given _merge_id.

    Handles deduped mbtiles schema where `tiles` is a VIEW over `map` + `images`.
    Returns (tiles_updated, tiles_checked).
    """
    tiles = candidate_tiles(z_click, x_click, y_click, min_zoom, max_zoom)
    tiles_updated = 0
    tiles_checked = 0

    cursor = conn.cursor()

    # Check if this is a deduped schema (tiles is a view, data is in map+images)
    cursor.execute("SELECT type FROM sqlite_master WHERE name='tiles'")
    tiles_type_row = cursor.fetchone()
    is_deduped = tiles_type_row and tiles_type_row[0] == 'view'

    for (z, x, y) in tiles:
        tms_y = (1 << z) - 1 - y

        if is_deduped:
            cursor.execute(
                'SELECT m.tile_id, i.tile_data FROM map m JOIN images i ON m.tile_id = i.tile_id '
                'WHERE m.zoom_level=? AND m.tile_column=? AND m.tile_row=?',
                (z, x, tms_y)
            )
        else:
            cursor.execute(
                'SELECT rowid, tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?',
                (z, x, tms_y)
            )

        row = cursor.fetchone()
        if not row:
            continue

        tile_id_or_rowid, tile_data = row
        tiles_checked += 1

        # Decompress
        is_gzipped = tile_data[:2] == b'\x1f\x8b'
        if is_gzipped:
            raw_data = gzip.decompress(tile_data)
        else:
            raw_data = tile_data

        # Decode
        decoded = mapbox_vector_tile.decode(raw_data)

        # Search all layers for the feature
        found = False
        for layer_name, layer in decoded.items():
            for feature in layer.get('features', []):
                props = feature.get('properties', {})
                if str(props.get('_merge_id', '')) == str(merge_id):
                    # Update properties: keep geometry-related props, replace the rest
                    for k, v in new_properties.items():
                        props[k] = v
                    found = True

        if not found:
            continue

        # Re-encode
        new_raw = mapbox_vector_tile.encode(decoded)
        if is_gzipped:
            new_tile_data = gzip.compress(new_raw)
        else:
            new_tile_data = new_raw

        # Write back, handling deduped schema
        if is_deduped:
            # Check if this tile_id is shared by other map entries
            cursor.execute(
                'SELECT COUNT(*) FROM map WHERE tile_id=?',
                (tile_id_or_rowid,)
            )
            share_count = cursor.fetchone()[0]

            if share_count > 1:
                # Shared tile_id: create new unique tile_id
                new_tile_id = str(uuid.uuid4())
                cursor.execute(
                    'INSERT INTO images (tile_id, tile_data) VALUES (?, ?)',
                    (new_tile_id, new_tile_data)
                )
                cursor.execute(
                    'UPDATE map SET tile_id=? WHERE zoom_level=? AND tile_column=? AND tile_row=?',
                    (new_tile_id, z, x, tms_y)
                )
            else:
                # Not shared: update in place
                cursor.execute(
                    'UPDATE images SET tile_data=? WHERE tile_id=?',
                    (new_tile_data, tile_id_or_rowid)
                )
        else:
            cursor.execute(
                'UPDATE tiles SET tile_data=? WHERE rowid=?',
                (new_tile_data, tile_id_or_rowid)
            )

        tiles_updated += 1

    return tiles_updated, tiles_checked


def update_geojson(geojson_path, merge_id, new_properties):
    """Update a feature's properties in a large GeoJSON file using grep + in-place write.

    Returns True if updated, False if not (file not found, feature not found, or new data too large).
    """
    if not geojson_path or not os.path.isfile(geojson_path):
        return False

    # Use grep to find the byte offset of the _merge_id in the file
    try:
        result = subprocess.run(
            ['grep', '-b', '-o', '-m', '1', '-F', f'"_merge_id":"{merge_id}"', geojson_path],
            capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        print(f'[edit] grep timed out searching for _merge_id={merge_id}')
        return False

    if result.returncode != 0 or not result.stdout.strip():
        # Also try with spaces around the colon
        try:
            result = subprocess.run(
                ['grep', '-b', '-o', '-m', '1', '-F', f'"_merge_id": "{merge_id}"', geojson_path],
                capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            return False
        if result.returncode != 0 or not result.stdout.strip():
            return False

    # Parse byte offset from grep output (format: "offset:matched_line")
    line = result.stdout.strip().split('\n')[0]
    colon_pos = line.index(':')
    byte_offset = int(line[:colon_pos])

    with open(geojson_path, 'r+b') as f:
        # Seek backwards from the match to find the feature start: {"type"
        search_start = max(0, byte_offset - 10000)
        f.seek(search_start)
        chunk = f.read(byte_offset - search_start + 1)
        chunk_str = chunk.decode('utf-8', errors='replace')

        # Find the last occurrence of {"type" before our match
        feature_rel = chunk_str.rfind('{"type"')
        if feature_rel == -1:
            # Try alternate format
            feature_rel = chunk_str.rfind('{ "type"')
        if feature_rel == -1:
            return False

        feature_start = search_start + feature_rel

        # Now read forward from feature_start to find the end of this feature (brace matching)
        f.seek(feature_start)
        # Read enough to capture the feature (most features are under 10KB, but be generous)
        read_size = 100_000
        feature_chunk = f.read(read_size).decode('utf-8', errors='replace')

        depth = 0
        feature_end = -1
        in_string = False
        escape_next = False
        for i, ch in enumerate(feature_chunk):
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    feature_end = i + 1
                    break

        if feature_end == -1:
            return False

        old_feature_str = feature_chunk[:feature_end]

        # Parse the feature, update properties, re-serialize
        try:
            feature_obj = json.loads(old_feature_str)
        except json.JSONDecodeError:
            return False

        for k, v in new_properties.items():
            feature_obj['properties'][k] = v

        new_feature_str = json.dumps(feature_obj, ensure_ascii=False, separators=(',', ':'))

        old_len = len(old_feature_str.encode('utf-8'))
        new_len = len(new_feature_str.encode('utf-8'))

        if new_len > old_len:
            # Can't do in-place write if new string is longer
            print(f'[edit] geojson: new feature is {new_len - old_len} bytes longer, skipping in-place update')
            return False

        # Pad with spaces to fill the same byte length
        padding = old_len - new_len
        padded = new_feature_str + (' ' * padding)

        f.seek(feature_start)
        f.write(padded.encode('utf-8'))

    return True


def update_feature(name, merge_id, new_properties, tile_hint):
    """Orchestrator: update tiles in mbtiles and optionally update the source geojson."""
    ts = TILESETS.get(name)
    if not ts:
        return {'success': False, 'error': f'Tileset "{name}" not found'}

    conn = get_connection(name)
    meta = ts.get('metadata', {})
    min_zoom = int(meta.get('minzoom', 0))
    max_zoom = int(meta.get('maxzoom', 14))

    z_click = tile_hint.get('z', min_zoom)
    x_click = tile_hint.get('x', 0)
    y_click = tile_hint.get('y', 0)

    try:
        tiles_updated, tiles_checked = update_tiles(
            conn, merge_id, new_properties, z_click, x_click, y_click, min_zoom, max_zoom
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return {'success': False, 'error': f'Tile update failed: {e}'}

    # Update geojson (independent of mbtiles — if this fails, tiles are still committed)
    geojson_path = ts.get('geojson')
    geojson_updated = False
    if geojson_path:
        geojson_updated = update_geojson(geojson_path, merge_id, new_properties)

    return {
        'success': True,
        'tiles_updated': tiles_updated,
        'tiles_checked': tiles_checked,
        'geojson_updated': geojson_updated,
        'message': f'Updated {tiles_updated} tiles; {"updated" if geojson_updated else "did not update"} source .geojson'
    }


class MBTilesHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/')

        if path == '' or path == '/':
            self.serve_index()
            return

        # Parse /{name}/... from path
        parts = path.split('/', 2)  # ['', name, rest...]
        if len(parts) < 2:
            self.send_error(404, 'Not found')
            return

        name = parts[1]

        # Single-tileset backwards compat: if name isn't a known tileset
        # and there's only one tileset, treat entire path as under that tileset
        if name not in TILESETS and len(TILESETS) == 1:
            only_name = next(iter(TILESETS))
            rest = path  # e.g. /metadata.json or /14/123/456.pbf

            if rest == '/metadata.json':
                self.serve_metadata(only_name)
            else:
                self.serve_tile(only_name, rest)
            return

        if name not in TILESETS:
            self.send_error(404, f'Tileset "{name}" not found')
            return

        rest = '/' + parts[2] if len(parts) > 2 else '/'

        if rest == '/' or rest == '':
            self.serve_tileset_info(name)
        elif rest == '/metadata.json':
            self.serve_metadata(name)
        else:
            self.serve_tile(name, rest)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')

        # Parse /{name}/edit or /edit (single-tileset compat)
        parts = path.split('/', 2)

        if len(parts) == 2 and parts[1] == 'edit' and len(TILESETS) == 1:
            name = next(iter(TILESETS))
        elif len(parts) >= 3:
            name = parts[1]
            rest = '/' + parts[2]
            if rest != '/edit':
                self.send_json_error(404, 'Not found')
                return
        else:
            self.send_json_error(404, 'Not found')
            return

        if name not in TILESETS:
            self.send_json_error(404, f'Tileset "{name}" not found')
            return

        # Read request body
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_json_error(400, 'Empty request body')
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError as e:
            self.send_json_error(400, f'Invalid JSON: {e}')
            return

        # Validate fields
        merge_id = body.get('_merge_id')
        if not merge_id:
            self.send_json_error(400, 'Missing _merge_id')
            return

        properties = body.get('properties')
        if not isinstance(properties, dict):
            self.send_json_error(400, 'Missing or invalid properties object')
            return

        tile_hint = body.get('tile_hint', {})
        if not isinstance(tile_hint, dict):
            self.send_json_error(400, 'tile_hint must be an object')
            return

        for key in ('z', 'x', 'y'):
            if key in tile_hint and not isinstance(tile_hint[key], int):
                self.send_json_error(400, f'tile_hint.{key} must be an integer')
                return

        result = update_feature(name, merge_id, properties, tile_hint)

        status = 200 if result.get('success') else 500
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def send_json_error(self, status, message):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({'success': False, 'error': message}).encode())

    def serve_index(self):
        port = self.server.server_address[1]
        rows = ''
        for name, ts in TILESETS.items():
            meta = ts.get('metadata', {})
            rows += (
                f'<tr>'
                f'<td><a href="/{name}/">{name}</a></td>'
                f'<td>{meta.get("format", "?")}</td>'
                f'<td>{meta.get("minzoom", "?")} - {meta.get("maxzoom", "?")}</td>'
                f'<td style="font-size:11px">{meta.get("bounds", "?")}</td>'
                f'<td><code>http://localhost:{port}/{name}/{{z}}/{{x}}/{{y}}.pbf</code></td>'
                f'</tr>'
            )

        body = f"""<html><body>
<h2>MBTiles Tile Server</h2>
<p>{len(TILESETS)} tileset(s) loaded</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:monospace;font-size:13px">
<tr><th>Name</th><th>Format</th><th>Zoom</th><th>Bounds</th><th>Tile URL</th></tr>
{rows}
</table>
</body></html>"""

        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body.encode())

    def serve_tileset_info(self, name):
        port = self.server.server_address[1]
        meta = TILESETS[name].get('metadata', {})
        body = f"""<html><body>
<h2>{name}</h2>
<p><b>File:</b> {TILESETS[name]['path']}</p>
<p><b>Format:</b> {meta.get('format', 'unknown')}</p>
<p><b>Zoom:</b> {meta.get('minzoom', '?')} - {meta.get('maxzoom', '?')}</p>
<p><b>Bounds:</b> {meta.get('bounds', '?')}</p>
<p><b>Center:</b> {meta.get('center', '?')}</p>
<p><b>Tile URL:</b> <code>http://localhost:{port}/{name}/{{z}}/{{x}}/{{y}}.pbf</code></p>
<p><a href="/{name}/metadata.json">Metadata (JSON)</a> | <a href="/">Back to index</a></p>
</body></html>"""

        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body.encode())

    def serve_metadata(self, name):
        try:
            conn = get_connection(name)
            cursor = conn.cursor()
            cursor.execute('SELECT name, value FROM metadata')
            metadata = {row[0]: row[1] for row in cursor.fetchall()}

            for key in ('json', 'strategies'):
                if key in metadata:
                    try:
                        metadata[key] = json.loads(metadata[key])
                    except (json.JSONDecodeError, TypeError):
                        pass

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(metadata, indent=2).encode())
        except Exception as e:
            self.send_error(500, str(e))

    def serve_tile(self, name, path):
        match = re.match(r'^/(\d+)/(\d+)/(\d+)(?:\.pbf)?$', path)
        if not match:
            self.send_error(404, 'Not found')
            return

        z, x, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        tms_y = (1 << z) - 1 - y

        try:
            conn = get_connection(name)
            cursor = conn.cursor()
            cursor.execute(
                'SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?',
                (z, x, tms_y)
            )
            row = cursor.fetchone()

            if not row:
                self.send_response(204)
                self.send_cors_headers()
                self.end_headers()
                return

            tile_data = row[0]
            is_gzipped = tile_data[:2] == b'\x1f\x8b'

            self.send_response(200)
            self.send_header('Content-Type', 'application/x-protobuf')
            if is_gzipped:
                self.send_header('Content-Encoding', 'gzip')
            self.send_cors_headers()
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(tile_data)

        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, format, *args):
        if args and '200' not in str(args[0]) and '204' not in str(args[0]):
            super().log_message(format, *args)


def load_tileset(filepath):
    """Load and validate an mbtiles file, return its metadata."""
    name = os.path.splitext(os.path.basename(filepath))[0]
    conn = sqlite3.connect(filepath)
    cursor = conn.cursor()
    cursor.execute('SELECT count(*) FROM tiles')
    count = cursor.fetchone()[0]
    cursor.execute('SELECT name, value FROM metadata')
    metadata = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()
    return name, count, metadata


if __name__ == '__main__':
    files = []
    port = 8081
    geojson_path = None

    # Parse args: all non-flag args are mbtiles files, --port sets port, --geojson sets geojson path
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--port' and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == '--geojson' and i + 1 < len(args):
            geojson_path = args[i + 1]
            i += 2
        elif args[i].startswith('--'):
            print(f'Unknown flag: {args[i]}')
            sys.exit(1)
        else:
            files.append(args[i])
            i += 1

    if not files:
        print(__doc__)
        sys.exit(1)

    # Load all tilesets
    for filepath in files:
        try:
            name, count, metadata = load_tileset(filepath)
            # Handle duplicate names
            original_name = name
            suffix = 2
            while name in TILESETS:
                name = f'{original_name}_{suffix}'
                suffix += 1

            # Determine geojson path: explicit flag, or derive from mbtiles name
            if geojson_path:
                gj_path = os.path.abspath(geojson_path)
            else:
                derived = os.path.splitext(filepath)[0] + '.geojson'
                gj_path = os.path.abspath(derived) if os.path.isfile(derived) else None

            TILESETS[name] = {
                'path': os.path.abspath(filepath),
                'metadata': metadata,
                'connection': None,
                'geojson': gj_path
            }
            print(f'  [{name}]')
            print(f'    File:    {filepath}')
            print(f'    Tiles:   {count}')
            print(f'    Format:  {metadata.get("format", "unknown")}')
            print(f'    Zoom:    {metadata.get("minzoom", "?")} - {metadata.get("maxzoom", "?")}')
            print(f'    Bounds:  {metadata.get("bounds", "?")}')
            print(f'    GeoJSON: {gj_path or "(none)"}')
            print(f'    URL:     http://localhost:{port}/{name}/{{z}}/{{x}}/{{y}}.pbf')
            print(f'    Edit:    POST http://localhost:{port}/{name}/edit')
            print()
        except Exception as e:
            print(f'Error loading {filepath}: {e}')
            sys.exit(1)

    print(f'{len(TILESETS)} tileset(s) loaded')
    print(f'Index:    http://localhost:{port}/')
    print()

    server = HTTPServer(('0.0.0.0', port), MBTilesHandler)
    print(f'Listening on http://localhost:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.server_close()
