"""Run the interactive SITL backend for simulation/visualiser."""

import argparse
from pathlib import Path
import sys
import time

# Support both `python -m simulation.sitl` and an absolute script path from
# another working directory. Importing this module never starts the simulator.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verbose', action='store_true', help='Show ArduPilot output')
    args = parser.parse_args()

    from simulation.environment import prepareForTest, shutdownAfterTest

    core, vehicle, camera, sitl, ui = prepareForTest(timeout=False, verbose=args.verbose)

    def uicallback(data):
        if data['type'] == 'control':
            camera.setGlobalCoordinate(data['latitude'], data['longitude'])

    ui.setOnDataCallback(uicallback)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        shutdownAfterTest(sitl, core, camera, ui)


if __name__ == '__main__':
    main()
