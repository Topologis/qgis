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
)
from qgis.PyQt.QtCore import QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..compat import (
    QABSTRACT_NO_EDIT_TRIGGERS,
    QABSTRACT_NO_SELECTION,
    QDIALOG_ACCEPT_ROLE,
    QDIALOG_CANCEL,
    QHEADER_RESIZE_TO_CONTENTS,
    QHEADER_STRETCH,
    QPALETTE_WINDOW,
    QSTYLE_WARNING_ICON,
    QT_ALIGN_LEFT,
    QT_CHECKED,
    QT_ITEM_IS_ENABLED,
    QT_ITEM_IS_USER_CHECKABLE,
    QT_NO_ITEM_FLAGS,
    QT_UNCHECKED,
    QT_USER_ROLE,
    is_supported_vector_geometry_type,
)
from ..core.api import request_anonymous_session
from ..core.config import API_URL
from ..core.export_task import ExportTask

# Resource paths are resolved relative to the plugin root, which is the
# parent of this ``gui/`` package.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))
_HEADER_LIGHT_PATH = os.path.join(_PLUGIN_DIR, "resources", "icons", "qgis-header.png")
_HEADER_DARK_PATH = os.path.join(
    _PLUGIN_DIR, "resources", "icons", "qgis-header-dark.png"
)
# Last-resort fallback if the bundled headers are stripped (e.g. forks that
# remove the wordmark for trademark reasons) - the toolbar icon is generic.
_ICON_FALLBACK_PATH = os.path.join(_PLUGIN_DIR, "resources", "icons", "icon.png")

# Canonical docs page explaining how to generate an import token. Surfaced
# both next to the token field and inside the info box shown when the field
# is empty.
DOCS_TOKEN_URL = "https://topologis.com/docs/qgis-plugin/token"

# We can only export vector layers with simple Point/Line/Polygon geometry.
# The helper handles QGIS 3/4 enum-family differences.
UNSUPPORTED_WARNING = "layer doesn't contain Polygon/Line/Point geometry"

# QGIS persists the import token in QSettings between sessions so users
# don't have to paste it every time. Stored unencrypted - same trust model
# as other QGIS plugins keeping API keys here.
TOKEN_SETTINGS_KEY = "topologis/import_token"

# Whether to open the resulting view in a browser after a successful export.
# Stored separately so it survives across sessions independently of the token.
OPEN_IN_BROWSER_SETTINGS_KEY = "topologis/open_in_browser"

# Anonymous-session token + session id cached after a successful Preview
# mint, reused on subsequent Preview clicks until the JWT's ``exp`` has
# passed. Cleared on server rejection. The pasted-token (Export) flow
# never reads or writes these keys.
ANON_TOKEN_SETTINGS_KEY = "topologis/anonymous_token"
ANON_SESSION_SETTINGS_KEY = "topologis/anonymous_session"

# Treat a cached token as expired if ``exp`` is within this many seconds
# of now, so we don't hand the server a token that expires mid-request.
_ANON_TOKEN_EXP_SKEW_SECONDS = 30

# Copy shown in the info box under the (empty) token field. Two variants:
# one for the first Preview (no cached session yet) and one for follow-up
# Previews where a cached anonymous session is still valid. Edit freely;
# HTML is supported (the label has ``setOpenExternalLinks(True)``).
PREVIEW_INFO_NO_CACHE = (
    "We'll create an anonymous account for you.&nbsp;To keep your data and access it later,&nbsp;sign up and "
    f'<a href="{DOCS_TOKEN_URL}">get a token</a>.'
)
PREVIEW_INFO_CACHED = (
    "Your data will be imported into your anonymous account.&nbsp;To keep your data and access it later,&nbsp;sign up and "
    f'<a href="{DOCS_TOKEN_URL}">get a token</a>.'
)


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

        # Token actually driving the current export. For pasted-token runs
        # this mirrors the field's value; for Preview runs it's the
        # anonymous-session token, which is never written to the field or
        # to QSettings. Cleared in ``_on_task_done`` so the next Preview
        # click always mints a fresh session.
        self._active_token = None
        # Anonymous-session identifier paired with ``_active_token``. Only
        # set for Preview runs (the server returns it alongside the token);
        # appended to the preview URL as ``?session=...`` after a successful
        # export so the view can find the right anonymous bucket.
        self._active_session = None
        # True when the in-flight ExportTask is running with a token loaded
        # from the QSettings anonymous-session cache. Lets ``_on_task_done``
        # decide whether a ``fatal_error`` means we should invalidate the
        # cache and auto-retry with a freshly minted session.
        self._used_cached_anonymous = False

        layout = QVBoxLayout(self)

        # ---- Branding header ------------------------------------------------
        # Pick a header variant matching the active QGIS theme so the wordmark
        # stays legible against both light and dark backgrounds.
        logo_label = QLabel(self)
        logo_label.setPixmap(
            QPixmap(_pick_header_path(self.palette())).scaledToHeight(32)
        )
        logo_label.setAlignment(QT_ALIGN_LEFT)
        logo_label.setContentsMargins(0, 0, 0, 10)
        layout.addWidget(logo_label)

        # ---- Layer list -----------------------------------------------------
        layout.addWidget(QLabel("Layers"))
        self.layer_table = QTableWidget(self)
        self.layer_table.setColumnCount(2)
        self.layer_table.setHorizontalHeaderLabels(["Layer", "Operation"])
        self.layer_table.verticalHeader().setVisible(False)
        self.layer_table.horizontalHeader().setSectionResizeMode(0, QHEADER_STRETCH)
        self.layer_table.horizontalHeader().setSectionResizeMode(
            1, QHEADER_RESIZE_TO_CONTENTS
        )
        self.layer_table.setEditTriggers(QABSTRACT_NO_EDIT_TRIGGERS)
        self.layer_table.setSelectionMode(QABSTRACT_NO_SELECTION)
        self.layer_table.setShowGrid(False)
        warning_icon = QApplication.style().standardIcon(QSTYLE_WARNING_ICON)

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
            item.setData(QT_USER_ROLE, layer.id())
            if _is_supported(layer):
                item.setFlags(QT_ITEM_IS_USER_CHECKABLE | QT_ITEM_IS_ENABLED)
                item.setCheckState(QT_UNCHECKED)
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
                item.setFlags(QT_NO_ITEM_FLAGS)
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

        # Shown only while the token field is empty - explains that the
        # Preview path produces a temporary export and links to the docs
        # for users who want a permanent project token instead. The text
        # itself is swapped by ``_sync_token_dependent_ui`` based on whether
        # a cached anonymous session is available; the constant here is
        # just the initial placeholder before that runs.
        self.token_info_box = QLabel(PREVIEW_INFO_NO_CACHE, self)
        self.token_info_box.setOpenExternalLinks(True)
        self.token_info_box.setWordWrap(True)
        self.token_info_box.setStyleSheet(
            "background: #fff8e1; border: 1px solid #e0c97f; "
            "padding: 8px; border-radius: 4px; color: #5a4a00;"
        )
        layout.addWidget(self.token_info_box)

        # Keep the project/expiry line in sync with the field's real value.
        self.token_input.realTextChanged.connect(self._update_token_info)
        # Pre-fill with the token from a previous session if there is one.
        self.token_input.setRealText(
            QgsSettings().value(TOKEN_SETTINGS_KEY, "", type=str)
        )

        # ---- Status / progress ---------------------------------------------
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        # ---- Bottom row -----------------------------------------------------
        # Checkbox on the left, buttons on the right. The button box still
        # flips between "Close" (idle) and "Cancel Export" (running) to avoid
        # a layout shift mid-flow; the action button's label is driven by
        # token presence (see ``_sync_token_dependent_ui``).
        bottom_row = QHBoxLayout()

        self.open_in_browser_checkbox = QCheckBox("Open map in browser", self)
        # Default True so the no-account path lands on the published view
        # automatically the first time someone tries the plugin.
        checked_default = QgsSettings().value(
            OPEN_IN_BROWSER_SETTINGS_KEY, True, type=bool
        )
        self.open_in_browser_checkbox.setChecked(checked_default)
        # Persist on every toggle so closing the dialog without exporting
        # still remembers the preference.
        self.open_in_browser_checkbox.toggled.connect(
            lambda checked: QgsSettings().setValue(
                OPEN_IN_BROWSER_SETTINGS_KEY, bool(checked)
            )
        )
        bottom_row.addWidget(self.open_in_browser_checkbox)
        bottom_row.addStretch()

        self.buttons = QDialogButtonBox(QDIALOG_CANCEL, parent=self)
        self.cancel_button = self.buttons.button(QDIALOG_CANCEL)
        self.cancel_button.setText("Close")
        self.action_button = self.buttons.addButton("Export", QDIALOG_ACCEPT_ROLE)
        self.buttons.rejected.connect(self._on_cancel_clicked)
        self.action_button.clicked.connect(self._on_action)
        bottom_row.addWidget(self.buttons)

        layout.addLayout(bottom_row)

        # Drive the action button's label and the info box's visibility off
        # the live token value. ``textChanged`` covers in-progress typing;
        # ``realTextChanged`` covers programmatic ``setRealText`` calls and
        # focus-out commits.
        self.token_input.textChanged.connect(self._sync_token_dependent_ui)
        self.token_input.realTextChanged.connect(self._sync_token_dependent_ui)
        self._sync_token_dependent_ui()

    # ------------------------------------------------------------------
    # Event handlers.
    # ------------------------------------------------------------------

    def _on_action(self):
        """Validate inputs, source a token if needed, and start the export.

        Single entry point for the dialog's primary button. The button's
        label is "Export" when the user has pasted a token and "Preview"
        when the field is empty; the empty-field branch mints a fresh
        anonymous-session token and keeps it on ``self._active_token`` only
        - it's never written to the field or persisted.
        """
        layers = self._collect_selected_layers()
        if not layers:
            self._show_inline("Select at least one layer.", error=True)
            return

        # Reset the cache-hit flag for this attempt; only the cached-Preview
        # branch flips it to True. Pasted-token runs and freshly-minted
        # Preview runs both leave it False so ``_on_task_done`` won't try
        # to auto-refresh.
        self._used_cached_anonymous = False

        pasted = self.token_input.realText().strip()
        active_session = None
        if pasted:
            # Export mode: use the pasted token and persist it for next run.
            # The anonymous-session cache is intentionally not consulted or
            # touched here - a pasted token wins outright.
            QgsSettings().setValue(TOKEN_SETTINGS_KEY, pasted)
            active_token = pasted
        else:
            # Preview mode: try the QSettings cache first; on a miss
            # (expired/absent) fall back to a fresh /anonymous-session mint
            # and persist the result for the next click.
            cached_token, cached_session = _load_cached_anonymous_session()
            if cached_token and cached_session:
                active_token = cached_token
                active_session = cached_session
                self._used_cached_anonymous = True
            else:
                # No cache (or expired): GET a one-off session token. Block
                # the buttons for the duration so the dialog can't be
                # re-entered while the network round-trip is in flight.
                self._set_buttons_enabled(False)
                self._show_inline("Starting anonymous session...")
                # Force the disabled state + status message to repaint before
                # the synchronous GET blocks the UI thread.
                QApplication.processEvents()
                active_token, active_session, error = request_anonymous_session()
                self._set_buttons_enabled(True)
                if error:
                    self._show_inline(error, error=True)
                    return
                _store_cached_anonymous_session(active_token, active_session or "")
                # Cache just gained a fresh entry; refresh the info-box copy
                # so the swap is visible if the user returns to this dialog.
                self._sync_token_dependent_ui()

        self._active_token = active_token
        self._active_session = active_session

        self._set_running(True)
        self._total_layers = len(layers)
        self.progress_bar.setValue(0)
        self._show_inline(f"Importing layer 1/{len(layers)}...")

        # Hand the task to QGIS's task manager so it shows up in the global
        # task panel and runs on a worker thread.
        self._task = ExportTask(active_token, layers)
        self._task.progressUpdated.connect(self._on_progress)
        self._task.done.connect(self._on_task_done)
        QgsApplication.taskManager().addTask(self._task)

    def _on_progress(
        self, current: int, total: int, layer_name: str, pct: int, phase: str
    ):
        """Render an inline status string for the current layer's progress."""
        suffix = "preparing..." if phase == "preparing" else f"{pct}%"
        self.status_label.setText(
            f"Importing layer {current}/{total}: {layer_name} ({suffix})"
        )
        if self._task is not None:
            # ``QgsTask.progress()`` returns the overall 0..100 percentage we
            # set inside the task; mirror it here for the inline progress bar.
            self.progress_bar.setValue(int(self._task.progress()))

    def _on_task_done(self):
        """Render the final summary once the task has completed or cancelled."""
        task = self._task
        self._task = None
        active_token = self._active_token
        active_session = self._active_session
        used_cached = self._used_cached_anonymous
        # Always clear the active token/session so the next Preview click
        # mints fresh values and a follow-up Export doesn't reuse stale state.
        # The QSettings cache is independent and only cleared on the
        # rejection path below.
        self._active_token = None
        self._active_session = None
        self._used_cached_anonymous = False
        self._set_running(False)
        self.progress_bar.hide()

        if task is None:
            return

        # Auto-refresh on cached-token rejection: if this run used a token
        # from the QSettings anonymous cache and the server returned a
        # ``fatal_error`` (token revoked / expired server-side / any blocking
        # response from qgis-get-urls), drop the cache and transparently
        # re-run with a freshly minted session. Gated by ``used_cached`` so
        # we retry at most once per click - the re-entered ``_on_action``
        # sees an empty cache and runs the fresh-mint branch, which leaves
        # ``_used_cached_anonymous`` False on its way back here.
        if used_cached and task.fatal_error and not task.isCanceled():
            _clear_cached_anonymous_session()
            # Cache just dropped; let the info-box copy reflect that before
            # we re-enter ``_on_action`` (which will mint a fresh session).
            self._sync_token_dependent_ui()
            self._show_inline("Refreshing anonymous session...")
            QApplication.processEvents()
            self._on_action()
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

        # If the user opted in and at least one layer landed, open the view
        # in a browser. ``viewId`` is decoded from the token used for this
        # run, so both pasted-token and Preview flows share one code path;
        # the ``?session=...`` query param is appended only for Preview
        # runs, where the server needs it to find the anonymous bucket.
        if (
            self.open_in_browser_checkbox.isChecked()
            and not cancelled
            and successes
            and active_token
        ):
            view_id = _decode_view_id(active_token)
            if view_id:
                url = f"{API_URL}/view/{view_id}"
                if active_session:
                    # Server returns a URL-safe value (already percent-encoded
                    # where needed) - append as-is to avoid double-encoding.
                    url += f"?session={active_session}"
                QDesktopServices.openUrl(QUrl(url))

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
            if item is None or item.checkState() != QT_CHECKED:
                continue
            layer = project.mapLayer(item.data(QT_USER_ROLE))
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
            combo.setEnabled(item.checkState() == QT_CHECKED)

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
        self.action_button.setEnabled(not running)
        self.cancel_button.setText("Cancel Export" if running else "Close")
        if running:
            self.progress_bar.show()
            self.status_label.setToolTip("")

    def _set_buttons_enabled(self, enabled: bool):
        """Enable or disable the dialog's primary buttons together.

        Used to lock the UI for the short anonymous-session round-trip when
        no task has been created yet - ``_set_running`` would be misleading
        there because the export task hasn't started.
        """
        self.action_button.setEnabled(enabled)
        self.cancel_button.setEnabled(enabled)

    def _sync_token_dependent_ui(self, *_):
        """Flip the action button label and toggle the info box based on
        whether the token field currently holds anything. When the field is
        empty, also pick the info-box copy variant that matches the current
        cache state (cached preview available vs. fresh mint required).

        Accepts and ignores positional args so the same slot can be wired
        to both ``textChanged(str)`` and ``realTextChanged(str)``.
        """
        has_token = bool(self.token_input.realText().strip())
        self.action_button.setText("Export" if has_token else "Preview")
        self.token_info_box.setVisible(not has_token)
        if not has_token:
            cached_token, cached_session = _load_cached_anonymous_session()
            if cached_token and cached_session:
                self.token_info_box.setText(PREVIEW_INFO_CACHED)
            else:
                self.token_info_box.setText(PREVIEW_INFO_NO_CACHE)


def _pick_header_path(palette) -> str:
    """Return the header asset best matching the current Qt palette.

    QGIS doesn't expose its theme directly, so we sniff the window-background
    luminance (Rec. 601 luma weights, 0..255 scale): below 128 we treat the
    palette as dark. Each candidate is checked for existence so a stripped
    install gracefully degrades to the toolbar icon.
    """
    bg = palette.color(QPALETTE_WINDOW)
    luminance = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    preferred = _HEADER_DARK_PATH if luminance < 128 else _HEADER_LIGHT_PATH
    for path in (preferred, _HEADER_LIGHT_PATH, _ICON_FALLBACK_PATH):
        if os.path.exists(path):
            return path
    return _ICON_FALLBACK_PATH


def _is_supported(layer) -> bool:
    """Return ``True`` if ``layer`` is a vector layer with a geometry type
    we know how to export."""
    return isinstance(layer, QgsVectorLayer) and is_supported_vector_geometry_type(
        layer.geometryType()
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


def _decode_jwt_payload(token: str):
    """Best-effort decode of a JWT payload (no signature verification).

    Returns the parsed payload dict, or ``None`` for anything we can't read
    (not a JWT, bad base64, bad JSON). Every failure mode collapses to the
    same fallback - readers handle missing claims themselves.
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
        return json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except Exception:
        return None


def _decode_token_info(token: str):
    """Pull project + expiry out of a token's payload for the muted hint
    below the token field. ``None`` when either claim is missing."""
    payload = _decode_jwt_payload(token)
    if payload is None:
        return None
    try:
        project_name = payload["projectName"]
        exp = payload["exp"]
        expires_in_days = int((float(exp) - time.time()) // 86400)
        return {"project_name": str(project_name), "expires_in_days": expires_in_days}
    except (KeyError, ValueError, TypeError):
        return None


def _decode_view_id(token: str):
    """Pull the ``viewId`` claim out of a token, or ``None`` if absent."""
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    view_id = payload.get("viewId")
    return str(view_id) if view_id else None


def _load_cached_anonymous_session():
    """Return ``(token, session)`` from QSettings if both are present and
    the JWT ``exp`` is still comfortably in the future. Any miss - empty
    keys, partial cache, unreadable token, missing/past ``exp`` - returns
    ``(None, None)`` so the caller mints a fresh session."""
    settings = QgsSettings()
    token = settings.value(ANON_TOKEN_SETTINGS_KEY, "", type=str)
    session = settings.value(ANON_SESSION_SETTINGS_KEY, "", type=str)
    if not token or not session:
        return None, None
    payload = _decode_jwt_payload(token)
    if not payload:
        return None, None
    try:
        exp = float(payload["exp"])
    except (KeyError, ValueError, TypeError):
        return None, None
    if exp - time.time() < _ANON_TOKEN_EXP_SKEW_SECONDS:
        return None, None
    return token, session


def _store_cached_anonymous_session(token: str, session: str):
    """Persist a freshly minted ``(token, session)`` pair so the next
    Preview click can skip the ``/anonymous-session`` round-trip."""
    settings = QgsSettings()
    settings.setValue(ANON_TOKEN_SETTINGS_KEY, token or "")
    settings.setValue(ANON_SESSION_SETTINGS_KEY, session or "")


def _clear_cached_anonymous_session():
    """Drop the cached anonymous session - called when the server
    rejects a previously cached token."""
    settings = QgsSettings()
    settings.remove(ANON_TOKEN_SETTINGS_KEY)
    settings.remove(ANON_SESSION_SETTINGS_KEY)
