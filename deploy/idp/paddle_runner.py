"""Disposable GPU recognizer: only /models read-only and this private /tmp job.

One process per document deliberately releases GPU memory before Qwen starts.
Paddle provides region evidence; block text has block coordinates, never made-up
word coordinates. Table HTML is converted to observed rows using the same real
block rectangle, and its limited spatial resolution remains explicit metadata.
"""
from __future__ import annotations

import hashlib
import importlib.abc
import importlib.metadata
import importlib.util
import json
import math
import os
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
RUNNER_BYTES = Path(__file__).read_bytes()
PIPELINE_BYTES = (APP_ROOT / "pipeline.yml").read_bytes()
DIAGNOSTIC_BYTES = (APP_ROOT / "idp_observability.py").read_bytes()
class CapturedDiagnostics(importlib.abc.SourceLoader):
    def get_filename(self, _fullname):
        return str(APP_ROOT / "idp_observability.py")

    def get_data(self, path):
        if path != self.get_filename(""):
            raise OSError("FROZEN_MODULE_PATH")
        return DIAGNOSTIC_BYTES


_SPEC = importlib.util.spec_from_loader("trackvance_idp_child_diagnostics", CapturedDiagnostics())
_DIAGNOSTICS_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _DIAGNOSTICS_MODULE
_SPEC.loader.exec_module(_DIAGNOSTICS_MODULE)
DIAGNOSTICS = vars(_DIAGNOSTICS_MODULE)
STARTUP_HASHES = {"paddle_runner.py": hashlib.sha256(RUNNER_BYTES).hexdigest(),
                  "pipeline.yml": hashlib.sha256(PIPELINE_BYTES).hexdigest(),
                  "idp_observability.py": hashlib.sha256(DIAGNOSTIC_BYTES).hexdigest()}


def startup_provenance():
    return {"capture": "PROCESS_STARTUP", "files_sha256": dict(STARTUP_HASHES)}


def captured_pipeline(folder):
    """Give Paddle the exact configuration bytes recorded by this child."""
    path = folder / "pipeline.snapshot.yml"
    path.write_bytes(PIPELINE_BYTES)
    return str(path)


class TableText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.cells, self.current = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag in {"td", "th"}:
            self.current = []
        elif tag == "br":
            self.current.append(" ")

    def handle_data(self, text):
        self.current.append(text)

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self.cells.append("".join(self.current).strip())
            self.current = []
        elif tag == "tr" and self.cells:
            self.rows.append(" | ".join(self.cells))
            self.cells = []


def group_spotted(words, page):
    """Group model-supplied polygons by measured baselines; never split boxes."""
    groups = []
    for word in sorted(words, key=lambda item: ((item["region"][1] + item["region"][3]) / 2, item["region"][0])):
        center = (word["region"][1] + word["region"][3]) / 2
        height = word["region"][3] - word["region"][1]
        selected = None
        for group in reversed(groups[-10:]):
            other = group[0]["region"]
            if abs(center - (other[1] + other[3]) / 2) <= max(height, other[3] - other[1]) * .45:
                selected = group
                break
        if selected is None:
            groups.append([word])
        else:
            selected.append(word)
    output = []
    for group in groups:
        group.sort(key=lambda word: word["region"][0])
        output.append({"text": " ".join(word["text"] for word in group), "page": page,
            "region": [min(word["region"][0] for word in group), min(word["region"][1] for word in group),
                       max(word["region"][2] for word in group), max(word["region"][3] for word in group)],
            "words": group, "method": "OCR", "confidence": None, "block_label": "spotting"})
    return output


def predictions(pipeline, source, regions):
    if not regions:
        for page, result in enumerate(pipeline.predict(str(source)), 1):
            yield page, result, [0, 0, 1, 1], None
        return
    import numpy as np
    from PIL import Image

    pdf = None
    if source.suffix == ".pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(source))
    try:
        for target in regions:
            page, box = target.get("page"), target.get("region")
            if (type(page) is not int or not 1 <= page <= 50 or not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                    or box[0] >= box[2] or box[1] >= box[3]):
                raise ValueError("DOCUMENT_REGION_INVALID")
            if pdf is not None:
                if page > len(pdf):
                    raise ValueError("DOCUMENT_PAGE_LIMIT")
                pdf_page = pdf[page - 1]
                size = pdf_page.get_size()
                bitmap = pdf_page.render(scale=min(2, 4000 / max(size)))
                image = bitmap.to_pil().copy()
                bitmap.close()
                pdf_page.close()
            else:
                if page != 1:
                    raise ValueError("DOCUMENT_PAGE_LIMIT")
                image = Image.open(source).convert("RGB")
            width, height = image.size
            if width * height > 20_000_000:
                raise ValueError("DOCUMENT_RESOLUTION_LIMIT")
            pixel_box = [int(box[0] * width), int(box[1] * height), max(1, int(box[2] * width)), max(1, int(box[3] * height))]
            if pixel_box[0] >= pixel_box[2] or pixel_box[1] >= pixel_box[3]:
                raise ValueError("DOCUMENT_REGION_INVALID")
            crop = image.crop(pixel_box)
            actual = [pixel_box[0] / width, pixel_box[1] / height, pixel_box[2] / width, pixel_box[3] / height]
            for result in pipeline.predict(np.asarray(crop)[:, :, ::-1], use_layout_detection=False,
                    prompt_label="spotting", max_new_tokens=4096):
                yield page, result, actual, (width, height)
            crop.close()
            image.close()
    finally:
        if pdf is not None:
            pdf.close()


def atomic_json(path, value):
    """Publish one child checkpoint only after all referenced bytes are durable."""
    pending = path.with_suffix(path.suffix + ".pending")
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    with pending.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def file_record(path, folder, kind, **metadata):
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("SOURCE_INTEGRITY")
    if before.st_size > 16 * 1024**2:
        raise ValueError("PADDLE_RESULT_LIMIT")
    return {"path": path.relative_to(folder).as_posix(), "kind": kind,
        "sha256": digest, "bytes": before.st_size, "metadata": metadata}


def source_page_metadata(source):
    """Read intrinsic page geometry; never infer a cell box from a table box."""
    if source.suffix == ".pdf":
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(source))
        try:
            if not 1 <= len(document) <= 50:
                raise ValueError("DOCUMENT_PAGE_LIMIT")
            pages = []
            for number in range(len(document)):
                page = document[number]
                try:
                    width, height = page.get_size()
                    pages.append({"page": number + 1, "width": width, "height": height,
                        "unit": "PDF_POINT", "rotation_degrees": page.get_rotation()})
                finally:
                    page.close()
            return pages
        finally:
            document.close()
    from PIL import Image
    with Image.open(source) as image:
        width, height = image.size
    if not 0 < width * height <= 20_000_000:
        raise ValueError("DOCUMENT_RESOLUTION_LIMIT")
    return [{"page": 1, "width": width, "height": height, "unit": "PIXEL", "rotation_degrees": 0}]


def render_pages(folder, request, diagnostic):
    """Bounded render-only child. It never imports Paddle or loads a model."""
    from PIL import Image
    source = folder / request["filename"]
    diagnostic.mark("OPEN_SOURCE")
    metadata = source_page_metadata(source)
    wanted = request.get("pages") or list(range(1, len(metadata) + 1))
    if (not isinstance(wanted, list) or not wanted or len(set(wanted)) != len(wanted)
            or any(type(page) is not int or not 1 <= page <= len(metadata) for page in wanted)):
        raise ValueError("DOCUMENT_PAGE_LIMIT")
    manifest = {"version": 1, "source_sha256": request["source_sha256"],
        "expected_pages": len(metadata), "pages": [], "status": "INCOMPLETE"}
    pdf = None
    if source.suffix == ".pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(source))
    try:
        for number in wanted:
            diagnostic.mark("RENDER_PAGE", number)
            page_meta = metadata[number - 1]
            scale = min(150 / 72, 2000 / max(page_meta["width"], page_meta["height"])) if pdf else min(1, 2000 / max(page_meta["width"], page_meta["height"]))
            if pdf:
                page = pdf[number - 1]
                try:
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil().convert("RGB")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            else:
                with Image.open(source) as opened:
                    image = opened.convert("RGB")
                if scale < 1:
                    resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
                    image.close()
                    image = resized
            try:
                if not 0 < image.width * image.height <= 4_000_000 or max(image.size) > 2200:
                    raise ValueError("DOCUMENT_RESOLUTION_LIMIT")
                name = f"image-{number:03}.png"
                pending = folder / (name + ".pending")
                image.save(pending, format="PNG")
                with pending.open("r+b") as stream:
                    os.fsync(stream.fileno())
                pending.replace(folder / name)
                geometry = {"width": image.width, "height": image.height, "unit": "PIXEL",
                    "rotation_degrees": page_meta["rotation_degrees"],
                    "transformations": [{"operation": "RENDER_RGB", "scale": scale,
                        "source_unit": page_meta["unit"], "source_width": page_meta["width"], "source_height": page_meta["height"]}]}
                manifest["pages"].append({"page": number, "page_geometry": geometry,
                    "artifacts": [file_record(folder / name, folder, "PAGE_IMAGE_PNG")]})
                atomic_json(folder / "pages.manifest.json", manifest)
            finally:
                image.close()
    finally:
        if pdf:
            pdf.close()
    manifest["status"] = "COMPLETE"
    atomic_json(folder / "pages.manifest.json", manifest)
    atomic_json(folder / "result.json", {"source_sha256": request["source_sha256"],
        "expected_pages": len(metadata), "emitted_pages": wanted, "engine": {"runtime_provenance": {"paddle_runner": startup_provenance()}}})
    diagnostic.mark("DONE")


def structured_pages(pipeline, folder, request, diagnostic):
    """Save official results before any field mapping or table text projection."""
    source = folder / request["filename"]
    expected = source_page_metadata(source)
    manifest = {"version": 1, "source_sha256": request["source_sha256"],
        "expected_pages": len(expected), "pages": [], "status": "INCOMPLETE"}
    diagnostic.mark("PREDICT_NEXT")
    for ordinal, result in enumerate(pipeline.predict_iter(str(source)), 1):
        if ordinal > len(expected):
            raise ValueError("DOCUMENT_PAGE_LIMIT")
        diagnostic.mark("PERSIST_ORIGINAL_PAGE", ordinal)
        directory = folder / f"page-{ordinal:03}"
        directory.mkdir()
        pending = directory / "official.pending.json"
        result.save_to_json(str(pending), ensure_ascii=False)
        with pending.open("r+b") as stream:
            os.fsync(stream.fileno())
        pending.replace(directory / "official.json")
        raw = json.loads((directory / "official.json").read_bytes())
        raw = raw.get("res", raw) if isinstance(raw, dict) else {}
        artifacts = [file_record(directory / "official.json", folder, "PADDLE_PAGE_JSON")]
        for index, block in enumerate(raw.get("parsing_res_list", [])):
            if isinstance(block, dict) and block.get("block_label") == "table" and isinstance(block.get("block_content"), str):
                html = directory / f"table-{index:03}.html"
                with html.open("xb") as stream:
                    stream.write(block["block_content"].encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                artifacts.append(file_record(html, folder, "PADDLE_TABLE_HTML", block_index=index))
        width, height = raw.get("width"), raw.get("height")
        width = width[0] if isinstance(width, list) and width else width
        height = height[0] if isinstance(height, list) and height else height
        geometry = {"width": width, "height": height, "unit": "PIXEL",
            "rotation_degrees": expected[ordinal - 1]["rotation_degrees"], "transformations": [],
            "source_page_geometry": expected[ordinal - 1]}
        manifest["pages"].append({"page": ordinal, "page_geometry": geometry, "artifacts": artifacts})
        atomic_json(folder / "pages.manifest.json", manifest)
        if raw.get("page_index") is not None and raw["page_index"] != ordinal - 1:
            raise ValueError("DOCUMENT_PAGE_LIMIT")
        diagnostic.mark("PREDICT_NEXT")
    if len(manifest["pages"]) != len(expected):
        raise ValueError("DOCUMENT_PAGE_LIMIT")
    manifest["status"] = "COMPLETE"
    atomic_json(folder / "pages.manifest.json", manifest)
    atomic_json(folder / "result.json", {"source_sha256": request["source_sha256"],
        "expected_pages": len(expected), "emitted_pages": [page["page"] for page in manifest["pages"]],
        "engine": {"paddle_model": "PaddleOCR-VL-1.6", "paddle_version": importlib.metadata.version("paddlepaddle-gpu"),
            "paddleocr_version": importlib.metadata.version("paddleocr"),
            "evidence_resolution": "STRUCTURED_NATIVE", "pipeline_sha256": hashlib.sha256(PIPELINE_BYTES).hexdigest(),
            "generation_overrides": {}, "requested_device": "gpu:0",
            "runtime_provenance": {"paddle_runner": startup_provenance()}}})
    diagnostic.mark("DONE")


def main():
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    diagnostic = DIAGNOSTICS["make_child_diagnostics"](Path(sys.argv[1]), STARTUP_HASHES,
        enable_stacks=os.environ.get("TRACKVANCE_IDP_STACK_DIAGNOSTICS") == "1")
    try:
        diagnostic.arm()
        folder = Path(sys.argv[1])
        request = json.loads((folder / "request.json").read_text())
        diagnostic.bind_source(request["source_sha256"])
        if request.get("operation") == "RENDER":
            render_pages(folder, request, diagnostic)
            return
        diagnostic.mark("IMPORT_PADDLE")
        import paddle
        diagnostic.mark("IMPORT_PADDLEOCR")
        from paddleocr import PaddleOCRVL

        started = time.monotonic()
        folder = Path(sys.argv[1])
        request = json.loads((folder / "request.json").read_text())
        diagnostic.bind_source(request["source_sha256"])
        if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() != 1:
            raise RuntimeError("GPU_REQUIRED")
        diagnostic.mark("CREATE_PIPELINE")
        pipeline = PaddleOCRVL(pipeline_version="v1.6", device="gpu:0", paddlex_config=captured_pipeline(folder),
            vl_rec_model_dir="/models/paddle", layout_detection_model_dir="/models/layout",
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            vl_rec_max_concurrency=1, use_queues=False)
        if request.get("operation") == "STRUCTURED":
            structured_pages(pipeline, folder, request, diagnostic)
            return
        lines, geometries, raw_pages = [], {}, []
        regions = request.get("regions", [])
        diagnostic.mark("PREDICT_NEXT")
        for page_number, result, crop_box, full_size in predictions(pipeline, folder / request["filename"], regions):
            if page_number > 50:
                raise ValueError("DOCUMENT_PAGE_LIMIT")
            diagnostic.mark("POSTPROCESS_PAGE", page_number)
            raw = result.json
            raw = raw.get("res", raw)
            width, height = raw["width"], raw["height"]
            if isinstance(width, list):
                width = width[0]
            if isinstance(height, list):
                height = height[0]
            if width <= 0 or height <= 0 or width * height > 20_000_000:
                raise ValueError("DOCUMENT_RESOLUTION_LIMIT")
            source_width, source_height = full_size or (width, height)
            geometries[str(page_number)] = {"width": source_width, "height": source_height, "unit": "PIXEL"}
            raw_pages.append(raw)
            spotting = raw.get("spotting_res")
            if regions and not isinstance(spotting, dict):
                raise ValueError("DOCUMENT_SPOTTING_UNAVAILABLE")
            if regions and isinstance(spotting, dict):
                words = []
                for text, polygon in zip(spotting.get("rec_texts", []), spotting.get("rec_polys", []), strict=True):
                    if isinstance(text, (list, tuple)):
                        text = text[0]
                    if not isinstance(text, str) or not text.strip() or not polygon:
                        continue
                    xs, ys = [point[0] for point in polygon], [point[1] for point in polygon]
                    normalized = [max(0, min(1, min(xs) / width)), max(0, min(1, min(ys) / height)),
                                  max(0, min(1, max(xs) / width)), max(0, min(1, max(ys) / height))]
                    global_box = [crop_box[0] + normalized[0] * (crop_box[2] - crop_box[0]),
                        crop_box[1] + normalized[1] * (crop_box[3] - crop_box[1]),
                        crop_box[0] + normalized[2] * (crop_box[2] - crop_box[0]),
                        crop_box[1] + normalized[3] * (crop_box[3] - crop_box[1])]
                    if global_box[0] < global_box[2] and global_box[1] < global_box[3]:
                        words.append({"text": text.strip(), "region": global_box, "confidence": None})
                lines.extend(group_spotted(words, page_number))
                diagnostic.mark("PREDICT_NEXT")
                continue
            for block in raw.get("parsing_res_list", []):
                text, box = block.get("block_content", ""), block.get("block_bbox")
                if not isinstance(text, str) or not box or not text.strip():
                    continue
                region = [max(0, min(1, box[0] / width)), max(0, min(1, box[1] / height)),
                          max(0, min(1, box[2] / width)), max(0, min(1, box[3] / height))]
                rows = text.splitlines()
                if block.get("block_label") == "table" and "<table" in text:
                    parser = TableText()
                    parser.feed(text)
                    rows = parser.rows
                for row in rows:
                    if row.strip() and region[0] < region[2] and region[1] < region[3]:
                        lines.append({"text": row.strip(), "page": page_number, "region": region,
                                      "method": "OCR", "confidence": None, "block_label": block.get("block_label")})
            diagnostic.mark("PREDICT_NEXT")
        diagnostic.mark("SERIALIZE_OBSERVATIONS")
        raw_hash = hashlib.sha256(json.dumps(raw_pages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        diagnostic.mark("HASH_MODEL")
        with open("/models/paddle/model.safetensors", "rb") as model_stream:
            model_hash = hashlib.file_digest(model_stream, "sha256").hexdigest()
        output = {"source_sha256": request["source_sha256"], "lines": lines, "page_geometry": geometries,
            "engine": {"paddle_model": "PaddleOCR-VL-1.6", "paddle_version": paddle.__version__,
                "paddleocr_version": importlib.metadata.version("paddleocr"),
                "model_sha256": model_hash,
                "device": paddle.device.cuda.get_device_name(0), "raw_observations_sha256": raw_hash,
                "runtime_provenance": {"paddle_runner": startup_provenance()},
                "evidence_resolution": "MODEL_SPOTTING" if regions else "LAYOUT_BLOCK", "independent_row_coverage": False,
                "phases": {"paddle_seconds": round(time.monotonic() - started, 4)}}}
        diagnostic.mark("WRITE_RESULT")
        (folder / "result.json").write_text(json.dumps(output, ensure_ascii=False, allow_nan=False))
        diagnostic.mark("DONE")
    finally:
        diagnostic.close()


if __name__ == "__main__":
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    main()
