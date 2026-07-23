import collections
import collections.abc


def apply_dronekit_compatibility():
    """Patch collection aliases removed from newer Python versions."""
    if not hasattr(collections, "MutableMapping"):
        collections.MutableMapping = collections.abc.MutableMapping
