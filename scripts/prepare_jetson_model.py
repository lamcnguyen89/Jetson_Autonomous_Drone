#!/usr/bin/env python3
"""Export official YOLO26m and build an FP16 engine on the target Jetson.

No camera, flight-controller connection, service changes or flight commands.

Artifact chain: pretrained yolo26m.pt -> portable ONNX graph -> GPU-specific
TensorRT engine plus checksum manifest. The installer calls --if-needed so
repeat installs validate/reuse an engine instead of repeating the long build.
The flight service never downloads weights or compiles its model at startup.
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def reuse_prepared_engine(output, runtime_version):
    """Reuse only a complete, unmodified engine that executes on this GPU/runtime."""
    from camera.tensorrt_yolo import TensorRTYolo, validate_manifest
    try:
        manifest = validate_manifest(output)
        if manifest.get('tensorrt_version') != runtime_version:
            raise ValueError('TensorRT version changed since the engine was built')
        # Construction includes GPU warmup. A valid checksum alone cannot tell
        # us whether a copied engine can execute on this Jetson/runtime.
        detector = TensorRTYolo(output)
        detector.close()
    except (OSError, ValueError, RuntimeError) as error:
        print(f'YOLO26m engine requires preparation: {error}', flush=True)
        return False
    print(f'YOLO26m TensorRT GPU engine already prepared: {output}', flush=True)
    return True


def main(argv=None):
    """Validate or prepare the Jetson TensorRT engine required by the GPU backend."""
    from config import inference_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--onnx', type=Path, help='Previously exported YOLO26m ONNX, skip PyTorch export')
    parser.add_argument('--output', type=Path, default=inference_settings.jetson_model_path)
    parser.add_argument('--trtexec', type=Path, default=Path('/usr/src/tensorrt/bin/trtexec'))
    parser.add_argument('--if-needed', action='store_true',
                        help='Reuse an existing engine after checksum, runtime and GPU execution checks')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.suffix != '.engine':
        parser.error('--output must end in .engine')
    from camera.tensorrt_yolo import INPUT_SIZE, load_tensorrt, TensorRTYolo
    trt = load_tensorrt()
    if not trt.__version__.startswith('10.'):
        parser.error('Build with the JetPack TensorRT 10 runtime')
    if args.if_needed and reuse_prepared_engine(output, trt.__version__):
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    if not args.trtexec.is_file():
        parser.error('JetPack trtexec is missing; install libnvinfer-bin')
    # Bound CPU export work while the host may have other services running.
    os.environ.setdefault('OMP_NUM_THREADS', '2')
    os.environ.setdefault('YOLO_AUTOINSTALL', 'false')
    if args.onnx is None:
        import torch
        from ultralytics import YOLO
        torch.set_num_threads(2)
        # Ultralytics downloads the official checkpoint when missing, or loads
        # the existing local weights. This is pretrained inference, not training.
        model = YOLO(str(output.parent / 'yolo26m.pt'))
        config = model.model.yaml
        if config.get('scale') != 'm' or 'yolo26' not in str(config.get('yaml_file', '')).lower():
            raise ValueError('Expected the official YOLO26m detection model')
        # Match the runtime decoder's fixed contract: batch one, 640-square,
        # end-to-end xyxy/score/class output. CPU export avoids requiring a CUDA
        # PyTorch wheel; TensorRT supplies the actual GPU inference runtime.
        onnx_path = Path(model.export(format='onnx', device='cpu', imgsz=INPUT_SIZE,
                                     batch=1, dynamic=False, simplify=False, opset=17, nms=False))
        del model
    else:
        onnx_path = args.onnx.resolve()
    # Verify model identity/shape/format even when accepting a pre-exported ONNX.
    import onnx
    graph = onnx.load(str(onnx_path))
    metadata = {item.key: item.value for item in graph.metadata_props}
    model_args = ast.literal_eval(metadata.get('args', '{}'))
    if ('yolo26m' not in metadata.get('description', '').lower()
            or model_args.get('nms') is not False):
        raise ValueError('ONNX must be an official YOLO26m export with nms=False')
    input_shape = [d.dim_value for d in graph.graph.input[0].type.tensor_type.shape.dim]
    output_shape = [d.dim_value for d in graph.graph.output[0].type.tensor_type.shape.dim]
    if input_shape != [1, 3, INPUT_SIZE, INPUT_SIZE] or output_shape != [1, 300, 6]:
        raise ValueError(f'Expected static YOLO26m end-to-end shapes, got {input_shape} / {output_shape}')
    with tempfile.TemporaryDirectory(prefix='.jetson-build-', dir=output.parent) as temporary:
        # Build beside the destination so os.replace can publish each artifact
        # on the same filesystem. A failed build leaves the previous pair intact.
        engine = Path(temporary) / output.name
        # FP16 enables TensorRT's reduced-precision GPU kernels. The engine's I/O
        # may still be FP32; the runtime queries tensor dtypes instead of guessing.
        subprocess.run([str(args.trtexec), f'--onnx={onnx_path}', '--fp16',
                        f'--saveEngine={engine}', '--skipInference', '--memPoolSize=workspace:2048'],
                       check=True)
        manifest = {
            'model': 'yolo26m', 'input_size': INPUT_SIZE, 'precision': 'fp16',
            'output_format': 'xyxy_score_class', 'tensorrt_version': trt.__version__,
            'engine_sha256': hashlib.sha256(engine.read_bytes()).hexdigest(),
            'onnx_sha256': hashlib.sha256(onnx_path.read_bytes()).hexdigest(),
        }
        engine.with_suffix('.json').write_text(json.dumps(manifest, indent=2) + '\n')
        # Prove the generated engine loads and executes on GPU before installing it.
        detector = TensorRTYolo(engine)
        detector.close()
        # The engine and manifest are replaced separately. Runtime checksum
        # validation rejects a mismatched pair if a reader catches that window.
        os.replace(engine, output)
        os.replace(engine.with_suffix('.json'), output.with_suffix('.json'))
    print(f'YOLO26m TensorRT GPU engine ready: {output}')


if __name__ == '__main__':
    main()
