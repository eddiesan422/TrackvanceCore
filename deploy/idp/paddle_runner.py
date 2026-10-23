"""Disposable GPU recognizer: only /models read-only and this private /tmp job.

One process per document deliberately releases GPU memory before Qwen starts.
Paddle provides region evidence; block text has block coordinates, never made-up
word coordinates. Table HTML is converted to observed rows using the same real
block rectangle, and its limited spatial resolution remains explicit metadata.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
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
STARTUP_HASHES = {"paddle_runner.py": hashlib.sha256(RUNNER_BYTES).hexdigest(),
                  "pipeline.yml": hashlib.sha256(PIPELINE_BYTES).hexdigest()}


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


def main():
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    import paddle
    from paddleocr import PaddleOCRVL

    started = time.monotonic()
    folder = Path(sys.argv[1])
    request = json.loads((folder / "request.json").read_text())
    if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() != 1:
        raise RuntimeError("GPU_REQUIRED")
    pipeline = PaddleOCRVL(pipeline_version="v1.6", device="gpu:0", paddlex_config=captured_pipeline(folder),
        vl_rec_model_dir="/models/paddle", layout_detection_model_dir="/models/layout",
        use_doc_orientation_classify=False, use_doc_unwarping=False,
        vl_rec_max_concurrency=1, use_queues=False)
    lines, geometries, raw_pages = [], {}, []
    regions = request.get("regions", [])
    for page_number, result, crop_box, full_size in predictions(pipeline, folder / request["filename"], regions):
        if page_number > 50:
            raise ValueError("DOCUMENT_PAGE_LIMIT")
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
    raw_hash = hashlib.sha256(json.dumps(raw_pages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
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
    (folder / "result.json").write_text(json.dumps(output, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    main()
