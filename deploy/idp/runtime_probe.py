"""Small real CUDA kernel probe. It never loads documents or model weights."""
import importlib.metadata
import json
import resource
import time

resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (60, 61))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
started = time.monotonic()
try:
    import paddle
    from paddleocr import PaddleOCRVL

    if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() != 1:
        raise RuntimeError("CUDA_DEVICE_UNAVAILABLE")
    paddle.set_device("gpu:0")
    tensor = paddle.to_tensor([1.0, 2.0], dtype="float32")
    if paddle.add(tensor, tensor).numpy().tolist() != [2.0, 4.0]:
        raise RuntimeError("CUDA_KERNEL_MISMATCH")
    result = {"ready": True, "paddle_version": paddle.__version__,
        "paddleocr_version": importlib.metadata.version("paddleocr"),
        "pipeline_api": PaddleOCRVL.__name__,
        "cuda_version": paddle.version.cuda(), "device": paddle.device.cuda.get_device_name(0),
        "kernel_probe": "PASS", "model_inference": False,
        "elapsed_seconds": round(time.monotonic() - started, 4)}
except (ImportError, RuntimeError, ValueError, OSError, AttributeError, TypeError) as error:
    result = {"ready": False, "kernel_probe": "FAIL", "model_inference": False,
              "error_type": type(error).__name__, "elapsed_seconds": round(time.monotonic() - started, 4)}
print(json.dumps(result))
