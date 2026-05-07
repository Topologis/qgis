"""Modal dialog for selecting layers and triggering an export.

The dialog is purely presentational: it collects user input (layer
selection + token), kicks off an :class:`ExportTask`, and translates
progress signals into label/progress-bar updates. All long-running work
happens on a worker thread inside the task.
"""

import base64
import json
import os
import time

from qgis.core import (
    QgsApplication,
    QgsProject,
    QgsSettings,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.export_task import ExportTask


# Resource paths are resolved relative to the plugin root, which is the
# parent of this ``gui/`` package.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))
_LOGO_PATH = os.path.join(_PLUGIN_DIR, "resources", "icons", "logo.png")
_ICON_FALLBACK_PATH = os.path.join(_PLUGIN_DIR, "resources", "icons", "icon.png")

# Help link shown next to the token input. Documentation explains how to
# generate an import token from a Topologis project's settings.
DOCS_TOKEN_URL = "https://topologis.com/docs/topologis-app/import-data#qgis"

# We can only export vector layers with simple Point/Line/Polygon geometry;
# raster layers, mesh layers, and unknown geometry types are listed in the
# UI but disabled. The set is checked against ``QgsVectorLayer.geometryType()``.
SUPPORTED_GEOMETRY_TYPES = {
    QgsWkbTypes.PointGeometry,
    QgsWkbTypes.LineGeometry,
    QgsWkbTypes.PolygonGeometry,
}
UNSUPPORTED_WARNING = "layer doesn't contain Polygon/Line/Point geometry"

# QGIS persists the import token in QSettings between sessions so users
# don't have to paste it every time. Stored unencrypted - same trust model
# as other QGIS plugins keeping API keys here.
TOKEN_SETTINGS_KEY = "topologis/import_token"


class TopologisExportDialog(QDialog):
    """Layer picker + token field + Export button.

    The lifecycle is single-shot: open dialog -> pick layers -> click Export
    -> watch progress -> see summary -> close. The same dialog instance is
    not reused across runs.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export to Topologis")
        self.resize(420, 480)

        # Reference to the running export task, or ``None`` when idle. Held
        # so we can call ``cancel()`` on the task from the close handler.
        self._task = None
        self._task_running = False

        layout = QVBoxLayout(self)

        # ---- Branding header ------------------------------------------------
        # Fall back to the toolbar icon if the logo asset is missing - keeps
        # the dialog usable for forks that strip the logo for trademark reasons.
        logo_path = _LOGO_PATH if os.path.exists(_LOGO_PATH) else _ICON_FALLBACK_PATH
        logo_label = QLabel(self)
        logo_label.setPixmap(QPixmap(logo_path).scaledToHeight(32))
        logo_label.setAlignment(Qt.AlignLeft)
        logo_label.setContentsMargins(0, 0, 0, 10)
        layout.addWidget(logo_label)

        # ---- Layer list -----------------------------------------------------
        layout.addWidget(QLabel("Layers"))
        self.layer_table = QTableWidget(self)
        self.layer_table.setColumnCount(2)
        self.layer_table.setHorizontalHeaderLabels(["Layer", "Operation"])
        self.layer_table.verticalHeader().setVisible(False)
        self.layer_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.layer_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.layer_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.layer_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.layer_table.setShowGrid(False)
        warning_icon = QApplication.style().standardIcon(QStyle.SP_MessageBoxWarning)

        # Sort supported (checkable) layers to the top so they're easier to
        # find when a project has dozens of unsupported raster/mesh layers.
        layers = list(QgsProject.instance().mapLayers().values())
        layers.sort(key=lambda layer: not _is_supported(layer))

        self.layer_table.setRowCount(len(layers))
        for row, layer in enumerate(layers):
            item = QTableWidgetItem(layer.name())
            # Stash the layer ID rather than a reference: layers can be
            # removed from the project while the dialog is open and we want
            # to fail gracefully when that happens.
            item.setData(Qt.UserRole, layer.id())
            if _is_supported(layer):
                item.setFlags(
                    Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                )
                item.setCheckState(Qt.Unchecked)
                self.layer_table.setItem(row, 0, item)

                combo = QComboBox(self.layer_table)
                combo.addItem("Replace existing", "replace")
                combo.addItem("Create new", "add")
                # Combo follows the row's check state - starts disabled
                # because rows start unchecked.
                combo.setEnabled(False)
                self.layer_table.setCellWidget(row, 1, combo)
            else:
                # Disable the row entirely - tooltip explains why.
                item.setFlags(Qt.NoItemFlags)
                item.setIcon(warning_icon)
                item.setToolTip(UNSUPPORTED_WARNING)
                self.layer_table.setItem(row, 0, item)

        self.layer_table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.layer_table, 1)

        # ---- Token input ----------------------------------------------------
        token_header = QHBoxLayout()
        token_header.addWidget(QLabel("Import Token"))
        token_header.addStretch()
        token_help = QLabel(
            f'<a href="{DOCS_TOKEN_URL}">How do I get a token?</a>',
            self,
        )
        token_help.setOpenExternalLinks(True)
        token_header.addWidget(token_help)
        layout.addLayout(token_header)

        self.token_input = MaskedTokenLineEdit(self)
        self.token_input.setPlaceholderText("Paste your import token")
        layout.addWidget(self.token_input)

        self.token_info = QLabel("", self)
        self.token_info.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.token_info)

        token_hint = QLabel("Click the field to view or edit your token.", self)
        token_hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(token_hint)

        # Keep the project/expiry line in sync with the field's real value.
        self.token_input.realTextChanged.connect(self._update_token_info)
        # Pre-fill with the token from a previous session if there is one.
        self.token_input.setRealText(QgsSettings().value(TOKEN_SETTINGS_KEY, "", type=str))

        # ---- Status / progress ---------------------------------------------
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        # ---- Buttons --------------------------------------------------------
        # Use a single button box that flips between "Close" (idle) and
        # "Cancel Export" (running). Avoids a layout shift mid-flow.
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel, parent=self)
        self.cancel_button = self.buttons.button(QDialogButtonBox.Cancel)
        self.cancel_button.setText("Close")
        self.export_button = self.buttons.addButton("Export", QDialogButtonBox.AcceptRole)
        self.buttons.rejected.connect(self._on_cancel_clicked)
        self.export_button.clicked.connect(self._on_export)
        layout.addWidget(self.buttons)

    # ------------------------------------------------------------------
    # Event handlers.
    # ------------------------------------------------------------------

    def _on_export(self):
        """Validate inputs and start the export task."""
        token = self.token_input.realText().strip()
        layers = self._collect_selected_layers()

        if not token:
            self._show_inline("Paste an Import Token first.", error=True)
            return
        if not layers:
            self._show_inline("Select at least one layer.", error=True)
            return

        # Persist the token so the next run pre-fills it.
        QgsSettings().setValue(TOKEN_SETTINGS_KEY, token)

        self._set_running(True)
        self._total_layers = len(layers)
        self.progress_bar.setValue(0)
        self._show_inline(f"Importing layer 1/{len(layers)}...")

        # Hand the task to QGIS's task manager so it shows up in the global
        # task panel and runs on a worker thread.
        self._task = ExportTask(token, layers)
        self._task.progressUpdated.connect(self._on_progress)
        self._task.done.connect(self._on_task_done)
        QgsApplication.taskManager().addTask(self._task)

    def _on_progress(self, current: int, total: int, layer_name: str, pct: int, phase: str):
        """Render an inline status string for the current layer's progress."""
        suffix = "preparing..." if phase == "preparing" else f"{pct}%"
        self.status_label.setText(f"Importing layer {current}/{total}: {layer_name} ({suffix})")
        if self._task is not None:
            # ``QgsTask.progress()`` returns the overall 0..100 percentage we
            # set inside the task; mirror it here for the inline progress bar.
            self.progress_bar.setValue(int(self._task.progress()))

    def _on_task_done(self):
        """Render the final summary once the task has completed or cancelled."""
        task = self._task
        self._task = None
        self._set_running(False)
        self.progress_bar.hide()

        if task is None:
            return

        summary = task.summary
        successes = [s for s in summary if s["ok"]]
        failures = [s for s in summary if not s["ok"]]
        cancelled = task.isCanceled()

        if cancelled:
            self._show_inline(
                f"Cancelled. {len(successes)} of {self._total_layers} imported.",
                error=False,
            )
        elif not failures:
            self._show_inline(f"All {len(successes)} layers imported.", error=False)
        else:
            failed_names = ", ".join(f["layerName"] for f in failures)
            self._show_inline(
                f"{len(successes)} of {self._total_layers} imported. Failed: {failed_names}",
                error=True,
            )
            # Tooltip carries the verbose per-layer error so the dialog
            # stays compact but the detail is still available on hover.
            self.status_label.setToolTip(
                "\n".join(f"{f['layerName']}: {f['error']}" for f in failures)
            )

    def _on_cancel_clicked(self):
        """Bottom-right button click. Cancels the task or closes the dialog."""
        if self._task_running and self._task is not None:
            self._task.cancel()
            self.status_label.setText("Cancelling...")
        else:
            self.reject()

    def closeEvent(self, event):
        """Intercept window-close (X button) the same way as Cancel.

        Without this, the user could close the window while the worker
        thread is still uploading, then have signals fire on a deleted
        widget. Cancelling first guarantees a clean teardown.
        """
        if self._task_running and self._task is not None:
            self._task.cancel()
            self.status_label.setText("Cancelling...")
            event.ignore()
        else:
            super().closeEvent(event)

    # ------------------------------------------------------------------
    # Helpers.
    # ------------------------------------------------------------------

    def _collect_selected_layers(self):
        """Return ``(layer, op)`` tuples for the currently checked rows,
        skipping any layers that have been removed from the project since
        the dialog opened."""
        project = QgsProject.instance()
        selected = []
        for row in range(self.layer_table.rowCount()):
            item = self.layer_table.item(row, 0)
            if item is None or item.checkState() != Qt.Checked:
                continue
            layer = project.mapLayer(item.data(Qt.UserRole))
            if layer is None:
                continue
            combo = self.layer_table.cellWidget(row, 1)
            op = combo.currentData() if combo is not None else "replace"
            selected.append((layer, op))
        return selected

    def _on_item_changed(self, item):
        """Keep each row's op combo enabled only when its layer is checked."""
        if item.column() != 0:
            return
        combo = self.layer_table.cellWidget(item.row(), 1)
        if combo is not None:
            combo.setEnabled(item.checkState() == Qt.Checked)

    def _update_token_info(self):
        """Refresh the muted line under the token field with project + expiry."""
        info = _decode_token_info(self.token_input.realText().strip())
        if info is None:
            if self.token_input.realText().strip():
                self.token_info.setText("Unable to read token info")
                self.token_info.show()
            else:
                self.token_info.clear()
                self.token_info.hide()
            return

        project = info["project_name"]
        days = info["expires_in_days"]
        if days > 0:
            tail = f"expires in {days} day" + ("" if days == 1 else "s")
        elif days == 0:
            tail = "expires today"
        else:
            ago = -days
            tail = f"expired {ago} day" + ("" if ago == 1 else "s") + " ago"
        self.token_info.setText(f"Project: {project} · {tail}")
        self.token_info.show()

    def _show_inline(self, message: str, error: bool = False):
        """Show ``message`` under the layer list, in red if ``error``."""
        color = "#c0392b" if error else "#333"
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setText(message)
        self.status_label.show()

    def _set_running(self, running: bool):
        """Toggle widgets between idle and busy states."""
        self._task_running = running
        self.layer_table.setEnabled(not running)
        self.token_input.setEnabled(not running)
        self.export_button.setEnabled(not running)
        self.cancel_button.setText("Cancel Export" if running else "Close")
        if running:
            self.progress_bar.show()
            self.status_label.setToolTip("")


def _is_supported(layer) -> bool:
    """Return ``True`` if ``layer`` is a vector layer with a geometry type
    we know how to export."""
    return (
        isinstance(layer, QgsVectorLayer)
        and layer.geometryType() in SUPPORTED_GEOMETRY_TYPES
    )


class MaskedTokenLineEdit(QLineEdit):
    """Line edit that hides its contents behind a fixed-width star mask.

    The mask is the same shape regardless of the underlying token's length,
    so the field reveals nothing about the token - not even its size.
    Clicking the field swaps the mask for the real value so it stays
    editable. ``realTextChanged`` fires whenever the stored value changes
    (initial set or after a focused edit commits) so consumers can refresh
    derived UI like a project/expiry hint.
    """

    MASKED_DISPLAY = "*" * 16

    realTextChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._real_text = ""

    def setRealText(self, text: str):
        self._real_text = text or ""
        self._render()
        self.realTextChanged.emit(self._real_text)

    def realText(self) -> str:
        # While focused, the visible text *is* the real text - the user may
        # be mid-edit, so trust the widget over the cached copy.
        if self.hasFocus():
            return super().text()
        return self._real_text

    def focusInEvent(self, event):
        super().setText(self._real_text)
        super().focusInEvent(event)
        # Place the cursor at the end so paste-over-select still works
        # naturally; selecting all here would be hostile to partial edits.
        self.end(False)

    def focusOutEvent(self, event):
        self._real_text = super().text()
        self._render()
        super().focusOutEvent(event)
        self.realTextChanged.emit(self._real_text)

    def _render(self):
        if self.hasFocus() or not self._real_text:
            super().setText(self._real_text)
        else:
            super().setText(self.MASKED_DISPLAY)


def _decode_token_info(token: str):
    """Best-effort decode of a JWT payload (no signature verification).

    Returns ``{"project_name": str, "expires_in_days": int}`` for a parseable
    token carrying both ``projectName`` and ``exp`` claims, or ``None`` for
    anything else. The same fallback covers every failure mode (not a JWT,
    bad base64, bad JSON, missing claims) - the UI doesn't distinguish them.
    """
    if not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        # JWT uses base64url with no padding; pad up to a multiple of 4.
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        project_name = payload["projectName"]
        exp = payload["exp"]
        expires_in_days = int((float(exp) - time.time()) // 86400)
        return {"project_name": str(project_name), "expires_in_days": expires_in_days}
    except Exception:
        return None
