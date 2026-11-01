"""Create a diagnostic source PDF from private content, without inference.

The private input is authoring material, never an extraction request/reference.
The layout derives from the frozen invoice_cards design; no semantic selection.
"""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from pypdf import PdfReader
import pypdfium2 as pdfium

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main():
    a=argparse.ArgumentParser()
    a.add_argument('--content',type=Path,required=True)
    a.add_argument('--output',type=Path,required=True)
    a.add_argument('--images',type=Path,required=True)
    args=a.parse_args()
    doc=json.loads(args.content.read_text(encoding='utf-8'))
    assert not args.output.exists() and not args.images.exists()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.images.mkdir(parents=True,exist_ok=False)
    w,h=A4
    c=canvas.Canvas(str(args.output),pagesize=A4,invariant=1,pageCompression=1)
    c.setTitle('Documento sintetico de prueba '+doc['id'])
    c.setAuthor('Trackvance contract diagnostic')
    boxes=[]
    def text(s,x,y,size=11,bold=False):
        c.setFillColor(colors.HexColor('#152A38'))
        c.setFont('Helvetica-Bold' if bold else 'Helvetica',size)
        c.drawString(x,y,s)
        boxes.append({'page':1,'text':s,'x':x,'baseline_y':y,'font_size':size,'bold':bold})
    top=h-52
    f=doc['fields']
    text('COBRO DE SERVICIOS',36,top,22,True)
    text(f['issuer'],36,top-35,12)
    text('Factura '+f['number']+' | Fecha '+f['date'],36,top-65)
    text('A cargo de: '+f['buyer'],36,top-88)
    c.setFillColor(colors.HexColor('#E4EEF2'));c.rect(32,top-138,w-64,24,fill=1,stroke=0)
    text('Servicios detallados - moneda '+f['currency'],40,top-130,11,True)
    y=top-175
    for row in doc['lines']:
        text(row['description'],44,y,12,True)
        text('Cantidad: '+row['quantity']+' | Precio unitario: '+row['unit_price']+' | Importe: '+row['amount'],44,y-25,10)
        y-=85
    text('Subtotal: '+f['subtotal']+' '+f['currency'],310,y-15,12,True)
    # Repeated equal-role observations receive exactly the same visual treatment.
    # Their order or position is not a priority instruction or semantic resolution.
    y-=100
    for x,amount in zip([36,304],doc['totals'],strict=True):
        c.setFillColor(colors.HexColor('#F0F5F6'));c.rect(x-4,y-22,255,55,fill=1,stroke=0)
        text('Total a pagar: '+amount+' '+f['currency'],x+3,y,12,True)
    c.setFont('Helvetica',7);c.setFillColor(colors.HexColor('#4C6574'))
    c.drawString(36,25,'Ejemplo sintetico determinista - sin validez comercial | '+doc['id']+' | 1')
    c.save()
    reader=PdfReader(args.output)
    assert len(reader.pages)==1
    pdf=pdfium.PdfDocument(str(args.output)); page=pdf[0]
    width,height=page.get_size();scale=min(150/72,2000/max(width,height))
    bitmap=page.render(scale=scale);image=bitmap.to_pil().convert('RGB')
    bitmap.close();page.close();pdf.close()
    png=args.images/'page-01.png';image.save(png,format='PNG')
    info={'page':1,'path':str(png),'sha256':sha(png),'bytes':png.stat().st_size,'width':image.width,'height':image.height,'dpi_requested':150,'render_scale':scale,'RGB':True}
    image.close()
    result={'document_id':doc['id'],'source_file':str(args.output),'source_sha256':sha(args.output),'pages':1,'model_pages':[info],'generator_sha256':sha(__file__),'private_content_sha256':sha(args.content),'render':{'reader':'pypdfium2','dpi':150,'max_edge_target':2000,'mode':'RGB_PNG'},'drawn_text_boxes':boxes,'models':0}
    (args.output.parent/'generation.private.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='drawn_text_boxes'}))

if __name__=='__main__':
    main()
