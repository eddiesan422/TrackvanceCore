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
VERSION = "0.7.0"
EDITION_DATE = "4 octubre 2026"
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
                           textColor=INK, spaceAfter=6, allowWidows=0, allowOrphans=0),
    "h1": ParagraphStyle("h1", fontName="ArialBold", fontSize=18, leading=22,
                         textColor=NAVY, spaceAfter=12, keepWithNext=True),
    "h2": ParagraphStyle("h2", fontName="ArialBold", fontSize=11, leading=15,
                         textColor=TEAL, spaceBefore=7, spaceAfter=6, keepWithNext=True),
    "h3": ParagraphStyle("h3", fontName="ArialBold", fontSize=9.5, leading=13,
                         textColor=NAVY, spaceBefore=7, spaceAfter=5, keepWithNext=True),
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
    # Portable PDF typography: avoid non-breaking / Unicode dash glyph fallbacks.
    text = re.sub("[\u2010-\u2015]", "-", text)
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<link href="\2" color="#008B83">\1</link>', text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', text)


def paragraph(text: str, style: str = "body") -> Paragraph:
    return Paragraph(escaped(text), STYLES[style])


def table(rows: list[list[str]], widths: list[float] | None = None) -> Table:
    if widths is None:
        if len(rows[0]) == 2:
            first_fraction = 0.55 if rows[0][0] == "Variable" else 0.29
            widths = [CONTENT * first_fraction, CONTENT * (1 - first_fraction)]
        else:
            widths = [CONTENT / len(rows[0])] * len(rows[0])
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
        if kind == "create-credentials":
            self.height = 464
        elif kind == "delivery-lanes":
            self.height = 234
        elif kind in {"local", "acquisition", "xlsx-streaming", "automation", "spark", "delivery-preparation"}:
            self.height = 310

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
            self.box(0, 75, w, 45, "Servicios y semántica de módulos", "Datasets · Intake · ReconOps · Sentinel · Delivery · Excepciones · Audit")
            boundaries = ["DatasetSource", "DataSink", "StorageProvider", "ExecutionEngine", "JobQueue"]
            for i, title in enumerate(boundaries):
                x = i * (w + 8) / 5
                self.box(x, 8, (w - 32) / 5, 46, title, "Contrato + adaptador")
                self.arrow(x + (w - 32) / 10, 74, x + (w - 32) / 10, 56)
            self.arrow(w / 2, 139, w / 2, 121)
        elif self.kind == "local":
            self.box(0, 248, 150, 50, "Web / navegador", "React + nginx")
            self.box(188, 248, 150, 50, "API / RBAC", "HTTP + staging acotado")
            self.box(376, 248, 150, 50, "PostgreSQL 16", "Metadata + cola + outbox")
            self.arrow(152, 274, 185, 274)
            self.arrow(340, 274, 374, 274)
            for i, (title, detail) in enumerate([
                ("Acquisition worker", "Sólo secretos de fuentes"),
                ("DEFAULT worker", "Polars / PySpark local"),
                ("Delivery worker", "Sólo secretos de destinos"),
            ]):
                x = i * 188
                self.box(x, 156, 150, 56, title, detail)
                self.arrow(x + 75, 214, 450, 246)
            for i, (title, detail) in enumerate([
                ("Scheduler", "Sentinel + Delivery: despacho"),
                ("Events CHAINING", "Intake output exacto"),
                ("Events NOTIFICATIONS", "Bandeja personal"),
            ]):
                self.box(i * 188, 80, 150, 55, title, detail)
            self.box(0, 5, w, 52, "StorageProvider / volumen de artefactos", "Snapshots + partes + descriptor + perfiles + resultados + manifiestos")
            # The lightweight processes are independent peers coordinated by
            # PostgreSQL, rather than the next stage of an individual worker.
        elif self.kind in {"acquisition", "xlsx-streaming", "automation", "spark", "delivery-preparation"}:
            flows = {
                "acquisition": [
                    ("Recibir / explorar", "Staging privado; opciones y dueño"),
                    ("Registrar AcquisitionRun", "202 durable; Job ACQUISITION"),
                    ("Leer / materializar", "Lotes; snapshot; IDs estables"),
                    ("Perfilar / verificar", "Toda la población; hashes"),
                    ("Publicar con lease", "Partes + descriptor + versión"),
                    ("Outbox / Notificación", "Transacción de transición"),
                ],
                "xlsx-streaming": [
                    ("HTTP: recibir e inspeccionar", "ZIP privado; muestra 100 / 4 MiB / 5 s"),
                    ("Job y snapshot congelados", "202; intento, owner y lease propios"),
                    ("ZIP / XML incremental", "Miembros, estilos, expansión y tokens acotados"),
                    ("Índices SQLite privados", "Shared strings y fórmulas; cachés limitadas"),
                    ("Lotes y tipos globales", "Filas físicas; 5.000 filas / 8 MiB por lote"),
                    ("Perfil y publicación íntegra", "Toda la población; fence, partes y versión"),
                ],
                "automation": [
                    ("Tick o evento Intake", "Scheduler / CHAINING separados"),
                    ("Resolver autorización", "Sólo metadata; usuario y permisos vigentes"),
                    ("Congelar ocurrencia", "Revisión + DatasetVersion exacta"),
                    ("No repetición / target", "Claims, overlap, UNKNOWN"),
                    ("Job DELIVERY / worker", "Integridad completa fuera del lock; antes de STARTED"),
                    ("Resultado y aviso", "Commit remoto; bandeja personal"),
                ],
                "spark": [
                    ("Plan congelado", "AUTO / POLARS / PYSPARK"),
                    ("Driver DEFAULT", "Local[K] o Standalone client"),
                    ("Executors limitados", "Reglas portables; grupos globales"),
                    ("Partes de salida", "Sin metadata ni JDBC de negocio"),
                    ("Fence de publicación", "Run + Job owner y lease vigente"),
                    ("Resultados completos", "Perfil, métricas, evidencia, linaje"),
                ],
                "delivery-preparation": [
                    ("Preflight persistido", "DELIVERY_PREFLIGHT; sin intento"),
                    ("Validación completa", "Tipos, claves, destino, permisos"),
                    ("PreparedRows sellado", "SQLite: hashes + binding exacto"),
                    ("Revalidar / STARTED", "Integridad actual, autorización y target guard"),
                    ("Una transacción SQL", "Lotes; cuatro estrategias"),
                    ("COMMITTED / FAILED / UNKNOWN", "Evidencia; nunca replay ciego"),
                ],
            }
            for i, (title, detail) in enumerate(flows[self.kind]):
                row, column = divmod(i, 2)
                x, y = column * 280, 230 - row * 105
                self.box(x, y, 246, 70, title, detail)
                if column == 0:
                    self.arrow(248, y + 35, 278, y + 35)
                elif row < 2:
                    self.arrow(402, y - 2, 123, y - 33)
        elif self.kind == "exceptions":
            names = ["ABIERTA / ASIGNADA", "EN GESTIÓN", "PENDIENTE DE VALIDACIÓN", "RESUELTA"]
            for i, title in enumerate(names):
                x = i * (w + 12) / 4
                self.box(x, 130, (w - 36) / 4, 51, title)
                if i < 3:
                    self.arrow(x + (w - 36) / 4 + 1, 155, x + (w + 12) / 4 - 2, 155)
            self.box(0, 70, w, 43, "Validación técnica posterior de la misma configuración", "Resolución humana o política automática por caso, deshabilitada por defecto")
            self.box(0, 7, w, 48, "Cierre administrativo / Reapertura", "Descartada, aceptada y no aplica requieren motivo; reabrir exige comentario")
        elif self.kind == "delivery-flow":
            titles = [("DatasetVersion", "Parquet + hash"), ("Configuración", "Destino + mapping"),
                      ("Preflight", "Solo lectura"), ("Run DELIVERY", "Cola aislada")]
            for i, (title, detail) in enumerate(titles):
                x = i * (w + 12) / 4
                self.box(x, 126, (w - 36) / 4, 52, title, detail)
                if i < 3:
                    self.arrow(x + (w - 36) / 4 + 1, 152, x + (w + 12) / 4 - 2, 152)
            self.box(0, 39, 163, 60, "Transacción remota", "Prepare -> STARTED -> DataSink")
            self.box(181, 39, 163, 60, "Resultado durable", "COMMITTED / FAILED / UNKNOWN")
            self.box(362, 39, 164, 60, "Evidencia local", "Receipt + manifest + linaje")
            self.arrow(460, 125, 460, 110)
            self.arrow(460, 110, 80, 110)
            self.arrow(80, 110, 80, 100)
            self.arrow(164, 70, 179, 70)
            self.arrow(345, 70, 360, 70)
        elif self.kind == "delivery-lanes":
            self.box(0, 178, 247, 47, "API", "Autorización + snapshots + idempotencia")
            self.box(279, 178, 247, 47, "Scheduler independiente", "Sólo despacho; no calcula datasets")
            self.arrow(122, 177, 122, 168)
            self.arrow(402, 177, 402, 168)
            self.box(0, 120, w, 47, "JobQueue durable", "Lane + lease + XOR Run / AcquisitionRun")
            self.box(0, 54, 166, 53, "Worker DEFAULT", "Intake / Recon / Sentinel")
            self.box(180, 54, 166, 53, "Worker DELIVERY", "Preflight + entrega; secretos destino")
            self.box(360, 54, 166, 53, "Worker ACQUISITION", "Lectura / perfil; secretos fuente")
            for x in (83, 263, 443):
                self.arrow(x, 119, x, 109)
            self.box(0, 0, w, 45, "Metadata y StorageProvider compartidos", "No hay transacción distribuida con la base externa")
        elif self.kind == "delivery-states":
            self.box(173, 137, 180, 42, "STARTED", "Marcador persistido antes del remoto")
            outcomes = [("FAILED", "Fallo / rollback conocido"), ("COMMITTED", "Commit confirmado"),
                        ("UNKNOWN", "Confirmación indeterminada")]
            for i, (title, detail) in enumerate(outcomes):
                x = i * 179
                self.box(x, 59, 168, 51, title, detail)
                self.arrow(263, 136, x + 84, 111)
            self.box(0, 0, 250, 45, "PENDING_REPAIR", "Solo evidencia; no repite COMMITTED")
            self.box(276, 0, 250, 45, "Revisión operacional", "Anexa observación; conserva UNKNOWN")
            self.arrow(263, 58, 125, 46)
            self.arrow(442, 58, 402, 46)
        elif self.kind == "delivery-lineage":
            self.box(0, 137, 156, 43, "DatasetVersion", "Input inmutable")
            self.box(186, 137, 155, 43, "Run DELIVERY", "Configuración fija")
            self.box(371, 137, 155, 43, "DestinationVersion", "Revisión inmutable")
            self.arrow(157, 157, 184, 157)
            self.arrow(342, 157, 369, 157)
            self.box(0, 54, 250, 47, "Artifact DELIVERY_RECEIPT", "RUN_OUTPUT + DELIVERY_RECEIPT")
            self.box(277, 54, 249, 47, "DeliveryAttempt", "Artifact --EVIDENCE_OF--> intento")
            self.arrow(263, 136, 125, 103)
            self.arrow(251, 77, 275, 77)
            self.box(0, 0, w, 45, "DatasetVersion --DELIVERY_INPUT--> Run --DELIVERED_TO--> destino", "DELIVERY_DESTINATION_VERSION es target_type, no relation")
        elif self.kind == "target-policy":
            self.box(0, 126, 150, 52, "Ausente", "Publicar audit=true")
            self.box(182, 126, 158, 52, "Required", "Política local permanente")
            self.box(372, 126, 154, 52, "Materialized", "Commit remoto confirmado")
            self.arrow(151, 152, 180, 152)
            self.arrow(341, 152, 370, 152)
            self.box(164, 39, 187, 59, "UNKNOWN / rollback", "Required permanece; UNKNOWN no autoriza replay")
            self.box(370, 39, 156, 59, "Drift detectado", "Falla cerrado; no repara ni desactiva auditoría")
            self.arrow(261, 125, 261, 100)
            self.arrow(449, 125, 449, 100)
        elif self.kind == "create-credentials":
            steps = [
                ("Administrator", "Sesion normal y users:manage"),
                ("Crear usuario", "Identidad, email, rol y estado"),
                ("Generar password temporal", "CSPRNG, 32 caracteres y 24 horas"),
                ("Argon2 en DB", "Solo hash persistido"),
                ("Mostrar UNA VEZ", "Modal con acciones de copia"),
                ("Administrador entrega credenciales externamente", "Canal elegido por el administrador"),
                ("Usuario inicia sesion", "Username o email y temporal vigente"),
                ("Cambio obligatorio", "Contraseña propia, sesion y CSRF nuevos"),
                ("Sesion normal", "La temporal queda invalidada"),
            ]
            for index, (title, detail) in enumerate(steps):
                y = 414 - index * 51
                self.box(30, y, w - 60, 40, title, detail)
                if index < len(steps) - 1:
                    self.arrow(w / 2, y - 1, w / 2, y - 10)
        elif self.kind == "regenerate-credentials":
            steps = [("Administrator", "Regenerar + version"), ("Nueva temporal", "Password anterior invalida"),
                     ("Revocar sesiones", "Commit antes de responder"), ("Modal UNA VEZ", "Entregar nueva temporal"),
                     ("Primer login", "Obligatorio nuevamente"), ("Sesion normal", "Password propia y CSRF nuevo")]
            bw = (w - 30) / 3
            for index, (title, detail) in enumerate(steps):
                row, col = divmod(index, 3)
                x = col * (bw + 15) if row == 0 else (2 - col) * (bw + 15)
                y = 113 if row == 0 else 20
                self.box(x, y, bw, 62, title, detail)
                if col < 2:
                    if row == 0:
                        self.arrow(x + bw + 1, y + 31, x + bw + 13, y + 31)
                    else:
                        self.arrow(x - 1, y + 31, x - 13, y + 31)
            self.arrow(w - bw / 2, 111, w - bw / 2, 85)
        elif self.kind in {"rbac", "role-lifecycle", "permission-request", "first-login", "sso", "notification"}:
            flows = {
                "rbac": [("User", "role_id, active, deleted"), ("Role", "estado y version"), ("RolePermission", "codigos del producto"), ("Administrator protegido", "Catalogo completo vigente; usuarios no copian permisos")],
                "role-lifecycle": [("Activo", "Crear / editar"), ("Inactivo", "Sin usuarios asociados"), ("Baja logica", "Nombre reservado"), ("Bloqueos backend", "Administrator permanente; usuario inactivo tambien bloquea baja")],
                "permission-request": [("Cookie / User", "Sesion y organizacion"), ("Role vigente", "Permisos actuales"), ("Ruta y recurso", "Matriz + modulo + scope"), ("Autoridad request-by-request", "Denegar ruta desconocida; UI refresca /me y role_version")],
                "first-login": [("Alta + Argon2", "Temporal 24 horas"), ("Modal una vez", "Entrega externa"), ("Sesion restringida", "Local o SSO"), ("Nueva password + sesion rotada", "Limpiar temporal; revocar sesiones; habilitar permisos actuales")],
                "sso": [("Proveedor", "Code + PKCE"), ("Validar ID token", "Firma, iss, aud, nonce"), ("ExternalIdentity", "provider / issuer / sub"), ("Trackvance User y Role", "Sin auto-provisioning; sesion HttpOnly local y primer acceso")],
                "notification": [("0.6.0", "Intento SMTP"), ("0.6.1", "Envio retirado"), ("Backlog", "Canal por decidir"), ("NotificationDeliveryRecord historico", "0011 y filas conservadas; sin productor de notificaciones") ],
            }
            boxes = flows[self.kind]
            for index, (title, detail) in enumerate(boxes[:3]):
                x = index * (w + 12) / 3
                self.box(x, 116, (w - 24) / 3, 56, title, detail)
                if index < 2:
                    self.arrow(x + (w - 24) / 3 + 1, 144, x + (w + 12) / 3 - 2, 144)
            self.box(0, 16, w, 63, *boxes[3])
            exit_x = (5 * w + 24) / 6 if self.kind == "first-login" else w / 2
            self.arrow(exit_x, 114, exit_x, 82)
        elif self.kind == "product":
            self.box(0, 128, w, 48, "Kubernetes / AKS / EKS / OpenShift", "Web + réplicas API y workers del monolito modular")
            titles = [("Metadata", "PostgreSQL administrado"), ("Storage", "S3 / Azure Blob"), ("Jobs / compute", "Redis-Celery / Polars-PySpark")]
            for i, (title, detail) in enumerate(titles):
                self.box(i * (w + 10) / 3, 60, (w - 20) / 3, 49, title, detail)
                self.arrow(i * (w + 10) / 3 + (w - 20) / 6, 127, i * (w + 10) / 3 + (w - 20) / 6, 111)
            self.box(0, 3, w, 42, "Gobierno · Secrets Manager · OpenTelemetry · CI/CD", "Terraform + Helm | Infraestructura objetivo")
        else:
            raise ValueError(f"Diagrama desconocido: {self.kind}")


class SpecDocument(BaseDocTemplate):
    def __init__(self, path: Path):
        super().__init__(str(path), pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=58, bottomMargin=49,
                         title="Trackvance Core - Especificación Técnica v1.1",
                         author="Trackvance Colombia SAS", subject=f"Implementación {VERSION} - {EDITION_DATE}",
                         invariant=1)
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
        if self.page == 1:
            return
        if isinstance(flowable, Paragraph) and flowable.style.name in {"h1", "h2"}:
            title = flowable.getPlainText()
            level = 0 if flowable.style.name == "h1" else 1
            if level == 0:
                self._section_key = title.split(".")[0]
            key = "section-" + self._section_key + "-" + hashlib.sha256(title.encode()).hexdigest()[:12]
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, level, False)
            self.notify("TOCEntry", (level, title, self.page, key))


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


def input_document(name):
    value = json.loads((REPO / "docs/specification" / f"{name}_{VERSION}.json").read_text(encoding="utf-8"))
    if isinstance(value, dict) and value.get("version", VERSION) != VERSION:
        raise ValueError(f"El insumo {name} no corresponde a {VERSION}.")
    return value


def model_tables():
    story = []
    for entity in input_document("model_contract")["entities"]:
        # Keep the model title, evolution label, table header and first fields
        # together; a heading alone at the foot of a page is not useful.
        story.append(CondPageBreak(120))
        story.append(paragraph(entity["table"], "h2"))
        evolution = {"NEW": "Nueva en 0.7.0", "MODIFIED": "Evolucionada en 0.7.0",
                     "PRESERVED": "Estructura histórica preservada"}.get(entity.get("evolution"))
        if evolution:
            story.append(paragraph(evolution, "small"))
        rows = [["Campo", "Tipo, nulabilidad y relación"]]
        for column in entity["columns"]:
            detail = column["type"] + ("; nullable" if column["nullable"] else "; obligatorio")
            if column["primary_key"]:
                detail += "; PK"
            if column["references"]:
                detail += "; FK " + ", ".join(column["references"])
            rows.append([column["name"], detail])
        story.append(table(rows, [CONTENT * 0.43, CONTENT * 0.57]))
        for constraint in entity["constraints"]:
            story.append(paragraph("Restricción: " + constraint, "small"))
        for index in entity["indexes"]:
            story.append(paragraph("Índice: " + index["name"] + " (" + ", ".join(index["columns"]) + ")" + (" UNIQUE" if index["unique"] else ""), "small"))
    return story


def permission_tables():
    data = input_document("permission_contract")
    codes = [["Código", "Dependencias / delegable"]]
    for entry in data["catalog"]:
        codes.append([entry["code"], ", ".join(entry["dependencies"]) + ("; Sí" if entry["delegable"] else "; No")])
    routes = [["Método", "Ruta", "Permiso"]]
    routes.extend([[entry["method"], entry["path"], entry["permission"]] for entry in data["routes"]])
    return [table(codes, [CONTENT * 0.43, CONTENT * 0.57]), table(routes, [49, 280, CONTENT - 329])]


def parameter_tables():
    data = input_document("parameters")
    rows = [["Variable", "Default, unidad, rango y control"]]
    for entry in data["variables"]:
        detail = f"{entry['default']} {entry['unit']}. {entry['range']}. {entry['scope']}. {entry['guard']} {entry['restart']}"
        rows.append([entry["variable"], detail])
    fixed = [["Cota fija", "Contrato y alcance"]]
    fixed.extend([[str(index), entry] for index, entry in enumerate(data["fixed_limits"], 1)])
    return [table(rows, [CONTENT * 0.43, CONTENT * 0.57]),
            paragraph("Cotas fijas y controles complementarios", "h2"),
            table(fixed, [CONTENT * 0.15, CONTENT * 0.85])]


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
    story.append(table([[f"IMPLEMENTACIÓN {VERSION} · CORRECCIONES C01–C06"], [f"{EDITION_DATE} · {cover_status}"]], [CONTENT]))
    story.append(Spacer(1, 18))
    story.append(paragraph("Arquitectura local y evolución a producto", "h2"))
    story.append(paragraph("Monolito modular · Persistencia local · Reglas portables · Evidencia verificable"))
    story.append(Spacer(1, 25))
    story.append(paragraph("Documento oficial de referencia. Describe el estado real de Trackvance Core y distingue las capacidades implementadas de los contratos preparados y la infraestructura objetivo."))
    story.append(paragraph("Trackvance Colombia SAS", "small"))
    story.append(PageBreak())
    story.append(Paragraph("Contenido", ParagraphStyle("toc-title", parent=STYLES["h1"])))
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("toc", fontName="ArialBold", fontSize=9, leading=12, textColor=INK, spaceBefore=4),
        ParagraphStyle("toc-detail", fontName="Arial", fontSize=8, leading=10.2, leftIndent=13, textColor=MUTED, spaceBefore=0),
    ]
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
        elif line.startswith("#### "):
            story.append(paragraph(line[5:], "h3"))
        elif line.startswith("@diagram "):
            story.extend([Diagram(line.split()[1]), Spacer(1, 8)])
        elif line == "@openapi":
            story.append(openapi_table())
        elif line == "@validation":
            story.append(table([["Verificación", "Resultado ejecutado"], *[[k, v] for k, v in results.items()]]))
        elif line == "@corrections":
            data = input_document("corrections_results")
            story.append(table([["Corrección / prueba", "Evidencia independiente"], *[
                [str(entry["label"]), str(entry["result"])] for entry in data["summary"]]]))
        elif line in {"@models", "@newmodels"}:
            story.extend(model_tables())
        elif line == "@permissions":
            story.extend(permission_tables())
        elif line == "@parameters":
            story.extend(parameter_tables())
        elif line == "@volume":
            story.append(table([["Ejecución", "Medición / alcance"], *[[k, v] for k, v in input_document("volume_results").items()]]))
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
            # Each line is a row: long JSON/examples may split across pages.
            block = Table([[item] for item in code], colWidths=[CONTENT])
            block.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PALE), ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            story.extend([block, Spacer(1, 10)])
        elif line.startswith("- ") or re.match(r"\d+\. ", line):
            story.append(paragraph(line, "bullet"))
        else:
            # Markdown soft line breaks belong to one paragraph. Keeping each
            # source line as its own Paragraph adds unintended vertical gaps.
            parts = [line]
            while i + 1 < len(lines):
                following = lines[i + 1].strip()
                if not following or following.startswith(("#", "@", "|", "```", "- ")) or re.match(r"\d+\. ", following):
                    break
                parts.append(following)
                i += 1
            story.append(paragraph(" ".join(parts)))
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
    def count_bookmarks(entries):
        return sum(count_bookmarks(item) if isinstance(item, list) else 1 for item in entries)
    bookmarks = count_bookmarks(reader.outline)
    top_level = sum(not isinstance(item, list) for item in reader.outline)
    if "\ufffd" in text or len(reader.pages) < 20 or top_level < 25:
        raise RuntimeError("El PDF no supera la validación estructural.")
    print(json.dumps({"candidate": str(candidate), "pages": len(reader.pages), "bookmarks": bookmarks,
                      "top_level_sections": top_level,
                      "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}, ensure_ascii=False))
    if args.publish:
        shutil.copy2(candidate, original)
        extracted = "\n\n".join(f"--- PAGE {i + 1} ---\n{page.extract_text()}" for i, page in enumerate(reader.pages))
        extracted = "\n".join(line.rstrip() for line in extracted.splitlines()) + "\n"
        original.with_suffix(".extracted.txt").write_text(extracted, encoding="utf-8")


if __name__ == "__main__":
    main()
