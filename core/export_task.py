"""Background ``QgsTask`` that exports vector layers to Topologis.

Flow for a single run:

1. Ask the API for a presigned upload URL per selected layer
   (``POST /api/public/qgis-get-urls``).
2. For each layer:
   a. Reproject to EPSG:4326 and write GeoJSON to a temp file.
   b. PUT that file to the presigned URL with progress reporting.
   c. Tell the API the upload landed
      (``POST /api/public/qgis-create-import-job``) so it can kick off the
      server-side import job.
3. Emit ``done`` with a per-layer success/failure summary.

The task runs on a worker thread managed by ``QgsApplication.taskManager``;
we never touch the GUI directly - the dialog connects to the
``progressUpdated`` and ``done`` signals instead.
"""

import os
import tempfile
from typing import List, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import pyqtSignal

from .config import API_URL
from .http_client import Cancelled, post_json, put_file_with_progress


# All Topologis layers are stored in WGS84 lon/lat. Any other layer CRS is
# transformed on write so the server doesn't have to guess.
_TARGET_CRS = "EPSG:4326"


class ExportTask(QgsTask):
    """Long-running export task driven from :class:`TopologisExportDialog`.

    Signals:
        progressUpdated(current, total, layer_name, pct, phase):
            Per-layer progress. ``phase`` is ``"preparing"`` while the
            GeoJSON is being written, then ``"uploading"`` while bytes are
            being streamed.
        done():
            Emitted exactly once on the main thread after all layers have
            been processed (or the task was cancelled / errored).
    """

    progressUpdated = pyqtSignal(int, int, str, int, str)
    done = pyqtSignal()

    def __init__(self, token: str, layers: List[QgsVectorLayer]):
        super().__init__("Topologis export", QgsTask.CanCancel)
        self._token = token
        self._layers = layers

        # Per-layer outcome list. Each entry is a dict with keys
        # ``layerName``, ``ok`` (bool) and ``error`` (str | None). The dialog
        # reads this in ``finished`` to render the closing summary.
        self.summary: List[dict] = []

        # Set when the run() method itself cannot proceed (e.g. the initial
        # presigned-URL request failed). When this is set, ``finished`` fans
        # the same error message out across every layer's summary entry.
        self.fatal_error: Optional[str] = None

    # ------------------------------------------------------------------
    # QgsTask hooks - called by the QGIS task manager on a worker thread.
    # ------------------------------------------------------------------

    def run(self) -> bool:
        """Execute the export. Returns ``True`` on completion (even partial
        completion with per-layer failures) and ``False`` only on a fatal
        error that prevents any layer from being processed at all."""
        try:
            # One round-trip up front to validate the token and reserve a
            # presigned URL per layer. If the token is wrong we want to fail
            # fast, before writing any temp files.
            names = [layer.name() for layer in self._layers]
            status, body = post_json(
                f"{API_URL}/api/public/qgis-get-urls",
                {"token": self._token, "names": names},
            )
            if status != 200:
                self.fatal_error = (
                    body.get("error") or f"Failed to get upload URLs (HTTP {status})"
                )
                return False

            urls = body.get("urls", [])
            if len(urls) != len(self._layers):
                self.fatal_error = "Server returned mismatched URL count"
                return False

            n = len(self._layers)
            for i, (layer, entry) in enumerate(zip(self._layers, urls)):
                if self.isCanceled():
                    break
                self._process_layer(i, n, layer, entry)
            return True
        except Cancelled:
            # A cancel during the inner upload loop is not a failure - just
            # stop and let ``finished`` report the partial results.
            return True
        except Exception as e:
            self.fatal_error = str(e)
            return False

    def finished(self, result: bool):
        """Always called on the main thread once :meth:`run` returns.

        We use it to (a) backfill per-layer error entries when the failure
        happened before any layer was processed, and (b) notify the dialog
        via the ``done`` signal so it can update the UI safely.
        """
        if not result and self.fatal_error and not self.summary:
            self.summary = [
                {"layerName": layer.name(), "ok": False, "error": self.fatal_error}
                for layer in self._layers
            ]
        self.done.emit()

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------

    def _process_layer(self, i: int, n: int, layer: QgsVectorLayer, entry: dict):
        """Export and upload a single layer. Failures are captured in
        ``self.summary`` rather than raised, so one bad layer doesn't abort
        the whole batch."""
        # The server may have sanitised the layer name. Use whatever it gave
        # us back so the import job points at the same key on S3.
        layer_name = entry.get("layerName") or layer.name()
        safe_name = entry.get("safeName")
        url = entry.get("url")

        # Initial "preparing" tick so the UI updates immediately when we
        # advance to the next layer, even before the GeoJSON write finishes.
        self.progressUpdated.emit(i + 1, n, layer_name, 0, "preparing")
        self.setProgress((i / n) * 100 if n else 0)

        tmp_path: Optional[str] = None
        try:
            tmp_path = self._write_geojson(layer)

            self.progressUpdated.emit(i + 1, n, layer_name, 0, "uploading")

            def progress_cb(sent: int, total: int):
                # Per-layer percentage drives the inline status string.
                pct = int(sent * 100 / total) if total else 0
                self.progressUpdated.emit(i + 1, n, layer_name, pct, "uploading")
                # Overall percentage drives the QGIS task manager bar.
                overall = ((i + (sent / total if total else 0)) / n) * 100
                self.setProgress(overall)

            put_file_with_progress(url, tmp_path, progress_cb, self.isCanceled)

            # Tell the API that the file landed. The server will then move
            # it from staging to the user's project asynchronously.
            status, body = post_json(
                f"{API_URL}/api/public/qgis-create-import-job",
                {"token": self._token, "safeName": safe_name},
            )
            if status != 200:
                self.summary.append({
                    "layerName": layer_name,
                    "ok": False,
                    "error": body.get("error") or f"create-import-job HTTP {status}",
                })
                return

            self.summary.append({"layerName": layer_name, "ok": True, "error": None})
        except Cancelled:
            # Propagate so run() can stop the outer loop and return cleanly.
            raise
        except Exception as e:
            self.summary.append({"layerName": layer_name, "ok": False, "error": str(e)})
        finally:
            # Always clean up the temp file - it can be tens or hundreds of
            # megabytes and QGIS sessions are long-lived.
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    # Best effort. Most platforms remove temp files on reboot.
                    pass

    def _write_geojson(self, layer: QgsVectorLayer) -> str:
        """Serialize ``layer`` to a temporary GeoJSON file in EPSG:4326.

        Returns the path to the file. The caller is responsible for deleting
        it once the upload finishes (or fails).
        """
        # ``mkstemp`` returns an open file descriptor we don't actually need;
        # close it so QGIS can write to the path itself.
        fd, tmp_path = tempfile.mkstemp(suffix=".geojson")
        os.close(fd)

        target_crs = QgsCoordinateReferenceSystem(_TARGET_CRS)
        transform_context = QgsProject.instance().transformContext()

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GeoJSON"
        options.fileEncoding = "UTF-8"
        # Skip the transform when the layer is already in WGS84 - cheaper and
        # avoids any tiny rounding error that ``QgsCoordinateTransform`` would
        # introduce on an identity transform.
        if layer.crs() != target_crs:
            options.ct = QgsCoordinateTransform(layer.crs(), target_crs, transform_context)

        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, tmp_path, transform_context, options
        )
        # Result is a tuple ``(error_code, error_message, ...)``. Anything
        # other than ``NoError`` means the file on disk is unusable.
        error_code = result[0]
        if error_code != QgsVectorFileWriter.NoError:
            error_msg = result[1] if len(result) > 1 else "GeoJSON write failed"
            raise RuntimeError(f"GeoJSON write failed: {error_msg}")
        return tmp_path
