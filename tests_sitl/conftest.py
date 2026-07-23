import pytest
import time
import threading
import json
import os
import platform
import sys

from typing import Tuple

from .dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()

from dronekit import connect
from dronekit import Vehicle
from dronekit_sitl import SITL

sys.path.insert(0, './src')

from core import Core, ExecutionState
from camera.mock import MockCamera
from .uiconnection import UIConnection

ACTIVE_THREADS = []

SITL_BINARIES_BY_SYSTEM = {
    "Darwin": ("arducopter",),
    "Linux": ("arducopter-linux", "arducopter"),
}

def is_linux_binary(path: str) -> bool:
    with open(path, "rb") as binary:
        return binary.read(4) == b"\x7fELF"

def sitl_binary_path() -> str:
    override_path = os.environ.get("ARDUPILOT_SITL_BINARY")
    if override_path:
        return os.path.abspath(override_path)

    system = platform.system()
    binary_names = SITL_BINARIES_BY_SYSTEM.get(system)
    if binary_names is None:
        supported_systems = ", ".join(sorted(SITL_BINARIES_BY_SYSTEM.keys()))
        raise RuntimeError(
            f"No default ArduPilot SITL binary is configured for {system}. "
            f"Set ARDUPILOT_SITL_BINARY to your arducopter executable. "
            f"Configured systems: {supported_systems}."
        )

    existing_paths = [
        os.path.abspath(os.path.join(".dronekit", binary_name))
        for binary_name in binary_names
        if os.path.exists(os.path.join(".dronekit", binary_name))
    ]
    for path in existing_paths:
        if system != "Linux" or is_linux_binary(path):
            return path

    expected_paths = ", ".join(
        os.path.abspath(os.path.join(".dronekit", binary_name))
        for binary_name in binary_names
    )
    raise FileNotFoundError(
        f"No usable ArduPilot SITL binary found for {system}. "
        f"Expected one of: {expected_paths}. "
        f"Add a Linux arducopter build there, or set ARDUPILOT_SITL_BINARY "
        f"to the path of your Linux arducopter executable."
    )

@pytest.fixture
def environment():
    [core, vehicle, camera, sitl, ui] = prepareForTest()
    yield (vehicle, camera, core)
    shutdownAfterTest(sitl, core, camera, ui)

def core_thread(core):
    print('running core thread...')
    core.run()
    print('stopped core thread')

def ui_thread(ui: UIConnection, core: Core, vehicle: Vehicle, camera: MockCamera):
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

    HOME = '-35.363261,149.165230,584,353'

    sitl = SITL(path=sitl_binary_path(), defaults_filepath=os.path.abspath('./.dronekit/copter-hexa.parm'))
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

    vehicle.mode = 'GUIDED'
    core.state = ExecutionState.Takeoff

    print('waiting for vehicle start')

    start = time.time()
    while True:
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
