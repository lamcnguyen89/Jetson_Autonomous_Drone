"""Assemble the real controller around a virtual autopilot and synthetic camera.

ArduPilot SITL replaces the physical flight controller, while MockCamera turns
a supplied person's geographic position into camera-like detections. Core and
its movement rules are reused. This exercises flight behavior, not YOLO/TensorRT
accuracy or real RGB/depth synchronization; those need separate perception tests.
"""

import time
import threading
import json
from typing import Tuple

from src.dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()

from dronekit import connect, Vehicle, VehicleMode
from dronekit_sitl import SITL
from core import Core, ExecutionState
from .mock_camera import MockCamera
from .uiconnection import UIConnection
from .paths import DEFAULTS_FILE, sitl_binary_path

ACTIVE_THREADS = []

def core_thread(core):
    print('running core thread...')
    core.run()
    print('stopped core thread')

def ui_thread(ui: UIConnection, core: Core, vehicle: Vehicle, camera: MockCamera):
    # Publish a slower UI view independently of the control loop so rendering
    # cadence does not determine how often the simulated vehicle is commanded.
    print('running ui thread...')

    # On startup, send reset message
    resetMessage = { "type": "reset" }
    ui.send(json.dumps(resetMessage))

    while ui.active():
        # generate json blob of data to send
        vehicleGlobalFrame = vehicle.location.global_relative_frame
        vehicleLocalFrame = vehicle.location.local_frame

        if vehicleGlobalFrame.lat == 0.0 and vehicleGlobalFrame.lon == 0.0:
            time.sleep(0.1)
            continue

        message = {
            "type": "update",
            "vehicle": {
                "heading": vehicle.heading,
                "coordinates": {
                    "latitude": vehicleGlobalFrame.lat,
                    "longitude": vehicleGlobalFrame.lon,
                },
                "altitude": "{:.2f}".format(vehicleGlobalFrame.alt)
            },
            "core": {
                "state": core.state,
                "rule": core.activeRule
            }
        }

        detection = camera.closestDetection()
        if detection != None:
            message["person"] = {
                "global": camera.mockedPosition,
                "local": {
                    "x": detection.x,
                    "z": detection.z
                }
            }

        ui.send(message)

        time.sleep(0.1)

    # On shutdown, send reset message
    resetMessage = { "type": "reset" }
    ui.send(json.dumps(resetMessage))

    print('stopped ui thread')

def prepareForTest(timeout = True, verbose=False) -> Tuple[Core, Vehicle, MockCamera, SITL, UIConnection]:
    '''
    Prepares the SITL environment for a test.

    Should be called from a fixture
    '''

    print('start fixture')

    # Use an executable for this host's CPU architecture, with simulation assets
    # resolved relative to the package rather than the shell's working directory.
    sitl = SITL(path=sitl_binary_path(), defaults_filepath=str(DEFAULTS_FILE))
    sitl_args = ['--model', 'hexa']
    sitl.launch(sitl_args, await_ready=True, restart=False, verbose=verbose)

    connection_string = sitl.connection_string()

    print('connecting to ' + connection_string)

    vehicle = connect(connection_string, wait_ready=True, rate=10)
    camera = MockCamera(vehicle)

    core = Core(vehicle, camera)
    thread = threading.Thread(target=core_thread, args=(core,))
    thread.start()

    ACTIVE_THREADS.append(thread)

    ui = UIConnection()
    uithread = threading.Thread(target=ui_thread, args=(ui, core, vehicle, camera))
    uithread.start()

    ACTIVE_THREADS.append(uithread)

    print('init done')

    # Wait until the virtual drone hits the home position
    start = time.time()
    while True:
        # -35.363261 149.1652299
        location = vehicle.location.global_relative_frame
        print(location.lat, location.lon)

        if location.lat == -35.363261 and location.lon == 149.1652299:
            break

        if timeout and time.time() - start > 10:
            core.stop()
            raise ValueError('timeout on gps initial position exceeded')

        time.sleep(1)

    # Setup initial state to get the drone "flying"

    vehicle.mode = VehicleMode('GUIDED')

    print('waiting for vehicle start')

    start = time.time()
    while True:
        # Simulate the pilot's arm button after GUIDED and pre-arm readiness.
        # Core itself must never send an arm command.
        if vehicle.mode.name == 'GUIDED' and vehicle.is_armable and not vehicle.armed:
            vehicle.armed = True

        if core.state == ExecutionState.Running:
            print('started')
            break

        if timeout and time.time() - start > 60:
            core.stop()
            raise ValueError('timeout on takeoff exceeded')

        time.sleep(1)

    return [core, vehicle, camera, sitl, ui]

def shutdownAfterTest(sitl, core: Core, camera: MockCamera, ui: UIConnection):
    '''
    Clears SITL environment after a test
    '''

    print('shutdown called')

    camera.stop()
    core.stop()
    ui.stop()

    if sitl != None:
        sitl.stop()
        print('stopped sitl')

    for thread in ACTIVE_THREADS:
        thread.join()

