# Topologis QGIS Plugin

Publish vector layers from QGIS to a [Topologis](https://topologis.com) project in a couple of clicks.

## Features

- Pick any subset of the loaded vector layers.
- Automatic reprojection to EPSG:4326 (WGS84) on the fly.
- Background upload via the QGIS task manager, with a cancellable progress bar.
- Token-based auth - paste once, persisted across sessions.

Supports Point, Line, and Polygon layers. Raster, mesh, and other layer types are listed but disabled.

## Installation

### From the QGIS Plugin Repository

Plugins -> Manage and Install Plugins -> search "Topologis".

### From source

1. Clone or download this repository.
2. Copy the `plugins/qgis` folder into your QGIS profile's plugin directory and rename it to `topologis`:
   - Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/topologis`
   - macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/topologis`
   - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\topologis`
3. Restart QGIS, then enable "Topologis" in Plugins -> Manage and Install Plugins -> Installed.

## Usage

1. Generate an import token in your Topologis project's settings. See the [docs](https://topologis.com/docs/topologis-app/import-data#qgis).
2. Click the Topologis icon in the toolbar (or Plugins -> Topologis -> Publish to Topologis).
3. Tick the layers to publish, paste the token, and click **Export**.

## Project layout

```
plugins/qgis/
├── __init__.py              # QGIS classFactory entry point
├── metadata.txt             # plugin manifest read by QGIS
├── topologis_plugin.py      # toolbar/menu wiring
├── core/
│   ├── config.py            # API endpoint configuration
│   ├── export_task.py       # background QgsTask
│   └── http_client.py       # urllib helpers (POST JSON, PUT with progress)
├── gui/
│   └── export_dialog.py     # the layer-picker dialog
└── resources/
    └── icons/               # toolbar icon, dialog logo
```

## Development

Set `TOPOLOGIS_API_URL` in QGIS's environment to point the plugin at a non-production server, e.g. `TOPOLOGIS_API_URL=http://localhost:5000`.

A symlink is the easiest way to iterate on the plugin without re-copying after every change:

```bash
ln -s "$(pwd)/plugins/qgis" \
  "$HOME/.local/share/QGIS/QGIS3/profiles/default/python/plugins/topologis"
```

Use the Plugin Reloader plugin to pick up changes without restarting QGIS.

## License

Source code is released under the [GNU GPL v3](LICENSE.txt).

The Topologis name, wordmark, and logo are trademarks - see [TRADEMARKS.md](TRADEMARKS.md). They are not covered by the GPL.
