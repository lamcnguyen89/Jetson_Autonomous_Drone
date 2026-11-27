"""Select a backend without importing optional Jetson dependencies in OAK mode."""

from config import inference_settings


def camera_class(config=inference_settings):
    """Choose once at startup; also expose the class for video frame dimensions.

    Local imports keep GPU/tracker dependencies out of Luxonis startup. Camera
    pipelines differ between modes, so a backend change requires a restart.
    """
    if config.backend == 'luxonis':
        from .yolocam import YoloCamera
        return YoloCamera
    if config.backend == 'jetson':
        from .jetson import JetsonCamera
        return JetsonCamera
    raise ValueError(f'Unknown inference backend: {config.backend}')


def create_camera(callback=None, config=inference_settings):
    """Instantiate the selected backend and pass it the detection callback."""
    # Construction selects the adapter; start() owns the worker/hardware lifecycle.
    return camera_class(config)(callback, config=config)
