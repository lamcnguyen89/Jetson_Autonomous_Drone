"""Luxonis adapter: TinyYOLO v4 inference and spatial measurement run on the OAK.

The host builds a DepthAI graph, deploys it with dai.Device, then consumes
spatial detections from USB queues. Host OpenCV handles optional overlays/video;
it does not run the neural network. The shared PerceptionCamera layer supplies
the same freshness and position API used by the Jetson backend.
"""

from .detection import Detection
from .runtime import PerceptionCamera, capture_time

from pathlib import Path
import depthai as dai
import numpy as np
import time
import cv2
import threading
import logging

import config as flight_config
from config import inference_settings

# Legacy lifecycle flags assume a single Luxonis camera worker per process.
# Factory selection ensures the service creates only one active camera backend.
THREAD_STOP = False
# Retained default path; setup() uses config.luxonis_model_path for selection.
BLOB_PATH   = str((Path(__file__).parent / Path('../../models/yolo-v4-tiny-tf_openvino_2021.4_6shave.blob')).resolve().absolute())
RUNNING     = False
logger = logging.getLogger(__name__)


def _get_packet(queue):
    """Fetch a queued device packet while checking the worker's shutdown signal."""
    # Poll so startup cancellation or a readiness timeout can stop an idle
    # pipeline; a blocking get() could otherwise keep the camera thread alive.
    while not THREAD_STOP:
        packet = queue.tryGet()
        if packet is not None:
            return packet
        time.sleep(0.01)
    return None

def thread(callback, _pipeline, outputFrames):
    """Own the DepthAI device loop and pass each completed result to its callback."""
    global THREAD_STOP, RUNNING

    with dai.Device(_pipeline) as device:
        logger.info('Inference ready: backend=Luxonis model=TinyYOLOv4 device=OAK-VPU USB=%s',
                    device.getUsbSpeed())
        # 'detections' already contains labels and millimeter spatial coordinates
        # computed on the OAK. No host inference/device selection happens here.
        previewQueue = device.getOutputQueue(name="rgb", maxSize=4, blocking=False)
        detectionNNQueue = device.getOutputQueue(name="detections", maxSize=4, blocking=False)

        startTime = time.monotonic()
        counter = 0
        fps = 0
        color = (0, 0, 0)
        font = cv2.FONT_HERSHEY_SIMPLEX
        fontSize = 0.4
        xStart = 8
        yHeight = 20

        print('Camera has started, outputting frames: ' + str(outputFrames))
        RUNNING = True

        while THREAD_STOP == False:
            inPreview = _get_packet(previewQueue)
            inDet = _get_packet(detectionNNQueue)
            if inPreview is None or inDet is None:
                break

            frame = None

            if outputFrames:
                frame = inPreview.getCvFrame()

            counter+=1
            current_time = time.monotonic()
            if (current_time - startTime) > 1 :
                fps = counter / (current_time - startTime)
                counter = 0
                startTime = current_time

            detections = inDet.detections

            # If the frame is available, draw bounding boxes on it and show the frame
            height = 0
            width = 0

            if outputFrames:
                height = frame.shape[0]
                width  = frame.shape[1]

            personDetections = []
            threshold = flight_config.settings.detection_thresh
            for detection in detections:
                # The shipped COCO model uses class 0 for people.
                if detection.label == 0 and detection.confidence >= threshold:
                    # Ignore detections with depth closer than the minimum the camera supports
                    if (detection.spatialCoordinates.z / 1000.0) <= 0.5:
                        continue

                    # Normalize device millimeters to the meter contract used by
                    # the movement rules and the Jetson spatial adapter.
                    personDetections.append(Detection(detection.spatialCoordinates.x / 1000.0, detection.spatialCoordinates.y / 1000.0, detection.spatialCoordinates.z / 1000.0, detection.confidence, fps))

                    if outputFrames:
                        # Denormalize bounding box
                        x1 = int(detection.xmin * width)
                        x2 = int(detection.xmax * width)
                        y1 = int(detection.ymin * height)
                        y2 = int(detection.ymax * height)
                        
                        cv2.rectangle(frame, (x1, y1), (x2, y2), color, cv2.FONT_HERSHEY_SIMPLEX)

            if outputFrames:
                cv2.putText(frame, "NN (fps): {:.2f}".format(fps), (xStart, yHeight*4), font, fontSize, color)

                if len(personDetections) == 0:
                    cv2.putText(frame, "X (m): 0", (xStart, yHeight), font, fontSize, color)
                    cv2.putText(frame, "Y (m): 0", (xStart, yHeight*2), font, fontSize, color)
                    cv2.putText(frame, "Z (m): 0", (xStart, yHeight*3), font, fontSize, color)
                else:
                    closest = None

                    for detection in personDetections:
                        if closest == None: closest = detection
                        elif closest.z > detection.z: closest = detection

                    cv2.putText(frame, f"X (m): {closest.x:.2f}", (xStart, yHeight), font, fontSize, color)
                    cv2.putText(frame, f"Y (m): {closest.y:.2f}", (xStart, yHeight*2), font, fontSize, color)
                    cv2.putText(frame, f"Z (m): {closest.z:.2f}", (xStart, yHeight*3), font, fontSize, color)

            callback(personDetections, frame if outputFrames else None, capture_time(inDet, dai))

        RUNNING = False

        print('Stopping camera...')

        previewQueue.close()
        detectionNNQueue.close()

        print('Camera has stopped')

class YoloCamera(PerceptionCamera):
    """Lifecycle wrapper for the OAK graph; default selection remains nearest Z."""

    _thread = None
    _detections = []
    _userCallback = None

    def previewSize():
        """Return the RGB preview width and height used by the OAK pipeline."""
        return (416, 416)

    def __init__(self, callback=None, config=inference_settings):
        """Store backend settings and initialize the shared detection publisher."""
        super().__init__(callback, config)
        self._detections = []
        self._thread = None
        self.setup()
        self._userCallback = callback

    def setup(self):
        """Construct the DepthAI graph and initialize camera and inference queues."""
        # Defining nodes describes device work; dai.Device in the worker starts it.
        self._pipeline = dai.Pipeline()

        # Define sources and outputs
        self._camRgb = self._pipeline.create(dai.node.ColorCamera)
        self._spatialDetectionNetwork = self._pipeline.create(dai.node.YoloSpatialDetectionNetwork)
        self._monoLeft = self._pipeline.create(dai.node.MonoCamera)
        self._monoRight = self._pipeline.create(dai.node.MonoCamera)
        self._stereo = self._pipeline.create(dai.node.StereoDepth)

        self._xoutRgb = self._pipeline.create(dai.node.XLinkOut)
        self._xoutNN = self._pipeline.create(dai.node.XLinkOut)

        self._xoutRgb.setStreamName("rgb")
        self._xoutNN.setStreamName("detections")

        # Properties
        self._camRgb.setPreviewSize(416, 416)
        self._camRgb.setResolution(dai.ColorCameraProperties.SensorResolution.THE_1080_P)
        self._camRgb.setInterleaved(False)
        self._camRgb.setColorOrder(dai.ColorCameraProperties.ColorOrder.BGR)

        self._monoLeft.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
        self._monoLeft.setBoardSocket(dai.CameraBoardSocket.LEFT)
        self._monoRight.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
        self._monoRight.setBoardSocket(dai.CameraBoardSocket.RIGHT)

        # setting node configs
        self._stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.HIGH_DENSITY)

        self._spatialDetectionNetwork.setBlobPath(str(self.config.luxonis_model_path))
        # The blob is compiled for the OAK VPU. Jetson mode loads its separate
        # TensorRT engine instead; the two artifacts are not interchangeable.
        # Filter on the host so lowering DETECTION_THRESH also takes effect
        # without rebuilding the OAK pipeline. Device-side NMS is unchanged.
        self._spatialDetectionNetwork.setConfidenceThreshold(0.0)
        self._spatialDetectionNetwork.input.setBlocking(False)
        self._spatialDetectionNetwork.setBoundingBoxScaleFactor(0.5)
        self._spatialDetectionNetwork.setDepthLowerThreshold(250)
        self._spatialDetectionNetwork.setDepthUpperThreshold(10000)

        # Yolo specific parameters
        self._spatialDetectionNetwork.setNumClasses(80)
        self._spatialDetectionNetwork.setCoordinateSize(4)
        self._spatialDetectionNetwork.setAnchors(np.array([10,14, 23,27, 37,58, 81,82, 135,169, 344,319]))
        self._spatialDetectionNetwork.setAnchorMasks({ "side26": np.array([1,2,3]), "side13": np.array([3,4,5]) })
        self._spatialDetectionNetwork.setIouThreshold(0.5)

        # Linking
        self._monoLeft.out.link(self._stereo.left)
        self._monoRight.out.link(self._stereo.right)

        self._camRgb.preview.link(self._spatialDetectionNetwork.input)
        # Passthrough originates from frames consumed by the network; its
        # output and detections are sent back through separate host queues.
        self._spatialDetectionNetwork.passthrough.link(self._xoutRgb.input)
        self._spatialDetectionNetwork.out.link(self._xoutNN.input)

        self._stereo.depth.link(self._spatialDetectionNetwork.inputDepth)

    def running(self):
        """Report whether camera capture is active and its latest result is fresh."""
        global RUNNING

        return RUNNING and not THREAD_STOP and self._fresh()

    def start(self):
        """Prepare device state and start the camera processing thread."""
        global THREAD_STOP, RUNNING
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError('Camera is already running')
        THREAD_STOP = False
        RUNNING = False
        self.ready_event.clear()
        self.stopped_event.clear()
        self.error = None
        self._snapshot = ([], None, None, False)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        """Run the capture loop, publish inference results, and handle worker errors."""
        global RUNNING
        try:
            thread(self._callback, self._pipeline, self._userCallback is not None)
        except Exception as error:
            self.error = error
            logger.exception('Camera pipeline failed')
        finally:
            RUNNING = False
            self.ready_event.clear()
            self.stopped_event.set()

    def stop(self):
        """Request worker shutdown and release the device and associated queues."""
        global THREAD_STOP
        THREAD_STOP = True
        self.ready_event.clear()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(5)
            if self._thread.is_alive():
                logger.warning('Camera thread did not stop within five seconds')

    def _callback(self, detections, frame, captured_at=None):
        """Convert model output to shared Detection objects and publish the frame."""
        if THREAD_STOP:
            return
        self._publish(detections, frame, captured_at)
