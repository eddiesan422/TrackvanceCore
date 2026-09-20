"""Build the official PDF from its editable Markdown source.

Run with the document-authoring runtime (reportlab and pypdf). Product runtime
dependencies remain unchanged. Candidate PDFs are reviewed before publication.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import shutil
from pathlib import Path

import reportlab
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, CondPageBreak, Flowable, Frame, Image, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

HERE = Path(__file__).resolve().parent
def find_repo() -> Path:
    candidates = [HERE, *HERE.parents, HERE.parent / "Sistema" / "TrackvanceCore"]
    for candidate in candidates:
        if (candidate / "backend/openapi.json").is_file() and (candidate / "frontend/public").is_dir():
            return candidate
    raise RuntimeError("No se encontró TrackvanceCore; use --repo con la raíz del repositorio.")


REPO: Path
SOURCE = HERE / "Trackvance_Core_Especificacion_Tecnica_v1.1.md"
PDF_NAME = "Trackvance_Core_Especificacion_Tecnica_v1.1.pdf"
VERSION = "0.4.0"
EDITION_DATE = "19 septiembre 2026"
ORIGINAL_SHA256 = "82341b3c63710abd996476e1ac9ca453010dcf7918cb3ed7de5d75c4b8b90244"
NAVY = colors.HexColor("#15324B")
TEAL = colors.HexColor("#008B83")
INK = colors.HexColor("#263E50")
MUTED = colors.HexColor("#617787")
PALE = colors.HexColor("#EFF7F7")
LINE = colors.HexColor("#D9E4EB")
WIDTH, HEIGHT = letter
MARGIN = 43
CONTENT = WIDTH - 2 * MARGIN

font_candidates = [
    (Path("C:/Windows/Fonts"), ("arial.ttf", "arialbd.ttf", "ariali.ttf")),
    (Path("/usr/share/fonts/truetype/dejavu"), ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf")),
    (Path(reportlab.__file__).resolve().parent / "fonts", ("Vera.ttf", "VeraBd.ttf", "VeraIt.ttf")),
]
font_root, font_files = next((root, files) for root, files in font_candidates if all((root / f).exists() for f in files))
for name, filename in zip(("Arial", "ArialBold", "ArialItalic"), font_files, strict=True):
    pdfmetrics.registerFont(TTFont(name, str(font_root / filename)))
pdfmetrics.registerFontFamily("Arial", normal="Arial", bold="ArialBold", italic="ArialItalic")

STYLES = {
    "body": ParagraphStyle("body", fontName="Arial", fontSize=9, leading=11.8,
                           textColor=INK, spaceAfter=6),
    "h1": ParagraphStyle("h1", fontName="ArialBold", fontSize=18, leading=22,
                         textColor=NAVY, spaceAfter=12, keepWithNext=True),
    "h2": ParagraphStyle("h2", fontName="ArialBold", fontSize=11, leading=15,
                         textColor=TEAL, spaceBefore=7, spaceAfter=6, keepWithNext=True),
    "cell": ParagraphStyle("cell", fontName="Arial", fontSize=8.3, leading=10.8,
                           textColor=INK),
    "head": ParagraphStyle("head", fontName="ArialBold", fontSize=8.3, leading=10.8,
                           textColor=colors.white),
    "code": ParagraphStyle("code", fontName="Courier", fontSize=7.8, leading=11,
                           textColor=NAVY, leftIndent=8, rightIndent=8, spaceAfter=1),
    "small": ParagraphStyle("small", fontName="Arial", fontSize=8, leading=11,
                            textColor=MUTED, spaceAfter=7),
    "bullet": ParagraphStyle("bullet", fontName="Arial", fontSize=9, leading=11.8,
                             textColor=INK, leftIndent=12, firstLineIndent=-9, spaceAfter=5),
}


def escaped(text: str) -> str:
    text = html.escape(text)
    return re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', text)


def paragraph(text: str, style: str = "body") -> Paragraph:
    return Paragraph(escaped(text), STYLES[style])


def table(rows: list[list[str]], widths: list[float] | None = None) -> Table:
    if widths is None:
        widths = [CONTENT * 0.29, CONTENT * 0.71] if len(rows[0]) == 2 else [CONTENT / len(rows[0])] * len(rows[0])
    cells = [[paragraph(c, "head" if i == 0 else "cell") for c in row] for i, row in enumerate(rows)]
    result = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 0), (-1, 0), 1, TEAL),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, LINE),
    ]))
    result.spaceAfter = 9
    return result


class Diagram(Flowable):
    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind
        self.width = CONTENT
        self.height = 188 if kind != "exceptions" else 195

    def box(self, x, y, w, h, title, detail=""):
        c = self.canv
        c.setFillColor(PALE)
        c.setStrokeColor(LINE)
        c.roundRect(x, y, w, h, 5, fill=1, stroke=1)
        p = Paragraph(escaped(title), ParagraphStyle("box", fontName="ArialBold", fontSize=9, leading=12, alignment=TA_CENTER, textColor=NAVY))
        _, ph = p.wrap(w - 12, h)
        p.drawOn(c, x + 6, y + h - ph - 9)
        if detail:
            p = Paragraph(escaped(detail), ParagraphStyle("detail", fontName="Arial", fontSize=7.7, leading=10, alignment=TA_CENTER, textColor=MUTED))
            _, ph = p.wrap(w - 12, h)
            p.drawOn(c, x + 6, y + 7)

    def arrow(self, x1, y1, x2, y2):
        c = self.canv
        c.setStrokeColor(TEAL)
        c.setLineWidth(1.2)
        c.line(x1, y1, x2, y2)
        angle = math.atan2(y2 - y1, x2 - x1)
        for offset in (-0.5, 0.5):
            c.line(x2, y2, x2 - 6 * math.cos(angle + offset), y2 - 6 * math.sin(angle + offset))

    def draw(self):
        w = CONTENT
        if self.kind == "logical":
            self.box(0, 139, w, 43, "React / API / RBAC", "Interacción, identidad y contratos HTTP")
            self.box(0, 75, w, 45, "Servicios y semántica de módulos", "Datasets · Intake · ReconOps · Sentinel · Excepciones · Audit")
            for i, title in enumerate(["DatasetSource", "StorageProvider", "ExecutionEngine", "JobQueue"]):
                x = i * (w + 8) / 4
                self.box(x, 8, (w - 24) / 4, 46, title, "Contrato + adaptador")
                self.arrow(x + (w - 24) / 8, 74, x + (w - 24) / 8, 56)
            self.arrow(w / 2, 139, w / 2, 121)
        elif self.kind == "local":
            self.box(0, 124, 122, 48, "Navegador / web", "React + nginx")
            self.box(162, 124, 140, 48, "API FastAPI", "Mismo monolito")
            self.box(346, 124, 180, 48, "Worker local", "Scheduler + Polars / Python")
            self.box(162, 34, 140, 51, "PostgreSQL 16", "Metadata + jobs")
            self.box(346, 34, 180, 51, "FileArtifactStore", "Volumen trackvance_data")
            self.arrow(123, 149, 160, 149)
            self.arrow(232, 124, 232, 87)
            self.arrow(436, 123, 436, 87)
            self.arrow(370, 123, 280, 87)
            self.arrow(280, 123, 370, 87)
        elif self.kind == "exceptions":
            names = ["ABIERTA / ASIGNADA", "EN GESTIÓN", "PENDIENTE DE VALIDACIÓN", "RESUELTA"]
            for i, title in enumerate(names):
                x = i * (w + 12) / 4
                self.box(x, 130, (w - 36) / 4, 51, title)
                if i < 3:
                    self.arrow(x + (w - 36) / 4 + 1, 155, x + (w + 12) / 4 - 2, 155)
            self.box(0, 70, w, 43, "Validación técnica posterior de la misma configuración", "Resolución humana o política automática por caso, deshabilitada por defecto")
            self.box(0, 7, w, 48, "Cierre administrativo / Reapertura", "Descartada, aceptada y no aplica requieren motivo; reabrir exige comentario")
        else:
            self.box(0, 128, w, 48, "Kubernetes / AKS / EKS / OpenShift", "Web + réplicas API y workers del monolito modular")
            titles = [("Metadata", "PostgreSQL administrado"), ("Storage", "S3 / Azure Blob"), ("Jobs / compute", "Redis-Celery / Polars-PySpark")]
            for i, (title, detail) in enumerate(titles):
                self.box(i * (w + 10) / 3, 60, (w - 20) / 3, 49, title, detail)
                self.arrow(i * (w + 10) / 3 + (w - 20) / 6, 127, i * (w + 10) / 3 + (w - 20) / 6, 111)
            self.box(0, 3, w, 42, "OIDC · Secrets Manager · OpenTelemetry · CI/CD", "Terraform + Helm | Infraestructura objetivo")


class SpecDocument(BaseDocTemplate):
    def __init__(self, path: Path):
        super().__init__(str(path), pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=58, bottomMargin=49,
                         title="Trackvance Core - Especificación Técnica v1.1",
                         author="Trackvance Colombia SAS", subject=f"Implementación {VERSION} - {EDITION_DATE}")
        frame = Frame(MARGIN, 49, CONTENT, HEIGHT - 107, leftPadding=0, bottomPadding=0, rightPadding=0, topPadding=0)
        self.addPageTemplates(PageTemplate(id="normal", frames=frame, onPage=self.decorate))

    def decorate(self, c, doc):
        if doc.page == 1:
            return
        c.setFillColor(NAVY)
        c.setFont("ArialBold", 8)
        c.drawString(MARGIN, HEIGHT - 29, "TRACKVANCE CORE")
        c.setFont("Arial", 7.2)
        c.setFillColor(MUTED)
        c.drawRightString(WIDTH - MARGIN, HEIGHT - 29, f"Especificación v1.1 | Implementación {VERSION}")
        c.setStrokeColor(TEAL)
        c.setLineWidth(0.8)
        c.line(MARGIN, HEIGHT - 38, WIDTH - MARGIN, HEIGHT - 38)
        c.setStrokeColor(LINE)
        c.line(MARGIN, 36, WIDTH - MARGIN, 36)
        c.setFont("Arial", 7)
        c.drawString(MARGIN, 24, f"Trackvance Colombia SAS · {EDITION_DATE}")
        c.drawRightString(WIDTH - MARGIN, 24, str(doc.page))

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "h1":
            title = flowable.getPlainText()
            key = "section-" + title.split(".")[0]
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, 0, False)
            self.notify("TOCEntry", (0, title, self.page, key))


def openapi_table():
    spec = json.loads((REPO / "backend/openapi.json").read_text(encoding="utf-8"))
    rows = [["Método", "Ruta", "Operación"]]
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            label = operation.get("summary", "")
            if operation.get("deprecated"):
                label += " (legacy)"
            rows.append([method.upper(), path, label])
    return table(rows, [49, 299, CONTENT - 348])


def build(candidate: Path, results: dict, draft: bool):
    story = []
    logo = REPO / "frontend/public/trackvance-logo.jpg"
    story.append(Spacer(1, 40))
    logo_img = Image(str(logo), width=215, height=115, kind="proportional", hAlign="LEFT")
    story.extend([logo_img, Spacer(1, 32)])
    story.append(Paragraph("Trackvance Core", ParagraphStyle("cover", fontName="ArialBold", fontSize=34, leading=40, textColor=NAVY)))
    story.append(Spacer(1, 14))
    story.append(Paragraph("Especificación técnica v1.1", ParagraphStyle("subtitle", fontName="Arial", fontSize=21, leading=28, textColor=NAVY)))
    story.append(Spacer(1, 25))
    cover_status = "Borrador: certificación consolidada en curso" if draft else "Estado funcional y evidencia de validación"
    story.append(table([[f"IMPLEMENTACIÓN {VERSION} · EVOLUCIÓN FUNCIONAL LOCAL"], [f"{EDITION_DATE} · {cover_status}"]], [CONTENT]))
    story.append(Spacer(1, 18))
    story.append(paragraph("Arquitectura local y evolución a producto", "h2"))
    story.append(paragraph("Monolito modular · Persistencia local · Reglas portables · Evidencia verificable"))
    story.append(Spacer(1, 25))
    story.append(paragraph("Documento oficial de referencia. Describe el estado real de Trackvance Core y distingue las capacidades implementadas de los contratos preparados y la infraestructura objetivo."))
    story.append(paragraph("Trackvance Colombia SAS", "small"))
    story.append(PageBreak())
    story.append(Paragraph("Contenido", ParagraphStyle("toc-title", parent=STYLES["h1"])))
    toc = TableOfContents()
    toc.levelStyles = [ParagraphStyle("toc", fontName="Arial", fontSize=9, leading=11.5, textColor=INK, spaceBefore=0)]
    story.extend([toc, PageBreak()])
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## "))
    i = start
    first = True
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("## "):
            if not first:
                # Leave enough space for the heading, its introduction and a
                # diagram/table start without orphaning short section tails.
                story.extend([CondPageBreak(250), Spacer(1, 16)])
            first = False
            story.append(paragraph(line[3:], "h1"))
        elif line.startswith("### "):
            story.append(paragraph(line[4:], "h2"))
        elif line.startswith("@diagram "):
            story.extend([Diagram(line.split()[1]), Spacer(1, 8)])
        elif line == "@openapi":
            story.append(openapi_table())
        elif line == "@validation":
            story.append(table([["Verificación", "Resultado ejecutado"], *[[k, v] for k, v in results.items()]]))
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"[-: ]+", c) for c in cells):
                    rows.append(cells)
                i += 1
            story.append(table(rows))
            continue
        elif line.startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(Paragraph(html.escape(lines[i]).replace(" ", "&nbsp;"), STYLES["code"]))
                i += 1
            block = Table([[code]], colWidths=[CONTENT])
            block.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PALE), ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            story.extend([block, Spacer(1, 10)])
        elif line.startswith("- ") or re.match(r"\d+\. ", line):
            story.append(paragraph(line, "bullet"))
        else:
            story.append(paragraph(line))
        i += 1
    SpecDocument(candidate).multiBuild(story)


def main():
    global REPO, SOURCE
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--results", type=Path, default=HERE / f"validation_results_{VERSION}.json")
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument("--draft", action="store_true", help="Marca el candidato como borrador; no permite publicar.")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.draft and args.publish:
        parser.error("No se publica un borrador. Complete la certificación y revise el candidato.")
    REPO = args.repo.resolve() if args.repo else find_repo()
    SOURCE = args.source.resolve()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    if not isinstance(results, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in results.items()):
        parser.error("El informe de resultados debe ser un objeto de etiquetas y valores de texto.")
    tmp = args.output_dir / "tmp/pdfs"
    tmp.mkdir(parents=True, exist_ok=True)
    original = args.output_dir / PDF_NAME
    archive = HERE / "Archivo/Trackvance_Core_Especificacion_Tecnica_v1.1_original_20260910.pdf"
    if archive.exists() and hashlib.sha256(archive.read_bytes()).hexdigest() != ORIGINAL_SHA256:
        raise RuntimeError("La edición original archivada cambió; se detiene la generación.")
    candidate = tmp / "specification-candidate.pdf"
    build(candidate, results, args.draft)
    reader = PdfReader(candidate)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if "\ufffd" in text or len(reader.pages) < 20 or len(reader.outline) < 25:
        raise RuntimeError("El PDF no supera la validación estructural.")
    print(json.dumps({"candidate": str(candidate), "pages": len(reader.pages), "bookmarks": len(reader.outline), "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}, ensure_ascii=False))
    if args.publish:
        shutil.copy2(candidate, original)
        extracted = "\n\n".join(f"--- PAGE {i + 1} ---\n{page.extract_text()}" for i, page in enumerate(reader.pages))
        original.with_suffix(".extracted.txt").write_text(extracted, encoding="utf-8")


if __name__ == "__main__":
    main()
