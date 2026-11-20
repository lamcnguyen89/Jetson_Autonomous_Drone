"""Select a backend without importing optional Jetson dependencies in OAK mode."""

from config import inference_settings


def camera_class(config=inference_settings):
    if config.backend == 'luxonis':
        from .yolocam import YoloCamera
        return YoloCamera
    if config.backend == 'jetson':
        from .jetson import JetsonCamera
        return JetsonCamera
    raise ValueError(f'Unknown inference backend: {config.backend}')


def create_camera(callback=None, config=inference_settings):
    return camera_class(config)(callback, config=config)
