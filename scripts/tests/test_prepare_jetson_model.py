"""Installer preparation reuses only verified artifacts and rebuilds when needed."""

import hashlib
import json
from types import SimpleNamespace
import sys

import pytest

from scripts import prepare_jetson_model as prepare
from camera import tensorrt_yolo


@pytest.fixture
def artifact(tmp_path):
    path = tmp_path / 'model with spaces.engine'
    path.write_bytes(b'engine')
    manifest = {
        'model': 'yolo26m', 'precision': 'fp16', 'input_size': 640,
        'output_format': 'xyxy_score_class', 'tensorrt_version': '10.7.0',
        'engine_sha256': hashlib.sha256(b'engine').hexdigest(),
    }
    path.with_suffix('.json').write_text(json.dumps(manifest))
    return path


def test_existing_valid_engine_runs_gpu_check_and_skips_export_and_build(artifact, monkeypatch):
    events = []
    before = artifact.stat().st_mtime_ns
    monkeypatch.setattr(tensorrt_yolo, 'load_tensorrt', lambda: SimpleNamespace(__version__='10.7.0'))
    def detector(path):
        assert path == artifact
        events.append('gpu-check')
        return SimpleNamespace(close=lambda: events.append('close'))
    monkeypatch.setattr(tensorrt_yolo, 'TensorRTYolo', detector)
    monkeypatch.setattr(prepare.subprocess, 'run', lambda *a, **k: pytest.fail('Must not rebuild'))
    # An existing engine does not even require the compiler to be present.
    prepare.main(['--if-needed', '--output', str(artifact), '--trtexec', '/missing/compiler'])
    assert events == ['gpu-check', 'close']
    assert artifact.stat().st_mtime_ns == before


@pytest.mark.parametrize('problem', ['missing_engine', 'missing_manifest', 'bad_json',
                                    'wrong_schema', 'modified_engine', 'wrong_model', 'runtime_changed'])
def test_missing_or_invalid_artifacts_need_preparation_without_gpu_check(artifact, monkeypatch, problem):
    manifest_path = artifact.with_suffix('.json')
    if problem == 'missing_engine':
        artifact.unlink()
    elif problem == 'missing_manifest':
        manifest_path.unlink()
    elif problem == 'bad_json':
        manifest_path.write_text('{')
    elif problem == 'wrong_schema':
        manifest_path.write_text('[]')
    elif problem == 'modified_engine':
        artifact.write_bytes(b'changed')
    else:
        manifest = json.loads(manifest_path.read_text())
        if problem == 'wrong_model':
            manifest['model'] = 'yolo26s'
        else:
            manifest['tensorrt_version'] = '10.6.0'
        manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(tensorrt_yolo, 'TensorRTYolo', lambda *a: pytest.fail('Must reject before GPU check'))
    assert not prepare.reuse_prepared_engine(artifact, '10.7.0')


def test_engine_that_cannot_execute_is_rebuilt(artifact, monkeypatch):
    def fail(path):
        raise RuntimeError('Engine is incompatible with this GPU')
    monkeypatch.setattr(tensorrt_yolo, 'TensorRTYolo', fail)
    assert not prepare.reuse_prepared_engine(artifact, '10.7.0')


def test_preparation_builds_missing_engine_then_reuses_it(tmp_path, monkeypatch):
    artifact = tmp_path / 'yolo26m.engine'
    compiler = tmp_path / 'trtexec'
    compiler.touch()
    onnx_path = tmp_path / 'yolo26m.onnx'
    onnx_path.write_bytes(b'onnx')
    def tensor(shape):
        return SimpleNamespace(type=SimpleNamespace(tensor_type=SimpleNamespace(
            shape=SimpleNamespace(dim=[SimpleNamespace(dim_value=n) for n in shape]))))
    graph = SimpleNamespace(
        metadata_props=[SimpleNamespace(key='description', value='Ultralytics YOLO26m'),
                        SimpleNamespace(key='args', value="{'nms': False}")],
        graph=SimpleNamespace(input=[tensor([1, 3, 640, 640])], output=[tensor([1, 300, 6])]))
    monkeypatch.setitem(sys.modules, 'onnx', SimpleNamespace(load=lambda path: graph))
    monkeypatch.setattr(tensorrt_yolo, 'load_tensorrt', lambda: SimpleNamespace(__version__='10.7.0'))
    builds, executions = [], []
    def build(command, check):
        assert check is True and '--fp16' in command
        builds.append(command)
        output = next(a.split('=', 1)[1] for a in command if a.startswith('--saveEngine='))
        from pathlib import Path
        Path(output).write_bytes(b'compiled engine')
    def detector(path):
        tensorrt_yolo.validate_manifest(path)
        executions.append(path)
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(prepare.subprocess, 'run', build)
    monkeypatch.setattr(tensorrt_yolo, 'TensorRTYolo', detector)
    arguments = ['--if-needed', '--output', str(artifact), '--trtexec', str(compiler), '--onnx', str(onnx_path)]
    prepare.main(arguments)
    assert artifact.read_bytes() == b'compiled engine'
    assert artifact.with_suffix('.json').is_file()
    assert len(builds) == 1 and len(executions) == 1
    prepare.main(arguments)
    assert len(builds) == 1 and len(executions) == 2
    assert not list(tmp_path.glob('.jetson-build-*'))
