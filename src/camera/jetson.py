"""OAK RGB/depth acquisition, YOLO26m GPU detection and BoT-SORT target tracking."""

from datetime import timedelta
import logging
import os
from pathlib import Path
import threading
import time

import cv2
import depthai as dai
import numpy as np

from config import inference_settings, settings
from .detection import Detection
from .runtime import PerceptionCamera, capture_time
from .spatial import spatial_position

logger = logging.getLogger(__name__)


def create_pipeline():
    pipeline = dai.Pipeline()
    rgb = pipeline.create(dai.node.ColorCamera)
    rgb.setBoardSocket(dai.CameraBoardSocket.CAM_A)
    rgb.setResolution(dai.ColorCameraProperties.SensorResolution.THE_1080_P)
    rgb.setIspScale(1, 3)  # Full 16:9 field of view, 640x360, no square preview crop.
    rgb.setFps(30)
    stereo = pipeline.create(dai.node.StereoDepth)
    stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.HIGH_DENSITY)
    stereo.setLeftRightCheck(True)
    stereo.setSubpixel(True)
    stereo.setDepthAlign(dai.CameraBoardSocket.CAM_A)
    stereo.setOutputSize(640, 360)
    for socket, destination in ((dai.CameraBoardSocket.CAM_B, stereo.left),
                                (dai.CameraBoardSocket.CAM_C, stereo.right)):
        mono = pipeline.create(dai.node.MonoCamera)
        mono.setBoardSocket(socket)
        mono.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
        mono.setFps(30)
        mono.out.link(destination)
    sync = pipeline.create(dai.node.Sync)
    sync.setSyncThreshold(timedelta(milliseconds=15))
    sync.setSyncAttempts(-1)  # Never emit an unsynchronized pair.
    rgb.isp.link(sync.inputs['rgb'])
    stereo.depth.link(sync.inputs['depth'])
    output = pipeline.create(dai.node.XLinkOut)
    output.setStreamName('frames')
    output.input.setBlocking(False)
    output.input.setQueueSize(1)
    sync.out.link(output.input)
    return pipeline


class TargetLock:
    """Keep the selected ID through short occlusions; use only measured positions."""
    def __init__(self, timeout):
        self.timeout = timeout
        self.track_id = None
        self.last_seen = None
        self.waiting = False

    def update(self, detections, now):
        if self.track_id is not None:
            match = next((d for d in detections if d.track_id == self.track_id), None)
            if match is not None:
                self.last_seen, self.waiting = now, False
                return match
            if now - self.last_seen < self.timeout:
                self.waiting = True
                return None
            logger.info('Target lost: track_id=%s', self.track_id)
            self.track_id = None
        self.waiting = False
        if not detections:
            return None
        match = min(detections, key=lambda d: d.z)
        self.track_id, self.last_seen = match.track_id, now
        logger.info('Target acquired: track_id=%s distance=%.2fm', self.track_id, match.z)
        return match


def create_tracker():
    # Imports stay optional for Luxonis mode. BoT-SORT runs on CPU; inference
    # uses TensorRT CUDA directly and does not require a CUDA PyTorch wheel.
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    from ultralytics.trackers.bot_sort import BOTSORT
    from ultralytics.utils import YAML, IterableSimpleNamespace
    config = YAML.load(Path(__file__).with_name('botsort.yaml'))
    config['new_track_thresh'] = max(config['track_high_thresh'], settings.detection_thresh)
    return BOTSORT(IterableSimpleNamespace(**config))


class JetsonCamera(PerceptionCamera):
    @staticmethod
    def previewSize():
        return (640, 360)

    def __init__(self, callback=None, config=inference_settings):
        super().__init__(callback, config)
        self._thread = None
        self._stop = threading.Event()
        self._target = TargetLock(config.target_lost_timeout)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError('Camera is already running')
        self._stop.clear()
        self.ready_event.clear()
        self.stopped_event.clear()
        self.error = None
        self._snapshot = ([], None, None, False)
        self._target = TargetLock(self.config.target_lost_timeout)
        self._thread = threading.Thread(target=self._run, name='jetson-perception', daemon=True)
        self._thread.start()

    def running(self):
        return not self._stop.is_set() and self._fresh()

    def closestDetection(self):
        snapshot = self._snapshot
        return snapshot[2] if not self._stop.is_set() and self._fresh(snapshot) else None

    def tracking_hold(self):
        return self._snapshot[3]

    def stop(self):
        self._stop.set()
        self.ready_event.clear()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(5)
            if self._thread.is_alive():
                logger.warning('Jetson camera thread did not stop within five seconds')

    def _run(self):
        detector = None
        try:
            from .tensorrt_yolo import TensorRTYolo
            detector = TensorRTYolo(self.config.jetson_model_path)
            tracker = create_tracker()
            from ultralytics.engine.results import Boxes
            if self._stop.is_set():
                return
            with dai.Device(create_pipeline()) as device:
                logger.info('OAK acquisition ready: USB=%s RGB=640x360 depth=RGB-aligned inference=Jetson',
                            device.getUsbSpeed())
                intrinsics = np.asarray(device.readCalibration().getCameraIntrinsics(
                    dai.CameraBoardSocket.CAM_A, 640, 360), dtype=np.float64)
                if not np.isfinite(intrinsics).all() or min(intrinsics[0, 0], intrinsics[1, 1]) <= 0:
                    raise ValueError('Invalid RGB camera calibration')
                queue = device.getOutputQueue('frames', maxSize=1, blocking=False)
                last_capture = None
                report_time, count = time.monotonic(), 0
                while not self._stop.is_set():
                    group = queue.tryGet()
                    if group is None:
                        self._stop.wait(0.005)
                        continue
                    captured_at = min(capture_time(group['rgb'], dai), capture_time(group['depth'], dai))
                    if last_capture is not None and captured_at <= last_capture:
                        continue
                    if time.monotonic() - captured_at > self.config.max_age:
                        continue
                    frame = group['rgb'].getCvFrame()
                    depth = group['depth'].getFrame()
                    if frame.shape[:2] != depth.shape or frame.shape[:2] != (360, 640):
                        raise ValueError('RGB and aligned depth dimensions do not match')
                    if last_capture is not None and captured_at - last_capture > self.config.target_lost_timeout:
                        tracker.reset()
                        self._target = TargetLock(self.config.target_lost_timeout)
                    rows = detector.infer(frame)
                    # Low-confidence person detections aid track association; the
                    # flight-facing confidence threshold is applied below.
                    tracks = tracker.update(Boxes(rows, frame.shape[:2]), frame)
                    fps = 0 if last_capture is None else 1 / max(captured_at - last_capture, 1e-6)
                    last_capture = captured_at
                    detections = []
                    for track in tracks:
                        # Use this frame's measured detector box for depth, rather
                        # than the Kalman-smoothed box returned by the tracker.
                        box = rows[int(track[7]), :4]
                        track_id, confidence = int(track[4]), float(track[5])
                        if confidence < settings.detection_thresh:
                            continue
                        position = spatial_position(box, depth, intrinsics)
                        if position is None:
                            continue
                        detection = Detection(*position, confidence, fps, track_id=track_id)
                        detections.append(detection)
                        if self._userCallback is not None:
                            x1, y1, x2, y2 = map(int, box)
                            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 1)
                            cv2.putText(frame, f'ID {track_id} {position[2]:.2f}m', (x1, max(12, y1)),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
                    selected = self._target.update(detections, captured_at)
                    self._publish(detections, frame, captured_at,
                                  selected=selected, holding=self._target.waiting)
                    count += 1
                    now = time.monotonic()
                    if now - report_time >= 5:
                        logger.info('Jetson perception: fps=%.1f capture_to_result_ms=%.1f target_id=%s fresh=%s',
                                    count / (now - report_time), (now - captured_at) * 1000,
                                    self._target.track_id, self._fresh())
                        report_time, count = now, 0
        except Exception as error:
            self.error = error
            logger.exception('Jetson perception failed; no inference fallback')
        finally:
            self.ready_event.clear()
            self.stopped_event.set()
            if detector is not None:
                detector.close()
