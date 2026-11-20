"""Run the real GPU detector and tracker on an image, with no camera or MAVLink I/O."""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import cv2
import numpy as np

from camera.tensorrt_yolo import TensorRTYolo
from camera.jetson import create_tracker
from config import inference_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, help='Omit to run on a blank 640x360 image')
    parser.add_argument('--iterations', type=int, default=30)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    frame = (cv2.imread(str(args.image)) if args.image else np.zeros((360, 640, 3), dtype=np.uint8))
    if frame is None:
        parser.error('Cannot read image')
    detector = TensorRTYolo(inference_settings.jetson_model_path)
    try:
        tracker = create_tracker()
        from ultralytics.engine.results import Boxes
        durations = []
        inference_times, tracker_times = [], []
        tracks = []
        # Detector construction already warmed the GPU. Repeating one image
        # isolates compute/association timing; it does not test moving-scene
        # identity retention, sensor transfer, depth projection or flight latency.
        for _ in range(args.iterations):
            start = time.perf_counter()
            rows = detector.infer(frame)
            inferred_at = time.perf_counter()
            # Detector timing includes preprocessing, GPU execution and decoding;
            # tracker timing includes Boxes adaptation and BoT-SORT association.
            tracks = tracker.update(Boxes(rows, frame.shape[:2]), frame)
            finished_at = time.perf_counter()
            durations.append((finished_at - start) * 1000)
            inference_times.append((inferred_at - start) * 1000)
            tracker_times.append((finished_at - inferred_at) * 1000)
        print(json.dumps({'model': 'YOLO26m', 'device': 'GPU:0', 'runtime': 'TensorRT',
                          'source_frame_size': [frame.shape[1], frame.shape[0]],
                          'iterations': args.iterations, 'detector_and_tracker_mean_ms': float(np.mean(durations)),
                          'detector_and_tracker_p95_ms': float(np.percentile(durations, 95)),
                          'detector_mean_ms': float(np.mean(inference_times)),
                          'tracker_mean_ms': float(np.mean(tracker_times)),
                          'person_tracks': np.asarray(tracks).tolist()}, indent=2))
    finally:
        detector.close()


if __name__ == '__main__':
    main()
