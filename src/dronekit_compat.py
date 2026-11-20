"""Compatibility for the released DroneKit package on Python 3.10+."""

import collections
import collections.abc


def apply_dronekit_compatibility():
    """Restore the collection alias DroneKit 2.9.2 uses during import."""
    if not hasattr(collections, "MutableMapping"):
        collections.MutableMapping = collections.abc.MutableMapping
