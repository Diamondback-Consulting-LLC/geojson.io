#!/usr/bin/env python3
"""
Simple HTTP tile server for .mbtiles files.

Usage:
    python3 serve_mbtiles.py <file1.mbtiles> [file2.mbtiles ...] [--port PORT]

Serves multiple tilesets, each under a name derived from the filename:

    http://localhost:8081/                       - index listing all tilesets
    http://localhost:8081/{name}/metadata.json   - metadata for a tileset
    http://localhost:8081/{name}/{z}/{x}/{y}.pbf - tiles for a tileset

Example:
    python3 serve_mbtiles.py west.mbtiles east.mbtiles midwest.mbtiles --port 8081

    Tile URLs:
        http://localhost:8081/west/{z}/{x}/{y}.pbf
        http://localhost:8081/east/{z}/{x}/{y}.pbf
        http://localhost:8081/midwest/{z}/{x}/{y}.pbf
"""

import json
import os
import sqlite3
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import re


# { name: { path, connection } }
TILESETS = {}


def get_connection(name):
    ts = TILESETS.get(name)
    if not ts:
        return None
    if ts.get('connection') is None:
        ts['connection'] = sqlite3.connect(ts['path'], check_same_thread=False)
    return ts['connection']


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

        # For single-tileset backwards compat: if name looks like a zoom level
        # and there's only one tileset, treat path as /{z}/{x}/{y}
        if name.isdigit() and len(TILESETS) == 1:
            only_name = next(iter(TILESETS))
            self.serve_tile(only_name, path)
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

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

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

    # Parse args: all non-flag args are mbtiles files, --port sets port
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--port' and i + 1 < len(args):
            port = int(args[i + 1])
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

            TILESETS[name] = {
                'path': os.path.abspath(filepath),
                'metadata': metadata,
                'connection': None
            }
            print(f'  [{name}]')
            print(f'    File:   {filepath}')
            print(f'    Tiles:  {count}')
            print(f'    Format: {metadata.get("format", "unknown")}')
            print(f'    Zoom:   {metadata.get("minzoom", "?")} - {metadata.get("maxzoom", "?")}')
            print(f'    Bounds: {metadata.get("bounds", "?")}')
            print(f'    URL:    http://localhost:{port}/{name}/{{z}}/{{x}}/{{y}}.pbf')
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
