"""Shared publication and freshness rules for real camera backends."""

import threading
import time

from .base import BaseCamera
from config import inference_settings


class PerceptionCamera(BaseCamera):
    """Publish camera output to the control thread through a single batch snapshot.

    A worker produces detections independently of Core's faster control loop.
    Readiness, health and capture age are shared here so both real backends
    apply the same rules before their positions can drive movement.
    """
    def __init__(self, callback=None, config=inference_settings):
        self.config = config
        self.ready_event = threading.Event()
        self.stopped_event = threading.Event()
        self.error = None
        # Tuple fields: all detections, capture time, selected detection, hold flag.
        # The last two fields are used by the Jetson target-lock policy.
        self._snapshot = ([], None, None, False)
        self._userCallback = callback

    def _fresh(self, snapshot=None):
        # A live worker alone is insufficient: USB or GPU work may have stalled.
        # Capture time includes time spent in transport and inference; receipt
        # time would make an old frame appear current whenever it finally arrived.
        captured_at = (self._snapshot if snapshot is None else snapshot)[1]
        return (captured_at is not None and self.error is None
                and not self.stopped_event.is_set()
                and 0 <= time.monotonic() - captured_at <= self.config.max_age)

    def _publish(self, detections, frame, captured_at=None, *, selected=None, holding=False):
        # Publish position, timestamp and target-lock state together. Readers
        # must never see a new timestamp attached to a previous target position.
        self._snapshot = (list(detections), time.monotonic() if captured_at is None else captured_at,
                          selected, holding)
        if not self._fresh():
            self.ready_event.clear()
            return
        # An empty, current batch proves the pipeline is healthy.
        self.ready_event.set()
        if self._userCallback is not None:
            self._userCallback(detections, frame)

    def detections(self):
        # Read one snapshot reference so its detections and timestamp stay paired.
        snapshot = self._snapshot
        return list(snapshot[0]) if self._fresh(snapshot) else []


def capture_time(packet, dai):
    """Translate DepthAI's host-synchronized clock to Python monotonic time."""
    # Compare ages within DepthAI's clock first, then subtract that age from
    # Python's clock. Neither clock needs the same epoch or wall-clock timezone.
    return time.monotonic() - (dai.Clock.now() - packet.getTimestamp()).total_seconds()
