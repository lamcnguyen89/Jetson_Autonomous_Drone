"""Backend selection, geometry, target identity and perception failure boundaries."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from config import ConfigurationError, InferenceSettings, load_inference_settings
from camera.detection import Detection
from camera.factory import camera_class, create_camera
from camera.jetson import JetsonCamera, TargetLock, create_pipeline
from camera.runtime import PerceptionCamera, capture_time
from camera.spatial import spatial_position
from camera.tensorrt_yolo import decode, preprocess, validate_manifest


def person(track_id=1, z=3):
    return Detection(0, 0, z, .95, 30, track_id=track_id)


def test_existing_env_defaults_to_luxonis(tmp_path):
    path = tmp_path / '.env'
    path.write_text('SPEED=1\n')
    assert load_inference_settings(path, environ={}).backend == 'luxonis'


def test_selection_and_paths_are_independent_of_working_directory(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    path.write_text('INFERENCE_BACKEND=jetson\nJETSON_MODEL_PATH=models/custom.engine\n')
    monkeypatch.chdir(tmp_path)
    config = load_inference_settings(path, environ={})
    assert config.jetson_model_path == Path(__file__).resolve().parents[2] / 'models/custom.engine'
    assert camera_class(config) is JetsonCamera
    assert create_camera(config=config).config is config
    assert load_inference_settings(path, environ={'INFERENCE_BACKEND': 'luxonis'}).backend == 'luxonis'


@pytest.mark.parametrize('key,value', [
    ('INFERENCE_BACKEND', 'cuda'), ('INFERENCE_BACKEND', ''),
    ('PERCEPTION_MAX_AGE_SECONDS', 'nan'), ('PERCEPTION_MAX_AGE_SECONDS', '0'),
    ('PERCEPTION_MAX_AGE_SECONDS', '-1'), ('PERCEPTION_MAX_AGE_SECONDS', 'fast'),
    ('TARGET_LOST_TIMEOUT_SECONDS', 'inf'), ('TARGET_LOST_TIMEOUT_SECONDS', '0'),
    ('JETSON_MODEL_PATH', ''),
])
def test_invalid_inference_configuration_is_rejected(tmp_path, key, value):
    path = tmp_path / '.env'
    path.write_text(f'{key}={value}\n')
    with pytest.raises(ConfigurationError, match=key):
        load_inference_settings(path, environ={})


def test_luxonis_startup_does_not_import_gpu_or_tracker_dependencies():
    root = Path(__file__).resolve().parents[2]
    import os
    result = subprocess.run([sys.executable, '-c',
                             'import main, sys; '
                             'assert not ({"torch", "ultralytics", "tensorrt"} & sys.modules.keys())'],
                            cwd=root, env={**os.environ, 'PYTHONPATH': str(root / 'src'),
                                          'INFERENCE_BACKEND': 'luxonis'},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('script', ['utils/camera.py', 'utils/check_jetson_inference.py',
                                    'scripts/prepare_jetson_model.py'])
def test_camera_tools_can_show_help_outside_project_without_hardware(script, tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root / script), '--help'], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout


def test_fresh_empty_output_is_healthy_but_old_results_are_hidden(monkeypatch):
    from camera import runtime
    now = [100.0]
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: now[0])
    camera = PerceptionCamera()
    assert not camera._fresh()
    camera._publish([], None)
    assert camera.ready_event.is_set() and camera._fresh()
    camera._publish([person()], None)
    now[0] += camera.config.max_age + .01
    assert not camera._fresh()
    assert camera.detections() == []
    camera._publish([person()], None, captured_at=99)
    assert not camera.ready_event.is_set()
    camera._publish([person()], None)
    camera.error = RuntimeError('Inference failed')
    assert not camera._fresh() and camera.detections() == []


def test_capture_age_includes_usb_transfer(monkeypatch):
    from datetime import timedelta
    from camera import runtime
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: 100)
    packet = SimpleNamespace(getTimestamp=lambda: timedelta(seconds=20))
    dai = SimpleNamespace(Clock=SimpleNamespace(now=lambda: timedelta(seconds=20.2)))
    assert capture_time(packet, dai) == pytest.approx(99.8)


def test_selected_track_is_published_with_its_batch_and_expires():
    camera = JetsonCamera()
    selected = person()
    camera._publish([selected], None, selected=selected)
    assert camera.closestDetection() is selected
    assert not camera.tracking_hold()
    camera._publish([], None, holding=True)
    assert camera.closestDetection() is None and camera.tracking_hold()
    camera._publish([selected], None, time.monotonic() - 10, selected=selected)
    assert camera.closestDetection() is None and not camera.running()


def test_letterbox_and_inverse_boxes():
    image = np.zeros((360, 640, 3), np.uint8)
    image[:, :, 2] = 255
    tensor, transform = preprocess(image)
    assert tensor.shape == (1, 3, 640, 640)
    assert tensor[0, 0, 140, 0] == 1  # RGB red channel
    assert tensor[0, 2, 140, 0] == 0
    assert tensor[0, 0, 0, 0] == pytest.approx(114 / 255)
    rows = np.array([[[10, 160, 60, 240, .9, 0],
                      [10, 160, 60, 240, .9, 2],
                      [10, 160, 60, 240, .05, 0],
                      [10, 160, float('nan'), 240, .9, 0]]])
    decoded = decode(rows, image.shape, transform)
    assert decoded.shape == (1, 6)
    assert decoded[0, :4] == pytest.approx([10, 20, 60, 100])


def test_depth_projection_uses_calibration_meters_and_y_up():
    depth = np.full((100, 200), 2000, dtype=np.uint16)
    calibration = np.array([[100, 0, 100], [0, 100, 50], [0, 0, 1]])
    assert spatial_position([100, 10, 140, 50], depth, calibration) == pytest.approx((.4, .4, 2))
    depth[20:40, 110:130] = 0
    assert spatial_position([100, 10, 140, 50], depth, calibration) is None


@pytest.mark.parametrize('depth_value', [0, 249, 500, 10001, float('nan')])
def test_invalid_or_too_close_depth_is_never_published(depth_value):
    assert spatial_position([10, 10, 90, 90], np.full((100, 100), depth_value), np.eye(3)) is None


def test_depth_median_rejects_background_outliers():
    depth = np.full((100, 100), 2000, dtype=np.uint16)
    depth[25:30, 25:75] = 9000
    assert spatial_position([0, 0, 100, 100], depth, np.eye(3))[2] == 2


def test_target_id_survives_nearer_people_and_short_occlusion():
    target = TargetLock(timeout=2)
    selected = person(1, 3)
    assert target.update([selected, person(2, 4)], 10) is selected
    assert target.update([selected, person(2, 1)], 10.5) is selected
    assert target.update([person(2, 1)], 11) is None
    assert target.waiting
    assert target.update([selected], 12) is selected
    assert not target.waiting
    assert target.update([person(2, 1)], 14.1).track_id == 2


def test_manifest_rejects_wrong_model_and_modified_engine(tmp_path):
    engine = tmp_path / 'model.engine'
    engine.write_bytes(b'engine')
    manifest = {'model': 'yolo26m', 'precision': 'fp16', 'input_size': 640,
                'output_format': 'xyxy_score_class',
                'engine_sha256': hashlib.sha256(b'engine').hexdigest()}
    engine.with_suffix('.json').write_text(json.dumps(manifest))
    assert validate_manifest(engine)['model'] == 'yolo26m'
    engine.write_bytes(b'changed')
    with pytest.raises(ValueError, match='manifest'):
        validate_manifest(engine)
    manifest['model'] = 'yolo26s'
    engine.with_suffix('.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='YOLO26m'):
        validate_manifest(engine)


def test_jetson_pipeline_contains_synchronization_and_no_oak_neural_network():
    pipeline = create_pipeline()
    nodes = [node.getName() for node in pipeline.getAllNodes()]
    assert 'Sync' in nodes and 'StereoDepth' in nodes
    assert not any('Network' in name for name in nodes)
    assert JetsonCamera.previewSize() == (640, 360)


def test_real_botsort_preserves_identity_and_handles_empty_frames():
    pytest.importorskip('ultralytics')
    from camera.jetson import create_tracker
    from ultralytics.engine.results import Boxes
    tracker = create_tracker()
    frame = np.zeros((360, 640, 3), np.uint8)
    rows = np.array([[100, 80, 200, 300, .95, 0]], np.float32)
    first = tracker.update(Boxes(rows, frame.shape[:2]), frame)
    second = tracker.update(Boxes(rows, frame.shape[:2]), frame)
    assert first[0, 4] == second[0, 4]
    empty = tracker.update(Boxes(np.empty((0, 6), np.float32), frame.shape[:2]), frame)
    assert len(empty) == 0


def test_gpu_startup_failure_does_not_open_camera_or_fall_back(monkeypatch):
    pytest.importorskip('ultralytics')
    from camera import jetson, tensorrt_yolo
    def fail(path):
        raise RuntimeError('TensorRT failed')
    monkeypatch.setattr(tensorrt_yolo, 'TensorRTYolo', fail)
    monkeypatch.setattr(jetson.dai, 'Device', lambda *a: pytest.fail('Must not open camera'))
    camera = JetsonCamera()
    camera.start()
    assert camera.stopped_event.wait(10)
    camera.stop()
    assert isinstance(camera.error, RuntimeError)
    assert not camera.ready_event.is_set() and not camera.running()


def test_jetson_worker_publishes_rgb_depth_tracks_without_hardware(monkeypatch):
    pytest.importorskip('ultralytics')
    from camera import jetson, tensorrt_yolo
    frame = np.zeros((360, 640, 3), np.uint8)
    depth = np.full((360, 640), 3000, np.uint16)
    config = replace(InferenceSettings(), backend='jetson')
    observed = []
    camera = JetsonCamera(lambda detections, image: (observed.append(detections), camera._stop.set()), config)
    detector = SimpleNamespace(infer=lambda image: np.array([[100, 80, 200, 300, .95, 0]], np.float32),
                               close=lambda: None)
    monkeypatch.setattr(tensorrt_yolo, 'TensorRTYolo', lambda path: detector)
    monkeypatch.setattr(jetson, 'capture_time', lambda *a: time.monotonic())
    packet = {'rgb': SimpleNamespace(getCvFrame=lambda: frame),
              'depth': SimpleNamespace(getFrame=lambda: depth)}
    class Device:
        def __init__(self, pipeline): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def getUsbSpeed(self): return 'mock'
        def readCalibration(self):
            return SimpleNamespace(getCameraIntrinsics=lambda *a: [[500, 0, 320], [0, 500, 180], [0, 0, 1]])
        def getOutputQueue(self, *a, **kw): return SimpleNamespace(tryGet=lambda: packet)
    monkeypatch.setattr(jetson.dai, 'Device', Device)
    camera.start()
    assert camera.stopped_event.wait(10)
    camera.stop()
    assert camera.error is None
    assert observed[0][0].z == 3
    assert observed[0][0].track_id is not None


@pytest.mark.parametrize('healthy,waiting', [(False, False), (True, True)])
def test_perception_failure_or_temporary_target_loss_holds_without_search(healthy, waiting):
    from dronekit_compat import apply_dronekit_compatibility
    apply_dronekit_compatibility()
    from core import Core
    vehicle = SimpleNamespace(add_attribute_listener=lambda *a: None)
    camera = SimpleNamespace(running=lambda: healthy, tracking_hold=lambda: waiting)
    core = Core(vehicle, camera)
    core.rules = [SimpleNamespace(update=lambda: pytest.fail('Must not use follow or search'))]
    assert core._rule_command() == ((0.0, 0.0), 0.0)
    assert core.activeRule == 'perception_hold'
