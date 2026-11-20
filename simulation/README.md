# Simulation

All SITL-specific code and assets are kept here. The simulator shares the flight
implementation in the project's `src/` directory.
Flight movement settings come from the project root `.env`, through the shared
`src/config.py` loader. Restart the backend after tuning them. The constants
below are only for the simulated camera.

```text
simulation/
  sitl.py             Interactive backend entry point
  environment.py      SITL setup, telemetry and shutdown
  paths.py            Binary and parameter file resolution
  mock_camera.py      Simulated detections and GPX playback
  constants.py        Mock camera settings
  uiconnection.py     WebSocket connection to the visualiser
  integration.sh     Test runner using the project's .venv
  requirements.txt   Python dependencies, including ../requirements.txt
  ardupilot/          SITL binaries and copter-hexa.parm
  tests/              Integration tests and layout regression checks
  testutils/          Mock vehicle helper
  visualiser/         Vue frontend, configuration and Yarn lockfile
```

Install Python dependencies with the project installer:

```bash
sudo ./scripts/Install_Auto_Drone.sh --with-sitl
```

This also installs/enables the flight service as described in the main README.
For an existing development `.venv`, the Python-only equivalent is:

```bash
.venv/bin/python -m pip install -r simulation/requirements.txt
```

The Linux executable at `simulation/ardupilot/arducopter-linux` must match your
host's CPU architecture; the bundled Linux binary is ARM64. The `arducopter`
binary is for Intel macOS. To use a different executable, set
`ARDUPILOT_SITL_BINARY` to its absolute path. The parameter file is always resolved
from this directory, regardless of your working directory.

Start the backend from the project root:

```bash
.venv/bin/python -m simulation.sitl
```

Add `--verbose` for ArduPilot output. You can also run an absolute path to
`simulation/sitl.py` with the project's Python from any working directory.
Importing `simulation.sitl` does not start a simulator.

Start the frontend in another terminal:

```bash
cd simulation/visualiser
yarn install
yarn serve
```

The frontend connects to the backend at `127.0.0.1:5678` and is normally served at
`http://localhost:8080`. Its source-relative imports and frontend configuration
remain within `visualiser/`.

Run all simulation tests from the project root:

```bash
./simulation/integration.sh
```

An absolute path to this script also works from another directory. It selects
the root `.venv`, root pytest configuration and this directory's tests. Additional
pytest arguments are forwarded, for example:

```bash
./simulation/integration.sh --collect-only -q
./simulation/integration.sh -k layout -q
```

The flight unit tests remain under `src/rules/tests/` and use the same root pytest
configuration. `pytest.ini` resolves source imports from the project root, so no
manual `PYTHONPATH` is needed.
