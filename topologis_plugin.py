"""Main plugin class wired into the QGIS GUI.

The class is intentionally thin: it registers a single ``QAction`` (menu entry
plus toolbar icon) and opens the export dialog when triggered. All real work -
GeoJSON conversion, network calls, progress reporting - lives in
:mod:`core.export_task` and :mod:`gui.export_dialog`.
"""

import os

from qgis.PyQt.QtGui import QIcon

from .compat import QAction, exec_dialog
from .gui.export_dialog import TopologisExportDialog


# Label shown as a submenu under Web -> &Topologis Exporter. The leading ``&``
# sets the keyboard accelerator; QGIS strips it from the visible text.
MENU_LABEL = "&Topologis Exporter"
ACTION_TEXT = "Publish to Topologis…"
ICON_RELATIVE_PATH = os.path.join("resources", "icons", "icon.png")


class TopologisPlugin:
    """Lifecycle wrapper expected by QGIS for every Python plugin."""

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        # Resolve the plugin directory once so paths to bundled resources
        # (icons, logo) stay correct regardless of QGIS's current working dir.
        self.plugin_dir = os.path.dirname(__file__)

    def initGui(self):
        """Called by QGIS when the plugin is loaded - register UI hooks."""
        icon = QIcon(os.path.join(self.plugin_dir, ICON_RELATIVE_PATH))

        self.action = QAction(icon, ACTION_TEXT, self.iface.mainWindow())
        self.action.triggered.connect(self.run)

        self.iface.addPluginToWebMenu(MENU_LABEL, self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        """Called by QGIS when the plugin is disabled.

        We undo everything :meth:`initGui` did so re-enabling the plugin does
        not duplicate menu entries or leave stale icons in the toolbar.
        """
        if self.action:
            self.iface.removePluginWebMenu(MENU_LABEL, self.action)
            self.iface.removeToolBarIcon(self.action)

    def run(self):
        """Open the modal export dialog. Invoked from the toolbar/menu."""
        dlg = TopologisExportDialog(self.iface.mainWindow())
        exec_dialog(dlg)
