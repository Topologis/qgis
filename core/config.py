"""Endpoint configuration for the Topologis backend.

By default the plugin talks to the production API. Set ``TOPOLOGIS_API_URL``
in QGIS's environment (e.g. ``TOPOLOGIS_API_URL=http://localhost:5000``) to
point at a local or staging server. Set ``TOPOLOGIS_DEBUG=1`` to write
developer diagnostics to the QGIS message log.
"""

import os


_DEFAULT_API_URL = "https://topologis.com"
_TRUE_VALUES = {"1", "true", "yes", "on"}

# Base URL for the public REST API. The plugin appends paths such as
# ``/api/public/qgis-get-urls`` and ``/api/public/qgis-create-import-job``,
# so we strip a trailing slash to keep the joined URLs clean.
API_URL = os.environ.get("TOPOLOGIS_API_URL", _DEFAULT_API_URL).rstrip("/")
DEBUG = os.environ.get("TOPOLOGIS_DEBUG", "").strip().lower() in _TRUE_VALUES
