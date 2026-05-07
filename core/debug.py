"""Debug logging helpers for development builds."""

from qgis.core import QgsMessageLog

from ..compat import QGIS_INFO_LEVEL
from .config import DEBUG


LOG_TAG = "Topologis"


def debug_log(message: str):
    """Write a debug message to the QGIS log when ``TOPOLOGIS_DEBUG`` is set."""
    if not DEBUG:
        return
    QgsMessageLog.logMessage(str(message), LOG_TAG, QGIS_INFO_LEVEL)
