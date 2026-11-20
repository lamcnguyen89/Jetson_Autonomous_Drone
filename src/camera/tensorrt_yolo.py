"""YOLO26m on JetPack TensorRT 10, with explicit CUDA memory and no CPU fallback.

This module knows about pixels, tensors and GPU execution, not person identity,
depth or flight commands. Its interface is infer(BGR frame) -> person boxes in
that frame's pixel coordinates. JetsonCamera adds tracking and 3D measurement.

Calling TensorRT directly allows inference on CUDA while export/tracker tooling
uses a CPU PyTorch wheel. Engine compilation belongs to the preparation script;
normal startup only validates, loads and warms up an existing engine.
"""

import ctypes
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import sys

import cv2
import numpy as np

logger = logging.getLogger(__name__)
INPUT_SIZE = 640


def load_tensorrt():
    """Use JetPack's matching native bindings without exposing all system packages."""
    try:
        import tensorrt
        return tensorrt
    except ModuleNotFoundError as error:
        if error.name != 'tensorrt':
            raise
    # Load only JetPack's TensorRT package, keeping the venv isolated from other
    # system packages (notably the system NumPy/OpenCV).
    package = Path('/usr/lib/python3.10/dist-packages/tensorrt/__init__.py')
    if sys.version_info[:2] != (3, 10) or not package.is_file():
        raise RuntimeError('Install JetPack python3-libnvinfer for this Python version')
    spec = importlib.util.spec_from_file_location('tensorrt', package,
                                                submodule_search_locations=[str(package.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules['tensorrt'] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop('tensorrt', None)
        raise
    return module


def validate_manifest(path):
    """Check the model/output contract and pair the engine with its build metadata.

    A file's presence is insufficient: it could be from a different model or a
    partial copy. Checksums detect mismatched artifacts; deserialization and
    warmup separately establish runtime/GPU compatibility.
    """
    if not path.is_file() or not path.with_suffix('.json').is_file():
        raise FileNotFoundError('Missing YOLO26m engine or manifest; run scripts/prepare_jetson_model.py')
    manifest = json.loads(path.with_suffix('.json').read_text())
    if not isinstance(manifest, dict):
        raise ValueError('Invalid YOLO26m manifest; rebuild the engine')
    if (manifest.get('model') != 'yolo26m' or manifest.get('input_size') != INPUT_SIZE
            or manifest.get('output_format') != 'xyxy_score_class'
            or manifest.get('precision') != 'fp16'):
        raise ValueError('Expected YOLO26m FP16 engine; run scripts/prepare_jetson_model.py')
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get('engine_sha256'):
        raise ValueError('Engine does not match its manifest; rebuild the YOLO26m engine')
    return manifest


def preprocess(frame):
    """BGR -> letterboxed, normalized RGB NCHW, retaining exact inverse geometry."""
    height, width = frame.shape[:2]
    scale = min(INPUT_SIZE / width, INPUT_SIZE / height)
    # Preserve aspect ratio with padding so people are not stretched to a square.
    resized_width, resized_height = round(width * scale), round(height * scale)
    left = (INPUT_SIZE - resized_width) // 2
    top = (INPUT_SIZE - resized_height) // 2
    image = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, dtype=np.uint8)
    image[top:top + resized_height, left:left + resized_width] = cv2.resize(
        frame, (resized_width, resized_height))
    tensor = np.ascontiguousarray(image[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32)
    # OpenCV uses BGR/HWC; the exported model expects RGB with dimensions
    # [batch, channels, height, width] and values between zero and one.
    tensor /= 255.0
    return tensor, (resized_width / width, resized_height / height, left, top)


def decode(output, frame_shape, transform, threshold=0.1):
    """Decode the end-to-end head, returning person xyxy/conf/class in image pixels."""
    rows = np.asarray(output).reshape(-1, 6)
    # The prepared end-to-end head already outputs [x1,y1,x2,y2,score,class].
    # COCO class 0 is person. Keep low scores here for tracker association;
    # JetsonCamera applies the stronger flight-facing confidence threshold.
    valid = np.isfinite(rows).all(axis=1) & (rows[:, 4] >= threshold) & (rows[:, 5] == 0)
    rows = rows[valid].astype(np.float32, copy=True)
    sx, sy, left, top = transform
    # Undo exactly the padding and rounded resize used above. Depth projection
    # later samples the original image, so model-space boxes cannot be used as-is.
    rows[:, [0, 2]] = (rows[:, [0, 2]] - left) / sx
    rows[:, [1, 3]] = (rows[:, [1, 3]] - top) / sy
    rows[:, [0, 2]] = rows[:, [0, 2]].clip(0, frame_shape[1])
    rows[:, [1, 3]] = rows[:, [1, 3]].clip(0, frame_shape[0])
    return rows[(rows[:, 2] > rows[:, 0]) & (rows[:, 3] > rows[:, 1])]


class CudaRuntime:
    """Small CUDA runtime binding for the memory/stream operations we need.

    Explicit ctypes signatures preserve pointer widths on this 64-bit platform.
    All nonzero CUDA results become worker errors rather than stale outputs.
    """
    def __init__(self):
        self.lib = ctypes.CDLL('libcudart.so.12')
        pointer = ctypes.c_void_p
        signatures = {
            'cudaSetDevice': [ctypes.c_int],
            'cudaMalloc': [ctypes.POINTER(pointer), ctypes.c_size_t],
            'cudaFree': [pointer],
            'cudaStreamCreate': [ctypes.POINTER(pointer)],
            'cudaStreamDestroy': [pointer],
            'cudaStreamSynchronize': [pointer],
            'cudaMemcpyAsync': [pointer, pointer, ctypes.c_size_t, ctypes.c_int, pointer],
        }
        for name, arguments in signatures.items():
            getattr(self.lib, name).argtypes = arguments
            getattr(self.lib, name).restype = ctypes.c_int
        self.call('cudaSetDevice', 0)

    def call(self, name, *args):
        status = getattr(self.lib, name)(*args)
        if status != 0:
            raise RuntimeError(f'{name} failed with CUDA error {status}')


class TensorRTYolo:
    """Own one engine, execution context, CUDA stream and reusable I/O buffers.

    One perception worker calls infer() sequentially. This instance and its
    buffers are not designed for concurrent inference from multiple threads.
    """
    def __init__(self, path):
        path = Path(path)
        self.manifest = validate_manifest(path)
        self.trt = load_tensorrt()
        if self.trt.__version__.split('.')[0] != '10':
            raise RuntimeError('This backend requires JetPack TensorRT 10.x')
        self.cuda = CudaRuntime()
        self.stream = ctypes.c_void_p()
        self.allocations = []
        self.logger = self.trt.Logger(self.trt.Logger.WARNING)
        self.runtime = self.trt.Runtime(self.logger)
        self.engine = self.runtime.deserialize_cuda_engine(path.read_bytes())
        if self.engine is None:
            raise RuntimeError('Cannot load engine on this Jetson/TensorRT version; rebuild it')
        self.context = self.engine.create_execution_context()
        if self.context is None:
            raise RuntimeError('Cannot create TensorRT execution context')
        try:
            names = [self.engine.get_tensor_name(i) for i in range(self.engine.num_io_tensors)]
            inputs = [n for n in names if self.engine.get_tensor_mode(n) == self.trt.TensorIOMode.INPUT]
            outputs = [n for n in names if self.engine.get_tensor_mode(n) == self.trt.TensorIOMode.OUTPUT]
            if len(inputs) != 1 or len(outputs) != 1:
                raise ValueError('Expected one YOLO26m input and one end-to-end output')
            self.input_name, self.output_name = inputs[0], outputs[0]
            if tuple(self.engine.get_tensor_shape(self.input_name)) != (1, 3, INPUT_SIZE, INPUT_SIZE):
                raise ValueError('Expected static batch=1, 640x640 input')
            if tuple(self.engine.get_tensor_shape(self.output_name)) != (1, 300, 6):
                raise ValueError('Expected YOLO26m end-to-end output (1, 300, 6)')
            self.buffers = {}
            # Static batch-one shapes let us allocate once and reuse memory for
            # every frame, keeping per-frame allocation overhead predictable.
            self.cuda.call('cudaStreamCreate', ctypes.byref(self.stream))
            for name in names:
                dtype = np.dtype(self.trt.nptype(self.engine.get_tensor_dtype(name)))
                if dtype not in (np.dtype('float32'), np.dtype('float16')):
                    raise ValueError(f'Unsupported tensor dtype: {dtype}')
                host = np.empty(tuple(self.engine.get_tensor_shape(name)), dtype=dtype)
                device = ctypes.c_void_p()
                self.cuda.call('cudaMalloc', ctypes.byref(device), host.nbytes)
                self.allocations.append(device)
                self.buffers[name] = (host, device)
                if not self.context.set_tensor_address(name, device.value):
                    raise RuntimeError(f'Cannot bind TensorRT tensor {name}')
            # Warm up before announcing readiness or opening the camera.
            for _ in range(3):
                self.infer(np.zeros((360, 640, 3), dtype=np.uint8))
        except Exception:
            self.close()
            raise
        logger.info('Inference ready: model=YOLO26m backend=TensorRT version=%s device=GPU:0 precision=FP16',
                    self.trt.__version__)

    def infer(self, frame):
        """Execute one frame and return decoded boxes only after GPU work completes."""
        tensor, transform = preprocess(frame)
        host_input, device_input = self.buffers[self.input_name]
        host_output, device_output = self.buffers[self.output_name]
        np.copyto(host_input, tensor, casting='unsafe')
        # Copy input -> execute -> copy output on the same stream, preserving
        # order. CUDA copy kinds 1 and 2 mean host-to-device and device-to-host.
        self.cuda.call('cudaMemcpyAsync', device_input, ctypes.c_void_p(host_input.ctypes.data),
                       host_input.nbytes, 1, self.stream)
        if not self.context.execute_async_v3(self.stream.value):
            raise RuntimeError('TensorRT GPU execution failed')
        self.cuda.call('cudaMemcpyAsync', ctypes.c_void_p(host_output.ctypes.data), device_output,
                       host_output.nbytes, 2, self.stream)
        # NumPy must not inspect the output until the asynchronous copy finishes.
        self.cuda.call('cudaStreamSynchronize', self.stream)
        return decode(host_output, frame.shape, transform)

    def close(self):
        # Finish outstanding work before releasing the context, buffers or stream.
        if self.stream.value:
            self.cuda.call('cudaStreamSynchronize', self.stream)
        self.context = None
        self.engine = None
        self.runtime = None
        for allocation in self.allocations:
            self.cuda.call('cudaFree', allocation)
        self.allocations.clear()
        if self.stream.value:
            self.cuda.call('cudaStreamDestroy', self.stream)
            self.stream = ctypes.c_void_p()
