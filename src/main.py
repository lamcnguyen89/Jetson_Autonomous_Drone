"""Flight-service entry point: assemble components and own process startup/shutdown.

The main thread sets up logging, the selected camera, the trusted MAVLink link
and readiness notifications. A camera worker publishes perception; a separate
Core worker consumes it and chooses movement commands. main() does not perform
inference or choose a person-following rule itself.
"""

import sys
if sys.version_info[0] < 3:
    raise Exception("Must be using Python 3")

import platform
import os
if platform.machine() == 'aarch64':  # Jetson
    os.environ['OPENBLAS_CORETYPE'] = "ARMV8"

from dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()

from dronekit import connect, Vehicle
import config as flight_config
from config import notification_settings, inference_settings, refresh_flight_parameter_defaults
from core import Core
from camera.base import BaseCamera
from camera.factory import create_camera, camera_class
from filtered_vehicle import FlightControllerVehicle
from notifications import Notifier
from log_management import cleanup_unarmed_logs, next_log_path

import argparse
import time
import threading
import signal
import cv2
import platform
import logging

PARENT_DIRECTORY = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))

EXIT: bool         = False
core: Core         = None
camera: BaseCamera = None
vehicle: Vehicle   = None
videoWriter        = None

# See: https://stackoverflow.com/a/66209331
class LoggerWriter:
    """Adapt writes to stdout/stderr into calls to the application's logger."""
    """Route legacy print() output through the same file/journal logging handlers."""
    def __init__(self, logfct):
        """Store the logging function that will receive complete output lines."""
        self.logfct = logfct
        self.buf = []

    def write(self, msg):
        """Send each nonblank line from a stream write to the configured logger."""
        if msg.endswith('\n'):
            self.buf.append(msg.removesuffix('\n'))
            self.logfct(''.join(self.buf))
            self.buf = []
        else:
            self.buf.append(msg)

    def flush(self):
        """Provide the no-op flush method expected by file-like stream consumers."""
        pass

def core_thread(core):
    """Run the flight controller loop on a worker and log unexpected failures."""
    global EXIT

    if EXIT:
        return

    logging.info('Control thread starting')
    try:
        core.run()
    except Exception:
        logging.exception('Control thread failed')
        raise
    finally:
        logging.info('Control thread stopped')

def camera_callback(detections, cvFrame):
    """Receive camera output for optional preview or application-level handling."""
    global videoWriter
    videoWriter.write(cvFrame)

def stop():
    """Set the shared shutdown signal used by the main and worker loops."""
    global EXIT, core, camera, vehicle

    EXIT = True

    if core:
        core.stop()

    if camera:
        camera.stop()

def signal_handler(sig, frame):
    """Translate an operating-system termination signal into graceful shutdown."""
    logging.info('Received signal %s; stopping service', signal.Signals(sig).name)
    stop()


def wait_for_camera_ready(camera, timeout, should_stop):
    """Require current pipeline output before declaring the tracking service ready.

    Starting a thread or opening a USB device does not prove inference works.
    An empty batch is sufficient; readiness must not require a person in view.
    Short waits also let a shutdown request cancel startup promptly.
    """
    logging.info('Waiting for first camera output: timeout=%ss', timeout)
    deadline = time.monotonic() + timeout
    while not should_stop():
        if camera.stopped_event.is_set():
            raise RuntimeError('Camera stopped before the tracking service became ready') from camera.error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Timed out waiting for the first camera output')
        if camera.ready_event.wait(min(0.1, remaining)) and camera.running():
            logging.info('Camera output ready')
            return not should_stop()
    logging.info('Camera readiness wait cancelled by shutdown')
    return False


def prepare_tracking_core(vehicle, camera, should_stop):
    """Wait for camera readiness, then build the notifier and flight-control core."""
    # Readiness is the intersection of perception output and a fresh heartbeat
    # from the trusted autopilot. Notifications describe that state afterward.
    if not wait_for_camera_ready(camera, notification_settings.camera_ready_timeout, should_stop):
        return None
    if (vehicle._trusted_fc_system_id is None
            or vehicle.last_heartbeat >= flight_config.settings.heartbeat_timeout):
        raise RuntimeError('Flight-controller connection is not ready')
    notifier = Notifier(vehicle)
    prepared = Core(vehicle, camera, notifier=notifier)
    if should_stop():
        return None
    try:
        notifier.service_ready()
    except Exception:
        logging.exception('Readiness notification failed; movement control continues')
    try:
        notifier.start_rc_notifications()
    except Exception:
        logging.exception('RC notification setup failed; movement control continues')
    return prepared


def main(args):
    """Load settings, connect hardware, start workers, and coordinate shutdown."""
    global EXIT, core, camera, vehicle, videoWriter

    thread = None

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Setup the log file
    logpath = args.log_path
    if not os.path.exists(logpath):
        logpath = os.path.join(PARENT_DIRECTORY, 'logs')

        print('WARNING Log path not found, redirecting to: ' + os.path.join(PARENT_DIRECTORY, 'logs'))

        # In the event the user has not got a relative `logs` folder
        if not os.path.exists(logpath):
            os.mkdir(logpath)

    # Run once per process startup, before opening the current session's log.
    removed_logs = cleanup_unarmed_logs(logpath)
    logFile = next_log_path(logpath)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)-15s %(levelname)-8s %(name)s [%(threadName)s] %(message)s",
        handlers=[
            logging.FileHandler(logFile, mode='x'),
            logging.StreamHandler()
        ]
    )

    logger = logging.getLogger()
    logger.info('Flight service starting: pid=%s uri=%s baud=%s log=%s',
                os.getpid(), args.uri, flight_config.settings.baud_rate, logFile)
    logger.info('Removed %d previous logs without an arming attempt', removed_logs)
    logger.debug('Flight settings: %s', flight_config.settings)
    logger.debug('Notification settings: %s', notification_settings)
    logger.info('Inference configuration: %s', inference_settings)

    # Publish only numeric fallback values for the external WebUI, before any
    # camera or flight-controller startup. Keep generated metadata out of tuning.
    refresh_flight_parameter_defaults()
    logger.info('Refreshed flight parameter display defaults')

    # Capture print-based messages from existing components in the same session log.
    sys.stdout = LoggerWriter(logger.info)
    sys.stderr = LoggerWriter(logger.error)

    videoEnabled = args.video
    # Frame dimensions belong to the selected adapter: the OAK and Jetson paths
    # use different RGB output sizes, even though both expose the same positions.
    if args.video:
        logging.info('Saving videos to: ' + args.video_path)

        if not os.path.exists(args.video_path):
            logging.warning('Video path ' + args.video_path + ' does not exist, not recording video.')
            videoEnabled = False
        else:
            fileCount = len(os.listdir(args.video_path))
            videoWriter = cv2.VideoWriter(os.path.join(args.video_path, str(fileCount) + '.mkv'), cv2.VideoWriter_fourcc('M','J','P','G'), 30, camera_class().previewSize())

    try:
        if not EXIT:
            logging.info('Starting tracking camera')
            camera = create_camera(camera_callback if videoEnabled else None)
            camera.start()

            # The file killswitch gates controller connection/Core startup.
            # Camera startup occurs first, so it does not disable acquisition.
            logging.info('Searching for killswitch at: ' + args.killswitch_path)
            if os.path.exists(args.killswitch_path):
                logging.warning("Killswitch engaged, preventing core run")
                while not EXIT:
                    time.sleep(1)
            else:
                logging.info("Starting core")
                logging.info("Connecting to vehicle on: %s" % (args.uri,))
                connection_started = time.monotonic()
                vehicle = connect(args.uri, baud=flight_config.settings.baud_rate, wait_ready=['gps_0', 'armed', 'mode', 'attitude'], rate=20, vehicle_class=FlightControllerVehicle )
                logging.info('Flight-controller connection ready: elapsed=%.2fs',
                             time.monotonic() - connection_started)

                # Keep the flight loop separate from acquisition/GPU work. Core
                # reads the latest published batch rather than waiting for frames.
                core = prepare_tracking_core(vehicle, camera, lambda: EXIT)
                if core is None:
                    return
                thread = threading.Thread(target=core_thread, args=(core,))
                thread.start()

                while not EXIT:
                    logging.debug('core state is: ' + core.state + ', ' + core.activeRule)
                    time.sleep(1)

    except Exception:
        logging.exception('Flight service failed')
        raise
    finally:
        logging.info('Flight service shutting down')
        # Also clean up partial startup (for example, camera failure after the
        # MAVLink connection opened but before Core was constructed).
        stop()
        if vehicle is not None and core is None:
            vehicle.close()
        if thread:
            thread.join(5)
        if videoWriter:
            videoWriter.release()

    logging.info('Thank you for flying Matchstic Air. We wish you a pleasant onward journey.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--uri', type=str, required=True, help="URI to connect with for MAVLink data. e.g., udp:127.0.0.1:14550")
    parser.add_argument('--video', required=False, default=False, help="Specify to save video of detections", action='store_true')
    parser.add_argument('--log_path', type=str, required=False, default=os.path.join(PARENT_DIRECTORY, 'logs'), help="Path to save log output into")
    parser.add_argument('--video_path', type=str, required=False, default=os.path.join(PARENT_DIRECTORY, 'videos'), help="Path to save video into")
    parser.add_argument('--killswitch_path', type=str, required=False, default=os.path.join(PARENT_DIRECTORY, 'killswitch'), help="Path to a file that if exists, this program will do nothing when ran")

    args = parser.parse_args()

    main(args)
