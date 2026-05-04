"""Topologis QGIS plugin entry point.

QGIS discovers plugins by importing the package and calling ``classFactory``.
This function is the only API contract between QGIS and the plugin, so we keep
it minimal and defer the real imports until it is actually called - that way a
broken sub-module does not stop the plugin from being listed in the manager.
"""


def classFactory(iface):
    """Instantiate the plugin.

    Args:
        iface: The QGIS ``QgisInterface`` handle, providing access to the main
            window, menus, toolbars and the active project.

    Returns:
        An instance of :class:`TopologisPlugin`.
    """
    from .topologis_plugin import TopologisPlugin
    return TopologisPlugin(iface)
