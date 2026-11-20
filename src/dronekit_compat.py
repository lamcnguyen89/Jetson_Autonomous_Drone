"""Compatibility for the released DroneKit package on Python 3.10+."""

import collections
import collections.abc


def apply_dronekit_compatibility():
    """Restore the collection alias DroneKit 2.9.2 uses during import."""
    # Call before importing DroneKit: the released package refers to the old
    # alias while defining its classes. Repeating the shim is harmless.
    if not hasattr(collections, "MutableMapping"):
        collections.MutableMapping = collections.abc.MutableMapping
