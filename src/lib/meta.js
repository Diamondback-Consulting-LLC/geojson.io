const geojsonRandom = require('geojson-random'),
  geojsonExtent = require('@mapbox/geojson-extent'),
  geojsonFlatten = require('geojson-flatten'),
  polyline = require('@mapbox/polyline'),
  wkx = require('wkx'),
  Buffer = require('buffer/').Buffer,
  zoomextent = require('../lib/zoomextent'),
  openlr = require('openlr-js');

function isValidTileUrl(url) {
  try {
    const u = new URL(url);

    // Must use HTTPS
    if (u.protocol !== 'https:') return false;

    // Optional: check that {z}, {x}, {y} placeholders are present
    const hasPlaceholders =
      url.includes('{z}') && url.includes('{x}') && url.includes('{y}');
    if (!hasPlaceholders) return false;

    return true;
  } catch (e) {
    // Invalid URL format
    return false;
  }
}

function isValidTilesetName(name) {
  const trimmed = name.trim();
  return (
    /^[a-zA-Z0-9 ]+$/.test(trimmed) &&
    trimmed.length >= 3 &&
    trimmed.length <= 25
  );
}

module.exports.adduserlayer = function (context, url, name) {
  function addUserSourceAndLayer() {
    // if the source and layer aren't present, add them
    context.map.setStyle({
      name: 'user-layer',
      version: 8,
      sources: {
        'user-layer': {
          type: 'raster',
          tiles: [url],
          tileSize: 256
        }
      },
      layers: [
        {
          id: 'user-layer',
          type: 'raster',
          source: 'user-layer',
          minzoom: 0,
          maxzoom: 22
        }
      ]
    });

    // make this layer's button active
    d3.select('.layer-switch .active').classed('active', false);
    d3.select('.user-layer-button').classed('active', true);

    context.data.set({
      mapStyleLoaded: true
    });
  }

  try {
    if (!isValidTileUrl(url)) {
      throw new Error(
        'Invalid tile URL. Must be HTTPS and include {z}, {x}, {y}.'
      );
    }

    if (!isValidTilesetName(name)) {
      throw new Error(
        'Invalid tileset name. Must be 3-25 characters long and contain only alphanumeric characters and spaces.'
      );
    }

    // reset the control if a user-layer was added before
    d3.select('.user-layer-button').remove();

    // append a button to the existing style selection UI
    d3.select('.layer-switch')
      .append('button')
      .attr('class', 'pad0x user-layer-button')
      .on('click', addUserSourceAndLayer)
      .text(name);

    addUserSourceAndLayer(url);
  } catch (e) {
    alert(e.message);
  }
};

module.exports.addvectortiles = function (context, baseUrl) {
  // Support both http://localhost:8081/west and http://localhost:8081/west/
  const cleanUrl = baseUrl.replace(/\/$/, '');
  const metadataUrl = cleanUrl + '/metadata.json';

  fetch(metadataUrl)
    .then((res) => {
      if (!res.ok)
        throw new Error('Failed to fetch metadata from ' + metadataUrl);
      return res.json();
    })
    .then((metadata) => {
      const sourceId = 'vector-tile-preview';
      const fillLayerId = 'vector-tile-preview-fill';
      const lineLayerId = 'vector-tile-preview-line';

      // Remove existing preview layers/source if present
      if (context.map.getLayer(fillLayerId))
        context.map.removeLayer(fillLayerId);
      if (context.map.getLayer(lineLayerId))
        context.map.removeLayer(lineLayerId);
      if (context.map.getSource(sourceId)) context.map.removeSource(sourceId);

      // Build tile URL from base
      const tileUrl = cleanUrl + '/{z}/{x}/{y}.pbf';

      // Extract layer info from metadata
      const layerInfo = metadata.json || {};
      const vectorLayers = layerInfo.vector_layers || [];
      const sourceLayer =
        vectorLayers.length > 0 ? vectorLayers[0].id : 'default';

      const minzoom = parseInt(metadata.minzoom, 10) || 0;
      const maxzoom = parseInt(metadata.maxzoom, 10) || 22;

      console.log('[geojson.io] Adding vector tiles:', {
        tileUrl: tileUrl,
        sourceLayer: sourceLayer,
        minzoom: minzoom,
        maxzoom: maxzoom,
        bounds: metadata.bounds,
        center: metadata.center
      });

      context.map.addSource(sourceId, {
        type: 'vector',
        tiles: [tileUrl],
        minzoom: minzoom,
        maxzoom: maxzoom
      });

      context.map.addLayer({
        id: fillLayerId,
        type: 'fill',
        source: sourceId,
        'source-layer': sourceLayer,
        minzoom: minzoom,
        paint: {
          'fill-color': '#088',
          'fill-opacity': 0.4
        }
      });

      context.map.addLayer({
        id: lineLayerId,
        type: 'line',
        source: sourceId,
        'source-layer': sourceLayer,
        minzoom: minzoom,
        paint: {
          'line-color': '#066',
          'line-width': 0.5
        }
      });

      // Click to inspect and edit feature properties
      context.map.on('click', fillLayerId, (e) => {
        if (!e.features || !e.features.length) return;

        const feature = e.features[0];
        const props = feature.properties || {};
        const keys = Object.keys(props);

        // Calculate tile coordinates from click point for tile_hint
        const zoom = Math.floor(context.map.getZoom());
        const lng = e.lngLat.lng;
        const lat = e.lngLat.lat;
        const tileX = Math.floor(((lng + 180) / 360) * (1 << zoom));
        const latRad = (lat * Math.PI) / 180;
        const tileY = Math.floor(
          ((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) /
            2) *
            (1 << zoom)
        );

        let html =
          '<div style="max-height:300px;overflow-y:auto;font-size:12px;font-family:monospace;">';
        html +=
          '<form id="vt-edit-form" action="javascript:void(0);" style="margin:0">';
        html += '<table style="border-collapse:collapse;width:100%">';
        for (let i = 0; i < keys.length; i++) {
          const k = keys[i];
          const v = String(props[k]);
          const isId = k === '_merge_id';
          html +=
            '<tr style="border-bottom:1px solid #eee">' +
            '<td style="padding:2px 4px 2px 0;font-weight:bold;vertical-align:top;white-space:nowrap;color:#066">' +
            '<input type="text" data-role="key" value="' +
            k.replace(/"/g, '&quot;') +
            '" readonly ' +
            'style="border:none;background:transparent;font-weight:bold;color:#066;width:100%;font-size:12px;font-family:monospace;padding:0">' +
            '</td>' +
            '<td style="padding:2px 0;word-break:break-all">' +
            '<input type="text" data-role="value" data-key="' +
            k.replace(/"/g, '&quot;') +
            '" value="' +
            v.replace(/"/g, '&quot;') +
            '"' +
            (isId ? ' readonly' : '') +
            ' style="border:1px solid ' +
            (isId ? 'transparent' : '#ccc') +
            ';width:100%;font-size:12px;font-family:monospace;padding:1px 3px;box-sizing:border-box;' +
            (isId ? 'background:transparent;color:#999' : 'background:#fff') +
            '">' +
            '</td>' +
            '</tr>';
        }
        html += '</table>';
        html +=
          '<div style="padding:6px 0 2px;display:flex;align-items:center;gap:6px">' +
          '<button type="submit" id="vt-save-btn" style="background:#088;color:#fff;border:none;padding:4px 12px;cursor:pointer;font-size:12px;border-radius:2px">Save</button>' +
          '<button type="button" id="vt-cancel-btn" style="background:#eee;color:#333;border:1px solid #ccc;padding:4px 12px;cursor:pointer;font-size:12px;border-radius:2px">Cancel</button>' +
          '<span id="vt-status" style="font-size:11px;color:#666"></span>' +
          '</div>';
        html += '</form></div>';

        const mapboxgl = require('mapbox-gl');
        const popup = new mapboxgl.Popup({ maxWidth: '420px' })
          .setLngLat(e.lngLat)
          .setHTML(html)
          .addTo(context.map);

        const popupEl = popup.getElement();

        // Bind cancel button
        const cancelBtn = popupEl.querySelector('#vt-cancel-btn');
        if (cancelBtn) {
          cancelBtn.addEventListener('click', () => popup.remove());
        }

        // Bind save
        const form = popupEl.querySelector('#vt-edit-form');
        if (form) {
          form.addEventListener('submit', (evt) => {
            evt.preventDefault();
            const saveBtn = popupEl.querySelector('#vt-save-btn');
            const status = popupEl.querySelector('#vt-status');

            // Collect edited properties
            const editedProps = {};
            const valueInputs = form.querySelectorAll(
              'input[data-role="value"]'
            );
            valueInputs.forEach((input) => {
              const key = input.getAttribute('data-key');
              if (key !== '_merge_id') {
                editedProps[key] = input.value;
              }
            });

            const mergeId = props['_merge_id'];
            if (!mergeId) {
              status.textContent = 'No _merge_id on feature';
              status.style.color = '#c00';
              return;
            }

            // Disable save while in-flight
            saveBtn.disabled = true;
            saveBtn.style.opacity = '0.5';
            status.textContent = 'Saving...';
            status.style.color = '#666';

            const editUrl = cleanUrl + '/edit';
            fetch(editUrl, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                _merge_id: String(mergeId),
                properties: editedProps,
                tile_hint: { z: zoom, x: tileX, y: tileY }
              })
            })
              .then((res) => res.json())
              .then((result) => {
                if (result.success) {
                  status.textContent =
                    'Updated ' +
                    result.tiles_updated +
                    ' tiles' +
                    (result.geojson_updated ? ' + geojson' : '');
                  status.style.color = '#080';

                  // Refresh tiles: swap tile URL with cache buster to force re-fetch
                  const source = context.map.getSource(sourceId);
                  if (source && source.setTiles) {
                    source.setTiles([
                      cleanUrl + '/{z}/{x}/{y}.pbf?_t=' + Date.now()
                    ]);
                  }
                } else {
                  status.textContent = result.error || 'Update failed';
                  status.style.color = '#c00';
                  saveBtn.disabled = false;
                  saveBtn.style.opacity = '1';
                }
              })
              .catch((err) => {
                status.textContent = 'Error: ' + err.message;
                status.style.color = '#c00';
                saveBtn.disabled = false;
                saveBtn.style.opacity = '1';
              });
          });
        }
      });

      // Pointer cursor on hover
      context.map.on('mouseenter', fillLayerId, () => {
        context.map.getCanvas().style.cursor = 'pointer';
      });
      context.map.on('mouseleave', fillLayerId, () => {
        context.map.getCanvas().style.cursor = '';
      });

      // Fly to the tileset's bounds/center
      if (metadata.center) {
        const parts = metadata.center.split(',').map(Number);
        if (parts.length >= 3) {
          context.map.flyTo({
            center: [parts[0], parts[1]],
            zoom: parts[2]
          });
        }
      } else if (metadata.bounds) {
        const b = metadata.bounds.split(',').map(Number);
        if (b.length === 4) {
          context.map.fitBounds(
            [
              [b[0], b[1]],
              [b[2], b[3]]
            ],
            { padding: 20 }
          );
        }
      }
    })
    .catch((e) => {
      console.error('[geojson.io] Error adding vector tile layer:', e);
      alert('Error adding vector tile layer: ' + e.message);
    });
};

module.exports.zoomextent = function (context) {
  zoomextent(context);
};

module.exports.clear = function (context) {
  context.data.clear();
};

module.exports.random = function (context, count, type) {
  context.data.mergeFeatures(geojsonRandom(count, type).features, 'meta');
};

module.exports.bboxify = function (context) {
  context.data.set({ map: geojsonExtent.bboxify(context.data.get('map')) });
};

module.exports.flatten = function (context) {
  context.data.set({ map: geojsonFlatten(context.data.get('map')) });
};

module.exports.polyline = function (context) {
  const input = prompt('Enter your polyline');
  try {
    const decoded = polyline.toGeoJSON(input);
    context.data.set({ map: decoded });
  } catch (e) {
    alert('Sorry, we were unable to decode that polyline');
  }
};

module.exports.polyline6 = function (context) {
  const input = prompt('Enter your polyline');
  try {
    const decoded = polyline.toGeoJSON(input, 6);
    context.data.set({ map: decoded });
  } catch (e) {
    alert('Sorry, we were unable to decode that polyline');
  }
};

module.exports.wkxBase64 = function (context) {
  const input = prompt('Enter your Base64 encoded WKB/EWKB');
  try {
    const decoded = wkx.Geometry.parse(Buffer.from(input, 'base64'));
    context.data.set({ map: decoded.toGeoJSON() });
    zoomextent(context);
  } catch (e) {
    console.error(e);
    alert('Sorry, we were unable to decode that Base64 encoded WKX data');
  }
};

module.exports.wkxHex = function (context) {
  const input = prompt('Enter your Hex encoded WKB/EWKB');
  try {
    const decoded = wkx.Geometry.parse(Buffer.from(input, 'hex'));
    context.data.set({ map: decoded.toGeoJSON() });
    zoomextent(context);
  } catch (e) {
    console.error(e);
    alert('Sorry, we were unable to decode that Hex encoded WKX data');
  }
};

module.exports.wkxString = function (context) {
  const input = prompt('Enter your WKT/EWKT String');
  try {
    const decoded = wkx.Geometry.parse(input);
    context.data.set({ map: decoded.toGeoJSON() });
    zoomextent(context);
  } catch (e) {
    console.error(e);
    alert('Sorry, we were unable to decode that WKT data');
  }
};

module.exports.openLR = function (context) {
  const openLrInput = prompt(
    'Enter your OpenLR String(s) - comma separated for multiple'
  );
  try {
    // Split by comma and trim whitespace
    const openLrStrings = openLrInput
      .split(',')
      .map((s) => s.trim().trim('"'))
      .filter((s) => s.length > 0);

    const BinaryDecoder = openlr.BinaryDecoder;
    const binaryDecoder = new BinaryDecoder();
    const LocationReference = openlr.LocationReference;
    const Serializer = openlr.Serializer;

    const allFeatures = [];
    const errors = [];

    // Process each OpenLR string
    for (const openLrString of openLrStrings) {
      try {
        const openLrBinary = Buffer.from(openLrString, 'base64');
        const locationReference = LocationReference.fromIdAndBuffer(
          'binary',
          openLrBinary
        );
        const rawLocationReference =
          binaryDecoder.decodeData(locationReference);
        const jsonObject = Serializer.serialize(rawLocationReference);

        switch (jsonObject.type) {
          case 'RawLineLocationReference':
            {
              const coordinates = jsonObject.properties._points.properties.map(
                ({ properties }) => [
                  properties._longitude,
                  properties._latitude
                ]
              );
              allFeatures.push({
                type: 'Feature',
                geometry: {
                  type: 'LineString',
                  coordinates: coordinates
                },
                properties: {
                  raw: jsonObject,
                  input: openLrString
                }
              });
            }
            break;
          case 'RawGeoCoordLocationReference':
            {
              const point = jsonObject.properties._geoCoord.properties;
              allFeatures.push({
                type: 'Feature',
                geometry: {
                  type: 'Point',
                  coordinates: [point._longitude, point._latitude]
                },
                properties: {
                  raw: jsonObject,
                  input: openLrString
                }
              });
            }
            break;
          case 'RawPolygonLocationReference':
            {
              const polygonCorners = jsonObject.properties._corners.properties;
              const polygonCoordinates = polygonCorners.map((point) => [
                point.properties._longitude,
                point.properties._latitude
              ]);
              allFeatures.push({
                type: 'Feature',
                geometry: {
                  type: 'Polygon',
                  coordinates: [[...polygonCoordinates]]
                },
                properties: {
                  raw: jsonObject,
                  input: openLrString
                }
              });
            }
            break;
          default:
            errors.push(
              `Unsupported OpenLR location type: ${jsonObject.type} for input: ${openLrString}`
            );
        }
      } catch (e) {
        errors.push(`Failed to decode: ${openLrString.substring(0, 20)}...`);
        console.error(`Error decoding ${openLrString}:`, e);
      }
    }

    if (allFeatures.length > 0) {
      const geojson = {
        type: 'FeatureCollection',
        features: allFeatures
      };
      context.data.set({ map: geojson });
      zoomextent(context);

      if (errors.length > 0) {
        alert(
          `Successfully decoded ${
            allFeatures.length
          } features, but encountered ${errors.length} error(s):\n${errors.join(
            '\n'
          )}`
        );
      }
    } else {
      alert(
        'Sorry, we were unable to decode any OpenLR data:\n' + errors.join('\n')
      );
    }
  } catch (e) {
    console.error(e);
    alert('Sorry, we were unable to decode that OpenLR data');
  }
};
