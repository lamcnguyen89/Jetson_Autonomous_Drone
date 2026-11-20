import pytest

from simulation.environment import prepareForTest, shutdownAfterTest


@pytest.fixture
def environment():
    core, vehicle, camera, sitl, ui = prepareForTest()
    try:
        yield vehicle, camera, core
    finally:
        shutdownAfterTest(sitl, core, camera, ui)
