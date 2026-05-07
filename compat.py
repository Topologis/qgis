"""Compatibility helpers for QGIS 3.20 through QGIS 4.x.

QGIS 4 moves to Qt 6/PyQt6, which requires scoped enum names and relocates a
few Qt classes. Keep those spelling differences here so plugin code can stay
focused on behavior instead of runtime-version branching.
"""

from qgis.core import Qgis, QgsTask, QgsVectorFileWriter, QgsWkbTypes
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QHeaderView,
    QStyle,
)

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction


def _resolve_enum(owner, scoped_path: str, legacy_name: str):
    """Return a Qt6-style scoped enum, falling back to Qt5 legacy spelling."""
    current = owner
    try:
        for part in scoped_path.split("."):
            current = getattr(current, part)
        return current
    except AttributeError:
        return getattr(owner, legacy_name)


def _enum_value(value):
    """Return a comparable integer value for PyQt/PyQGIS enum variants."""
    try:
        return int(value)
    except (AttributeError, TypeError):
        return int(value.value)


def exec_dialog(dialog):
    """Execute a dialog using the available Qt5/Qt6 method name."""
    exec_method = getattr(dialog, "exec", None)
    if exec_method is not None:
        return exec_method()
    return getattr(dialog, "exec_")()


QT_ALIGN_LEFT = _resolve_enum(Qt, "AlignmentFlag.AlignLeft", "AlignLeft")
QT_USER_ROLE = _resolve_enum(Qt, "ItemDataRole.UserRole", "UserRole")
QT_CHECKED = _resolve_enum(Qt, "CheckState.Checked", "Checked")
QT_UNCHECKED = _resolve_enum(Qt, "CheckState.Unchecked", "Unchecked")
QT_NO_ITEM_FLAGS = _resolve_enum(Qt, "ItemFlag.NoItemFlags", "NoItemFlags")
QT_ITEM_IS_USER_CHECKABLE = _resolve_enum(
    Qt, "ItemFlag.ItemIsUserCheckable", "ItemIsUserCheckable"
)
QT_ITEM_IS_ENABLED = _resolve_enum(Qt, "ItemFlag.ItemIsEnabled", "ItemIsEnabled")

QHEADER_STRETCH = _resolve_enum(QHeaderView, "ResizeMode.Stretch", "Stretch")
QHEADER_RESIZE_TO_CONTENTS = _resolve_enum(
    QHeaderView, "ResizeMode.ResizeToContents", "ResizeToContents"
)
QABSTRACT_NO_EDIT_TRIGGERS = _resolve_enum(
    QAbstractItemView, "EditTrigger.NoEditTriggers", "NoEditTriggers"
)
QABSTRACT_NO_SELECTION = _resolve_enum(
    QAbstractItemView, "SelectionMode.NoSelection", "NoSelection"
)
QSTYLE_WARNING_ICON = _resolve_enum(
    QStyle, "StandardPixmap.SP_MessageBoxWarning", "SP_MessageBoxWarning"
)
QDIALOG_CANCEL = _resolve_enum(QDialogButtonBox, "StandardButton.Cancel", "Cancel")
QDIALOG_ACCEPT_ROLE = _resolve_enum(
    QDialogButtonBox, "ButtonRole.AcceptRole", "AcceptRole"
)

QGIS_INFO_LEVEL = _resolve_enum(Qgis, "MessageLevel.Info", "Info")
QGSTASK_CAN_CANCEL = _resolve_enum(QgsTask, "Flag.CanCancel", "CanCancel")
QGSVECTOR_WRITER_NO_ERROR = _resolve_enum(
    QgsVectorFileWriter, "WriterError.NoError", "NoError"
)


def _qgis_geometry_type(name: str):
    try:
        from qgis.core import Qgis
    except ImportError:
        return None

    geometry_type = getattr(Qgis, "GeometryType", None)
    if geometry_type is None:
        return None
    return getattr(geometry_type, name, None)


def _wkb_geometry_type(legacy_name: str):
    scoped = getattr(QgsWkbTypes, "GeometryType", None)
    if scoped is not None and hasattr(scoped, legacy_name):
        return getattr(scoped, legacy_name)
    return getattr(QgsWkbTypes, legacy_name)


def _geometry_type(name: str, legacy_name: str):
    return _qgis_geometry_type(name) or _wkb_geometry_type(legacy_name)


SUPPORTED_VECTOR_GEOMETRY_TYPES = {
    _geometry_type("Point", "PointGeometry"),
    _geometry_type("Line", "LineGeometry"),
    _geometry_type("Polygon", "PolygonGeometry"),
}
SUPPORTED_VECTOR_GEOMETRY_TYPE_VALUES = {
    _enum_value(geometry_type) for geometry_type in SUPPORTED_VECTOR_GEOMETRY_TYPES
}


def is_supported_vector_geometry_type(geometry_type) -> bool:
    """Return whether a QGIS 3/4 geometry type is point, line, or polygon."""
    return (
        geometry_type in SUPPORTED_VECTOR_GEOMETRY_TYPES
        or _enum_value(geometry_type) in SUPPORTED_VECTOR_GEOMETRY_TYPE_VALUES
    )
