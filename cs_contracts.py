"""Archivos de contrato y análisis conservador de la cartera CSV."""
import csv
import hashlib
import io
import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from werkzeug.utils import secure_filename
from cs_daily import name_key

MAX_BYTES=10*1024*1024
FORMATS={'.csv':'text/csv', '.pdf':'application/pdf',
         '.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
         '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
HEADERS=('Cliente','Servicio','Recurrencia','Precio Unitario','Propiedad','Monto Cotizado','Estatus','ID')


def money(value, field, line):
    raw=re.sub(r'[$\s]', '', value or '')
    try:
        if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?',raw):
            raise InvalidOperation
        result=Decimal(raw.replace(',',''))
        if not result.is_finite() or result<0 or result>=Decimal('1000000000000') or result.as_tuple().exponent < -2:
            raise InvalidOperation
    except InvalidOperation:
        raise ValueError(f'Fila {line}: {field} debe ser un importe válido, sin IVA agregado automáticamente.')
    return result


def analyze_csv(data):
    try:
        contents=data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('Guarda el CSV en UTF-8 antes de subirlo.')
    if not contents.strip():raise ValueError('El CSV no tiene registros.')
    if '\x00' in contents:
        raise ValueError('El CSV contiene caracteres inválidos.')
    delimiter=';' if contents.splitlines()[0].count(';') > contents.splitlines()[0].count(',') else ','
    reader=csv.DictReader(io.StringIO(contents),delimiter=delimiter,strict=True)
    try:headers=reader.fieldnames
    except csv.Error:raise ValueError('El CSV tiene encabezados mal formados.')
    if not headers or len(set(headers)) != len(headers):
        raise ValueError('El CSV debe tener encabezados únicos.')
    if any(key not in reader.fieldnames for key in HEADERS):
        raise ValueError('El CSV necesita Cliente, Servicio, Recurrencia, Precio Unitario, Propiedad, Monto Cotizado, Estatus e ID.')
    rows=[];ids=set();clients=set();active_amount=Decimal(0);cancelled=0
    try:
        for line,row in enumerate(reader,2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'Fila {line}: el número de columnas no coincide con los encabezados.')
            if not any(row.values()):continue
            if len(rows)>=5000:
                raise ValueError('El CSV admite hasta 5,000 registros.')
            row={key:value.strip() for key,value in row.items()}
            if any(len(value)>3000 for value in row.values()):
                raise ValueError(f'Fila {line}: una celda supera los 3,000 caracteres.')
            for key in ('ID','Cliente','Servicio','Propiedad','Recurrencia','Estatus'):
                if not row[key]:raise ValueError(f'Fila {line}: falta {key}.')
            key=name_key(row['ID'])
            if key in ids:raise ValueError(f'Fila {line}: ID de servicio duplicado.')
            ids.add(key);clients.add(row['Cliente'])
            amount=money(row['Monto Cotizado'],'Monto Cotizado',line)
            price=money(row['Precio Unitario'],'Precio Unitario',line)
            status=name_key(row['Estatus'])
            if status not in ('agendado','activo','vigente','cancelado'):
                raise ValueError(f'Fila {line}: estatus no reconocido; usa Agendado, Activo, Vigente o Cancelado.')
            discount=row.get('Descuento','')
            if discount:
                try:
                    d=Decimal(discount.rstrip('%'))
                    if not d.is_finite() or not 0<=d<=100:raise InvalidOperation
                except InvalidOperation:raise ValueError(f'Fila {line}: descuento inválido.')
            is_active=status!='cancelado'
            if is_active:active_amount+=amount
            else:cancelled+=1
            recurrence=name_key(row['Recurrencia'])
            every=2 if re.fullmatch(r'cada 2 meses(?: el segundo lunes)?',recurrence) else 1 if recurrence in ('mensual','cada mes') else None
            rows.append(dict(id=row['ID'],cliente=row['Cliente'],servicio=row['Servicio'],propiedad=row['Propiedad'],
                recurrencia=row['Recurrencia'],cada_meses=every,precio=str(price),monto=str(amount),
                descuento=discount,detalles=row.get('Detalles',''),estatus=row['Estatus'],activo=is_active))
    except csv.Error:
        raise ValueError('El CSV tiene comillas o filas mal formadas.')
    if not rows:raise ValueError('El CSV no tiene registros.')
    if len({name_key(client) for client in clients})!=1:
        raise ValueError('Carga un contrato por cliente; este CSV contiene más de uno.')
    active=[row for row in rows if row['activo']]
    return dict(cliente=rows[0]['cliente'],registros=len(rows),activos=len(active),cancelados=cancelled,
        sucursales=len({name_key(row['propiedad']) for row in active}),importe_activos=str(active_amount),
        cada_meses=active[0]['cada_meses'] if active and all(row['cada_meses']==active[0]['cada_meses'] for row in active) else None,
        filas=rows,
        pendiente='Falta confirmar vigencia y fechas del ciclo por sucursal para calcular compromisos de cada mes.')


def prepare_file(upload):
    if not upload or not upload.filename:raise ValueError('Selecciona el archivo del contrato.')
    ext=Path(upload.filename).suffix.lower()
    if ext not in FORMATS:raise ValueError('Admitimos CSV, PDF, Excel (.xlsx) y Word (.docx).')
    data=upload.read(MAX_BYTES+1)
    if not data:raise ValueError('El archivo está vacío.')
    if len(data)>MAX_BYTES:raise ValueError('El archivo supera 10 MB.')
    summary={}
    if ext=='.csv':summary=analyze_csv(data)
    elif ext=='.pdf' and not data.startswith(b'%PDF-'):raise ValueError('El archivo no es un PDF válido.')
    elif ext in ('.xlsx','.docx'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                needed='xl/workbook.xml' if ext=='.xlsx' else 'word/document.xml'
                if needed not in archive.namelist():raise ValueError('El contenido no corresponde al formato indicado.')
        except zipfile.BadZipFile:raise ValueError('El archivo de Office no es válido.')
    filename=(secure_filename(upload.filename) or 'contrato'+ext)
    if len(filename)>200:filename=filename[:180]+ext
    return filename,FORMATS[ext],data,hashlib.sha256(data).hexdigest(),summary


def validity(form):
    dates=[]
    for key in ('inicio','fin'):
        raw=form.get(key,'').strip()
        try:dates.append(date.fromisoformat(raw) if raw else None)
        except ValueError:raise ValueError('Indica una fecha de vigencia válida.')
    if all(dates) and dates[1]<dates[0]:raise ValueError('La fecha final debe ser posterior o igual a la inicial.')
    return dates


def contract_reference(account_id,cut):
    from models import CSContrato
    from cs_weekly import MX
    doc=CSContrato.query.filter_by(account_id=account_id,activo=True).first()
    if not doc:return None
    summary={key:value for key,value in doc.resumen.items() if key!='filas'}
    uploaded=doc.created_at.astimezone(MX)
    return dict(id=str(doc.id),nombre=doc.nombre,created_by=doc.created_by,
        cargado=uploaded.strftime('%d/%m/%Y'),posterior_al_corte=uploaded.date()>cut,
        inicio=doc.inicio.strftime('%d/%m/%Y') if doc.inicio else '',
        fin=doc.fin.strftime('%d/%m/%Y') if doc.fin else '',resumen=summary)
