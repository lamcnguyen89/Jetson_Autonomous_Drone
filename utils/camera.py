"""Preview the configured inference backend without connecting to the autopilot."""

import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import cv2
from camera.factory import create_camera
from config import inference_settings, notification_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--seconds', type=float, default=0, help='0 runs until interrupted')
    args = parser.parse_args()
    latest = [None]

    def callback(detections, frame):
        latest[0] = frame

    camera = create_camera(callback)
    print(f'Camera-only preview: backend={inference_settings.backend}; no MAVLink connection')
    print('The flight service must release the OAK before running this utility.')
    camera.start()
    started, reported = time.monotonic(), 0
    try:
        while not args.seconds or time.monotonic() - started < args.seconds:
            if camera.stopped_event.is_set():
                raise RuntimeError('Camera pipeline stopped') from camera.error
            if not camera.ready_event.is_set() and time.monotonic() - started > notification_settings.camera_ready_timeout:
                raise TimeoutError('Camera did not produce current output before readiness timeout')
            now = time.monotonic()
            if now - reported >= 1:
                target = camera.closestDetection()
                print(f'healthy={camera.running()} target='
                      + (f'id={target.track_id} xyz=({target.x:.2f}, {target.y:.2f}, {target.z:.2f})m'
                         if target else 'none'))
                reported = now
            if not args.headless and latest[0] is not None:
                cv2.imshow('Tracking preview', latest[0])
                if cv2.waitKey(1) == ord('q'):
                    break
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        camera.stop()
        if not args.headless:
            cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
