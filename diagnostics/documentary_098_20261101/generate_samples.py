"""Render deterministic synthetic source documents from a separate private JSON.

The input contains test content, never model output. No inference or extraction.
The renderer is generic to the declared document types and source layout variants.
Its drawing coordinates are NOT extraction configuration or model inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess

from PIL import Image, ImageEnhance
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def h(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def wrap(text, width, font='Helvetica', size=10):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    lines = []
    line = ''
    for word in str(text).split():
        trial = (line + ' ' + word).strip()
        if line and stringWidth(trial, font, size) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


class Painter:
    def __init__(self, path, pagesize, identity):
        self.c = canvas.Canvas(str(path), pagesize=pagesize, invariant=1, pageCompression=1)
        self.w, self.h = pagesize
        self.identity = identity
        self.page = 1
        self.boxes = []
        self.c.setTitle('Documento sintetico de prueba ' + identity)
        self.c.setAuthor('Trackvance documentary evaluation')

    def text(self, text, x, y, width=None, size=10, bold=False, color=colors.HexColor('#152A38')):
        if text is None:
            return y
        font = 'Helvetica-Bold' if bold else 'Helvetica'
        lines = wrap(text, width or self.w-80-x, font, size)
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        from reportlab.pdfbase.pdfmetrics import stringWidth
        for line in lines:
            assert y >= 40 and x + stringWidth(line, font, size) <= self.w-25, (self.identity, line, x, y)
            self.c.drawString(x, y, line)
            self.boxes.append({'page': self.page, 'text': line, 'x': x, 'baseline_y': y, 'font_size': size})
            y -= size*1.35
        return y

    def footer(self):
        self.c.setFont('Helvetica',7);self.c.setFillColor(colors.HexColor('#4C6574'))
        self.c.drawString(36,25,'Ejemplo sintetico determinista - sin validez comercial | '+self.identity+' | '+str(self.page))

    def newpage(self):
        self.footer(); self.c.showPage(); self.page += 1

    def finish(self):
        self.footer(); self.c.save()

    def band(self, text, y, width=None):
        self.c.setFillColor(colors.HexColor('#E4EEF2'))
        self.c.rect(32, y-8, width or self.w-64, 24, fill=1, stroke=0)
        return self.text(text, 40, y, size=11, bold=True)-16

    def table(self, rows, columns, labels, widths, y):
        xs = [36]
        for width in widths[:-1]: xs.append(xs[-1]+width)
        self.c.setFillColor(colors.HexColor('#D9E7ED'))
        self.c.rect(32,y-10,sum(widths)+8,25,fill=1,stroke=0)
        for i,label in enumerate(labels):self.text(label,xs[i],y,widths[i]-8,size=8,bold=True)
        y-=30
        for index,row in enumerate(rows):
            count=max(len(wrap('' if row.get(col) is None else row[col], widths[i]-8, size=9)) for i,col in enumerate(columns))
            height=max(28,count*12+12)
            if index%2==0:
                self.c.setFillColor(colors.HexColor('#F0F5F6'));self.c.rect(32,y-height+8,sum(widths)+8,height,fill=1,stroke=0)
            for i,col in enumerate(columns):self.text('' if row.get(col) is None else row[col],xs[i],y,widths[i]-8,size=9)
            y-=height
        return y


def render(item, path):
    wide=item['layout'] in {'invoice_matrix','order_cards','statement_report'}
    p=Painter(path,landscape(A4) if wide else A4,item['design_id'])
    f=item['fields'];tables=item['tables'];top=p.h-52
    layout=item['layout']
    if layout=='invoice_cards':
        p.text('COBRO DE SERVICIOS',36,top,size=22,bold=True)
        p.text(f['issuer'],36,top-35,width=300,size=12)
        p.text('Factura '+f['number']+' | Fecha '+f['date'],36,top-65,size=11)
        p.text('A cargo de: '+f['buyer'],36,top-88,size=11)
        y=p.band('Servicios detallados - moneda '+f['currency'],top-130)
        for r in tables['lines'][:2]:
            y=p.text(r['description'],44,y,470,size=12,bold=True)
            y=p.text('Cantidad: '+r['quantity']+' | Precio unitario: '+r['unit_price']+' | Importe: '+r['amount'],44,y-8,size=10)-28
        p.newpage();y=p.band('Continuacion de servicios - '+f['number'],top)
        for r in tables['lines'][2:]:
            y=p.text(r['description'],44,y,470,size=12,bold=True)
            y=p.text('Cantidad: '+r['quantity']+' | Precio unitario: '+r['unit_price']+' | Importe: '+r['amount'],44,y-8,size=10)-28
        for label,key in [('Subtotal','subtotal'),('Impuesto total','tax'),('Total a pagar','total')]:y=p.text(label+': '+f[key]+' '+f['currency'],310,y-25,size=12,bold=True)
        p.text('IVA 19 %: '+f['tax'],44,y-30,size=10)
    elif layout=='invoice_matrix':
        p.text('LIQUIDACION COMERCIAL',36,top,size=20,bold=True)
        p.text('Documento '+f['number']+' / '+f['date'],36,top-32,size=11)
        p.text('Cliente: '+f['buyer'],36,top-58,size=12)
        p.text('Vendedor: '+f['issuer'],36,top-80,size=12)
        y=p.table(tables['lines'],['amount','description','unit_price','quantity'],['Importe '+f['currency'],'Concepto','Valor por unidad','Unidades'],[115,410,120,100],top-130)
        p.text('Subtotal '+f['subtotal']+' | IVA 19 % '+f['tax']+' | Total '+f['total']+' '+f['currency'],36,y-35,size=13,bold=True)
    elif layout=='dispatch_table':
        p.text('CONSTANCIA DE DESPACHO',36,top,size=20,bold=True)
        p.text('Remision '+f['number']+' - '+f['date'],36,top-35,size=12)
        p.text('Remitente: '+f['issuer'],36,top-72,size=11)
        p.text('Destinatario: '+f['receiver'],36,top-95,size=11)
        y=p.table(tables['lines'],['reference','unit','description','quantity'],['Referencia','U.M.','Mercancia','Cantidad'],[100,65,275,80],top-150)
        p.text('Entrega parcial expresamente autorizada; las filas repetidas son bultos separados.',36,y-30,width=500,size=10)
    elif layout=='order_cards':
        p.text('SOLICITUD DE SUMINISTROS',36,top,size=22,bold=True)
        p.text('Pedido '+f['number']+' | Emitido '+f['date'],36,top-34,size=12)
        p.text('Solicita: '+f['issuer']+' | Recibe: '+f['receiver'],36,top-62,width=740,size=11)
        for page,rows in enumerate([tables['lines'][:2],tables['lines'][2:]]):
            if page:p.newpage();p.text('Continuacion del pedido '+f['number'],36,top,size=16,bold=True)
            y=top-115 if not page else top-70
            for r in rows:
                y=p.band('Referencia '+r['reference'],y)
                y=p.text(r['description'],44,y,680,size=13)
                y=p.text('Unidad de medida: '+r['unit']+' | Cantidad solicitada: '+r['quantity'],44,y-16,size=12)-45
    elif layout=='certificate_letter':
        p.c.setStrokeColor(colors.HexColor('#22636D'));p.c.rect(26,45,p.w-52,p.h-80,fill=0,stroke=1)
        p.text(f['issuer'],55,top-20,width=470,size=15,bold=True)
        p.text(f['document_type'],55,top-80,width=470,size=21,bold=True)
        p.text('Referencia: '+f['reference']+' | Expedicion: '+f['date'],55,top-122,width=470,size=11)
        y=p.text('Se certifica que '+f['holder']+', identificacion '+f['identification']+', cumple la actividad indicada en este documento.',55,top-180,width=460,size=13)
        for label,key in [('Actividad certificada','purpose'),('Sede','location'),('Estado','status'),('Vigente hasta','valid_until')]:
            if f.get(key) is not None:y=p.text(label+': '+f[key],55,y-30,width=460,size=12)
        p.text('Documento ficticio para pruebas de lectura. Sin firma ni autoridad real.',55,y-65,width=450,size=10)
    elif layout=='certificate_form':
        p.text(f['document_type'],36,top,size=20,bold=True)
        y=p.band('Seccion I - Identificacion',top-55)
        for label,key in [('Titular','holder'),('Identificacion','identification'),('Registro','reference')]:y=p.text(label+': '+f[key],42,y-18,size=12)
        p.newpage();y=p.band('Seccion II - Entidad y alcance',top)
        for label,key in [('Emisor','issuer'),('Fecha de expedicion','date'),('Localizacion','location')]:y=p.text(label+': '+f[key],42,y-22,width=480,size=12)
        p.text('No se declara fecha de vencimiento, finalidad ni estado en este formulario.',42,y-45,width=480,size=11)
    elif layout in {'statement_bank','statement_report'}:
        p.text('EXTRACTO DE CUENTA' if layout=='statement_bank' else 'REPORTE DE MOVIMIENTOS',36,top,size=21,bold=True)
        p.text(f['entity']+' | Referencia: '+f['reference'],36,top-34,size=12)
        p.text('Periodo '+f['period_start']+' a '+f['period_end']+' | Moneda '+f['currency'],36,top-60,size=11)
        rows=tables['movements']
        batches=[rows[:3],rows[3:]] if layout=='statement_bank' else [rows]
        for page,batch in enumerate(batches):
            if page:p.newpage();p.text('Movimientos - continuacion '+f['reference'],36,top,size=16,bold=True)
            y=top-105 if not page else top-45
            if f.get('opening_balance') is not None and not page:y=p.text('Saldo inicial: '+f['opening_balance'],36,y,size=12)-30
            if layout=='statement_bank':y=p.table(batch,['date','reference','description','debit','credit','balance'],['Fecha','Ref.','Detalle','Debito','Credito','Saldo'],[70,58,155,75,75,85],y)
            else:y=p.table(batch,['description','credit','date','debit','reference','balance'],['Descripcion','Ingreso','Fecha','Salida','Comprobante','Saldo'],[290,80,90,80,115,90],y)
            if f.get('closing_balance') is not None and page==len(batches)-1:p.text('Saldo final: '+f['closing_balance'],36,y-35,size=12,bold=True)
    else:
        p.text(f['title'],36,top,width=p.w-72,size=21,bold=True)
        y=top-55
        if f.get('date'):y=p.text('Fecha: '+f['date'],36,y,size=11)-20
        for index,section in enumerate(tables['sections']):
            if layout=='minutes' and index==2:p.newpage();y=top
            y=p.band(section['heading'],y-10)
            y=p.text(section['text'],40,y,width=p.w-82,size=12)-28
    p.finish()
    return p.boxes


def main():
    a=argparse.ArgumentParser();a.add_argument('--content',type=Path,required=True);a.add_argument('--output',type=Path,required=True);args=a.parse_args()
    source=json.loads(args.content.read_bytes());args.output.mkdir(parents=True,exist_ok=False)
    documents=[]
    for item in source['designs']:
        digital=args.output/(item['design_id']+'-D.pdf');boxes=render(item,digital)
        raster_dir=args.output/(item['design_id']+'-raster');raster_dir.mkdir()
        subprocess.run(['pdftoppm','-png','-r','160',str(digital),str(raster_dir/'page')],check=True,capture_output=True)
        scan=args.output/(item['design_id']+'-S.pdf');reader=PdfReader(digital)
        c=canvas.Canvas(str(scan),pagesize=A4,invariant=1,pageCompression=1)
        for image,page in zip(sorted(raster_dir.glob('page-*.png')),reader.pages,strict=True):
            w,hp=float(page.mediabox.width),float(page.mediabox.height);c.setPageSize((w,hp))
            im=Image.open(image).convert('L');im=ImageEnhance.Contrast(im).enhance(0.96)
            im=im.rotate(0.18,resample=Image.Resampling.BICUBIC,fillcolor=255,expand=False)
            stream=io.BytesIO();im.save(stream,format='JPEG',quality=82,optimize=False)
            c.drawImage(ImageReader(io.BytesIO(stream.getvalue())),0,0,width=w,height=hp);c.showPage()
        c.save();scanreader=PdfReader(scan)
        assert len(reader.pages)==len(scanreader.pages) and 1<=len(reader.pages)<=3
        assert all(not page.extract_text().strip() for page in scanreader.pages)
        for kind,path in [('digital',digital),('scanned',scan)]:
            documents.append({'document_id':item['design_id']+('-D' if kind=='digital' else '-S'),'design_id':item['design_id'],'type':item['type'],'modality':kind,'source_file':str(path),'sha256':h(path),'pages':len(reader.pages),'provenance':'NEW_DETERMINISTIC_SYNTHETIC','pair_id':item['design_id'],'same_content_variants':True,'layout':item['layout']})
        (raster_dir/'drawn-text-boxes.private.json').write_text(json.dumps(boxes,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    (args.output/'generated-manifest.private.json').write_text(json.dumps({'documents':documents,'generator_sha256':h(__file__),'private_content_sha256':h(args.content),'scan':{'dpi':160,'grayscale':True,'rotation_degrees':0.18,'contrast_factor':0.96,'jpeg_quality':82,'image_only':True}},ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'documents':len(documents),'pages':sum(x['pages'] for x in documents),'output':str(args.output)}))


if __name__=='__main__':main()
