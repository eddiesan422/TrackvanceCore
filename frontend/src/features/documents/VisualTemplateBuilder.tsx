import { useState } from 'react'
import type { PointerEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, post } from '../../api/client'
import type { Collection, RecordData } from '../../api/client'
import { ErrorState, Field, Loading, Notice, Pagination } from '../../components/ui'
import HeaderSourceConfirmation from './HeaderSourceConfirmation'
import { needsReview, sourceDescription, sourceSelection } from './documentReview'
import DocumentCandidateDetails from './DocumentCandidateDetails'
import DocumentExecution from './DocumentExecution'
import DocumentRecognition from './DocumentRecognition'
import './visual-template-builder.css'
import FieldNormalization from './FieldNormalization'

type Region = [number, number, number, number]
const groups = { DOCUMENT: 'Documento', ISSUER: 'Emisor', BUYER: 'Adquiriente', TOTALS: 'Totales' }
const kinds = { STRING: 'Texto o identificador', DECIMAL: 'Importe o decimal', INT64: 'Número entero', DATE: 'Fecha', BOOLEAN: 'Verdadero / falso' }
const regionStyle = (r: Region) => ({ left: `${r[0] * 100}%`, top: `${r[1] * 100}%`, width: `${(r[2] - r[0]) * 100}%`, height: `${(r[3] - r[1]) * 100}%` })

function advancedDefinition(value: unknown): RecordData {
  const object = (item: unknown): item is RecordData => item !== null && typeof item === 'object' && !Array.isArray(item)
  const text = (item: unknown) => item === undefined || typeof item === 'string'
  const field = (item: unknown) => object(item) && typeof item.name === 'string' && text(item.label) && text(item.logical_type) && (item.visual == null || object(item.visual) && text(item.visual.label))
  const invalid = () => new Error('La definición avanzada requiere un objeto con campos y tablas en listas, nombres de texto y columnas válidas. Corrige el JSON; las reglas actuales se conservan.')
  if (!object(value)) throw invalid()
  // Match the omitted collection defaults of DocumentDraftDefinition. The
  // server still validates extraction rules, types and publication semantics.
  const fields = value.fields === undefined ? [] : value.fields
  const tables = value.tables === undefined ? [] : value.tables
  const arithmetic = value.arithmetic === undefined ? [] : value.arithmetic
  const publication = value.publication === undefined ? {} : value.publication
  if (!Array.isArray(fields) || !fields.every(field) || !Array.isArray(tables) || !tables.every(table => object(table) && typeof table.name === 'string' && Array.isArray(table.fields) && table.fields.every(field) && (table.visual == null || object(table.visual) && Array.isArray(table.visual.column_bounds) && table.visual.column_bounds.every((bound: unknown) => typeof bound === 'number' && Number.isFinite(bound)) && text(table.visual.stop_label))) || !Array.isArray(arithmetic) || !arithmetic.every(object) || !object(publication)) throw invalid()
  return { ...value, fields, tables, arithmetic, publication }
}

export default function VisualTemplateBuilder({ identity, refresh, templates = [] }: {
  identity: string; refresh: () => void; existing?: RecordData; templates?: RecordData[]; onReviewDocument?: (id: string) => void
}) {
  const key = `document-draft:${identity}`
  const [selected, setSelected] = useState(() => localStorage.getItem(key) || '')
  const [source, setSource] = useState('')
  const [error, setError] = useState<unknown>(null)
  const drafts = useQuery({ queryKey: ['document-drafts', identity], queryFn: () => api<Collection>(`/documents/families/${identity}/builder/drafts`) })
  const draft = useQuery({ queryKey: ['document-draft', selected], queryFn: () => api<RecordData>(`/documents/builder/drafts/${selected}`), enabled: !!selected })
  const choose = (id: string) => { setSelected(id); localStorage.setItem(key, id) }
  return <section className="panel visual-template-builder" aria-labelledby="visual-builder-title">
    <header className="visual-builder-heading"><div><h2 id="visual-builder-title">Referencias y borrador de plantilla</h2><p>Analiza varios originales, ajusta la propuesta, prueba la misma configuración y aprueba su activación.</p></div></header>
    <Notice>Las referencias sirven para configurar. Su carga y sus pruebas no crean revisiones formales, documentos operativos ni datasets.</Notice>
    <div className="visual-builder-example">
      <Field label="Borrador guardado"><select value={selected} onChange={e => choose(e.target.value)}><option value="">Elegir borrador</option>{drafts.data?.items.map(d => <option value={d.id} key={d.id}>{d.name} · versión {d.version}</option>)}{selected && !drafts.data?.items.some(d => d.id === selected) && <option value={selected}>Borrador seleccionado</option>}</select></Field>
      <Field label="Base de un nuevo borrador"><select value={source} onChange={e => setSource(e.target.value)}><option value="">Vacío</option>{templates.map(t => <option value={t.id} key={t.id}>Copia explícita de revisión {t.revision}</option>)}</select></Field>
      <button className="button secondary" onClick={async () => { try { const result = await post(`/documents/families/${identity}/builder/drafts`, { source_template_id: source || null }); choose(result.id); await drafts.refetch() } catch (e) { setError(e) } }}>Crear borrador</button>
    </div>
    {!selected && <p>Elige un borrador para continuar, o crea uno vacío. Las revisiones históricas solo se copian al elegirlas.</p>}
    {draft.isLoading && <Loading/>}{draft.data && <DraftEditor key={draft.data.id} initial={draft.data} identity={identity} refresh={refresh}/>}
    {!!(error || drafts.error || draft.error) && <ErrorState error={error || drafts.error || draft.error}/>}
  </section>
}

function DraftEditor({ initial, identity, refresh }: { initial: RecordData; identity: string; refresh: () => void }) {
  const [draft, setDraft] = useState(initial)
  const [definition, setDefinition] = useState<RecordData>(initial.definition)
  const [ids, setIds] = useState<string[]>(initial.reference_ids)
  const [referenceId, setReferenceId] = useState<string>(initial.reference_ids[0] || '')
  const [test, setTest] = useState<RecordData | null>(initial.latest_test || null)
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [role, setRole] = useState('CONFIGURATION')
  const [association, setAssociation] = useState('')
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState(1)
  const [zoom, setZoom] = useState(100)
  const [mode, setMode] = useState<'FIELD' | 'TABLE'>('FIELD')
  const [selection, setSelection] = useState<Region | null>(null)
  const [start, setStart] = useState<[number, number] | null>(null)
  const [reason, setReason] = useState('')
  const [changes, setChanges] = useState<Record<string, string>>({})
  const [confirmed, setConfirmed] = useState<string[]>([])
  const [advanced, setAdvanced] = useState<string | null>(null)
  const references = useQuery({ queryKey: ['document-references', identity, offset], queryFn: () => api<Collection>(`/documents/families/${identity}/builder/references?offset=${offset}&limit=25`), refetchInterval: q => q.state.data?.items.some(r => ['QUEUED', 'RUNNING'].includes(r.status)) ? 1500 : false })
  const reference = useQuery({ queryKey: ['document-reference', referenceId], queryFn: () => api<RecordData>(`/documents/builder/references/${referenceId}`), enabled: !!referenceId, refetchInterval: q => q.state.data && ['QUEUED', 'RUNNING'].includes(q.state.data.status) ? 1500 : false })
  const selectedPending = references.data?.items.some(r => ids.includes(r.id) && r.status !== 'READY') || false
  const act = async (action: () => Promise<void>) => { setBusy(true); setError(null); try { await action() } catch (e) { setError(e) } finally { setBusy(false) } }
  const edit = (value: RecordData) => { setDefinition(value); setDirty(true); setTest(null) }
  const chooseReference = (id: string) => { setReferenceId(id); setPage(1); setChanges({}); setConfirmed([]); setSelection(null) }
  const save = async () => {
    const result = await api<RecordData>(`/documents/builder/drafts/${draft.id}`, { method: 'PUT', body: JSON.stringify({ expected_version: draft.version, definition, reference_ids: ids, name: draft.name }) })
    setDraft(result); setDefinition(result.definition); setDirty(false); setTest(null); setNotice(`Borrador guardado: versión ${result.version}.`); return result
  }
  const updateField = (index: number, change: RecordData) => edit({ ...definition, fields: definition.fields.map((f: RecordData, i: number) => i === index ? { ...f, ...change } : f) })
  const updateTable = (index: number, change: RecordData) => edit({ ...definition, tables: definition.tables.map((t: RecordData, i: number) => i === index ? { ...t, ...change } : t) })
  const propose = (region: Region) => void act(async () => {
    const result = await post(`/documents/builder/references/${referenceId}/propose`, { kind: mode, selection: { page, region } })
    setSelection(region)
    if (result.field) { const index = definition.fields.findIndex((f: RecordData) => f.name === result.field.name); edit({ ...definition, fields: index < 0 ? [...definition.fields, result.field] : definition.fields.map((f: RecordData, i: number) => i === index ? { ...f, visual: result.field.visual, label: result.field.label, visual_alternatives: [] } : f) }) }
    if (result.table) edit({ ...definition, tables: [...definition.tables.filter((t: RecordData) => t.name !== result.table.name), { ...result.table, visual: { ...result.table.visual, flowing: true, continue_pages: true } }] })
    setNotice(result.notice)
  })
  const point = (e: PointerEvent<HTMLDivElement>): [number, number] => { const r = e.currentTarget.getBoundingClientRect(); return [Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)), Math.max(0, Math.min(1, (e.clientY - r.top) / r.height))] }
  const finish = (e: PointerEvent<HTMLDivElement>) => { if (!start || reference.data?.role === 'HOLDOUT') return; const end = point(e); const r: Region = [Math.min(start[0], end[0]), Math.min(start[1], end[1]), Math.max(start[0], end[0]), Math.max(start[1], end[1])]; setStart(null); if (r[2] - r[0] > .003 && r[3] - r[1] > .003) propose(r) }
  const payload = test && (test.reviewed_results[referenceId] || test.automatic_results[referenceId])
  const cells: { path: string; name: string; cell: RecordData }[] = payload ? [
    ...Object.entries(payload.fields).map(([name, cell]) => ({ path: `headers.0.${name}`, name, cell: cell as RecordData })),
    ...Object.entries(payload.tables).flatMap(([table, rows]) => (rows as RecordData[]).flatMap((row, i) => Object.entries(row).map(([name, cell]) => ({ path: `${table}.${i}.${name}`, name: `${table} · fila ${i + 1} · ${name}`, cell: cell as RecordData }))))
  ] : []
  const focusCell = (path: string, cell: RecordData) => { const source = sourceSelection(cell, path); if (source.page) setPage(source.page); setSelection(source.region || null); setNotice(sourceDescription(source)); document.getElementById(`builder-cell-${path}`)?.scrollIntoView?.({ block: 'nearest' }) }
  const reviewCell = ({ path, name, cell }: { path: string; name: string; cell: RecordData }) => <div className="visual-builder-card" id={`builder-cell-${path}`} key={path}>
    <Field label={`Corregir valor de este documento: ${name}`}><input value={changes[path] ?? cell.value ?? ''} onFocus={() => focusCell(path, cell)} onChange={e => { setChanges({ ...changes, [path]: e.target.value }); setConfirmed(confirmed.filter(value => value !== path)) }}/></Field>
    <small>Reconocimiento: {cell.confidence == null ? 'Sin estimación' : `${cell.confidence.toFixed(1)} %`} · Validez: {cell.recognition_status === 'NOT_APPLICABLE' ? 'No aplicable en esta referencia' : cell.validity === 'VALID' ? 'Válido' : cell.validity === 'AMBIGUOUS' ? 'Ambiguo' : cell.validity === 'MISSING' ? 'Esperado no localizado' : 'Formato inválido'} · Revisión: {path in changes ? 'Corrección pendiente de confirmar' : needsReview(path, cell, test?.issues?.[referenceId]) ? 'Pendiente' : cell.reviewed ? 'Confirmada' : 'No requerida por el validador'}</small>
    <button className="button secondary small" onClick={() => focusCell(path, cell)}>Resaltar origen</button>
    {(path in changes || needsReview(path, cell, test?.issues?.[referenceId]) || cell.reviewed) && <label><input type="checkbox" disabled={!!cell.reviewed && !needsReview(path, cell, test?.issues?.[referenceId]) && !(path in changes)} checked={confirmed.includes(path) || !!cell.reviewed && !needsReview(path, cell, test?.issues?.[referenceId]) && !(path in changes)} onChange={e => setConfirmed(e.target.checked ? [...new Set([...confirmed, path])] : confirmed.filter(p => p !== path))}/> Confirmar contra original</label>}
    {cell.corrected && <small>Corrección manual, sin coordenadas inventadas. El resultado automático permanece conservado.</small>}
    <DocumentCandidateDetails value={cell} canReview onChoose={value => { setChanges({ ...changes, [path]: value }); setConfirmed(confirmed.filter(current => current !== path)) }}/>
  </div>
  return <div>
    <div className="visual-builder-example"><Field label="Nombre del borrador"><input value={draft.name} onChange={e => { setDraft({ ...draft, name: e.target.value }); setDirty(true); setTest(null) }}/></Field><span role="status">Versión {draft.version} · {dirty ? 'Cambios sin guardar; pruebas invalidadas' : 'Guardado en servidor'}</span><button className="button primary" disabled={busy || !dirty} onClick={() => void act(async () => { await save() })}>Guardar borrador</button></div>
    <div className="visual-builder-example">
      <Field label="Uso de las nuevas referencias"><select value={role} onChange={e => setRole(e.target.value)}><option value="CONFIGURATION">Configurar y descubrir</option><option value="HOLDOUT">Variante reservada solo para probar</option></select></Field>
      <Field label="Asociación confirmada de la misma factura"><input value={association} maxLength={120} placeholder="Vacío si no se ha comprobado" onChange={e => setAssociation(e.target.value)}/></Field>
      <label className="button secondary visual-file-label">Cargar referencias conjuntamente<input aria-label="Cargar referencias" type="file" accept=".pdf,.png,.jpg,.jpeg" multiple disabled={busy} onChange={e => { const files = Array.from(e.target.files || []); if (!files.length) return; void act(async () => { const body = new FormData(); files.forEach(f => body.append('files', f)); body.append('role', role); if (association.trim()) body.append('association', association.trim()); const result = await api<Collection>(`/documents/families/${identity}/builder/references`, { method: 'POST', body }); setIds([...new Set([...ids, ...result.items.map(r => r.id)])]); setDirty(true); setTest(null); chooseReference(result.items[0].id); await references.refetch(); setNotice('Originales conservados. Guarda el borrador para conservar esta selección.'); refresh() }) }}/></label>
    </div>
    <fieldset className="visual-builder-reference-list"><legend>Referencias para la prueba conjunta</legend>{references.data?.items.map(r => <div key={r.id}><label><input type="checkbox" checked={ids.includes(r.id)} onChange={e => { setIds(e.target.checked ? [...ids, r.id] : ids.filter(id => id !== r.id)); setDirty(true); setTest(null) }}/>{r.filename} · {r.role === 'HOLDOUT' ? 'Variante reservada' : 'Configuración'} {r.configuration_used ? ' · Usada para configurar' : ''} · {r.status === 'READY' ? 'Lectura disponible' : r.status === 'FAILED' ? 'Lectura fallida' : 'Leyendo'}</label><button className="button secondary small" onClick={() => chooseReference(r.id)}>Ver original</button></div>)}</fieldset>
    <Pagination offset={offset} limit={25} total={references.data?.total || 0} onChange={setOffset}/>
    {reference.data && <><DocumentExecution document={reference.data}/><DocumentRecognition document={reference.data} canDownload canReplay={false} resource="builder/references" refresh={() => reference.refetch()}/></>}
    <div className="visual-builder-actions">
      <button className="button primary" aria-describedby={`discovery-replacement-${draft.id}`} disabled={busy || !ids.length || selectedPending} onClick={() => void act(async () => { const current = dirty ? await save() : draft; const result = await post(`/documents/builder/drafts/${draft.id}/discover`, { expected_version: current.version }); setDraft(result.draft); setDefinition(result.draft.definition); setDirty(false); setTest(null); setNotice(`${result.proposal.notice} Campos esperados no localizados: ${(result.proposal.expected_not_located || []).join(', ') || 'ninguno'}.`) })}>Analizar documentos y proponer plantilla</button>
      <button className="button secondary" disabled={busy || !definition.fields.length || !ids.length || selectedPending} onClick={() => void act(async () => { const current = dirty ? await save() : draft; setTest(await post(`/documents/builder/drafts/${draft.id}/test`, { expected_version: current.version })); setChanges({}); setConfirmed([]); setNotice('Misma instantánea sobre todas las referencias. Revisa los resultados antes de activar.') })}>Probar todas las referencias</button>
    </div>
    <p id={`discovery-replacement-${draft.id}`}>Analizar las referencias genera una propuesta nueva que reemplaza los campos, controles aritméticos y políticas de este borrador. La revisión activa se conserva.</p>
    <div className="visual-builder-workspace"><div className="visual-builder-document">
      <div className="visual-builder-toolbar"><Field label="Referencia en revisión"><select value={referenceId} onChange={e => chooseReference(e.target.value)}><option value="">Elegir referencia</option>{[...new Map([...(references.data?.items || []), ...(reference.data ? [reference.data] : [])].map(r => [r.id, r])).values()].map(r => <option key={r.id} value={r.id}>{r.filename}</option>)}</select></Field><Field label="Página"><select value={page} onChange={e => { setPage(Number(e.target.value)); setSelection(null) }}>{(reference.data?.payload?.pages || [1]).map((p: number) => <option key={p}>{p}</option>)}</select></Field><Field label="Zoom"><select value={zoom} onChange={e => setZoom(Number(e.target.value))}>{[50, 75, 100, 125, 150, 200].map(z => <option key={z} value={z}>{z} %</option>)}</select></Field></div>
      {reference.data && <>{reference.data.role === 'HOLDOUT' && <Notice>Variante reservada: permite revisar valores; la selección para configurar reglas está bloqueada.</Notice>}<div role="group" aria-label="Selección visual de corrección"><button className="button secondary small" aria-pressed={mode === 'FIELD'} onClick={() => setMode('FIELD')}>Corregir regla de campo</button><button className="button secondary small" aria-pressed={mode === 'TABLE'} onClick={() => setMode('TABLE')}>Corregir regla de tabla</button></div><p className="visual-builder-hint">Selecciona un valor o una región para ajustar reglas futuras. Las correcciones de valores se guardan a la derecha.</p>
        <div className="visual-builder-viewport"><div className="visual-builder-page" style={{ width: `${zoom}%` }} onPointerDown={e => { if ((e.target as HTMLElement).closest('button')) return; e.currentTarget.setPointerCapture(e.pointerId); setStart(point(e)) }} onPointerUp={finish}>
          <img draggable={false} alt={`Página ${page} de ${reference.data.filename}`} src={reference.data.filename.toLowerCase().endsWith('.pdf') ? `/api/v1/documents/builder/references/${referenceId}/pages/${page}` : `/api/v1/documents/builder/references/${referenceId}/file`}/>
          {reference.data.payload?.lines.filter((line: RecordData) => line.page === page).flatMap((line: RecordData, n: number) => (line.words || []).map((word: RecordData, i: number) => <button key={`${n}-${i}`} className="visual-builder-word" style={regionStyle(word.region)} aria-label={`Seleccionar ${word.text}`} disabled={reference.data.role === 'HOLDOUT' || busy} onClick={() => propose(word.region)}><span className="sr-only">{word.text}</span></button>))}
          {selection && <span className="visual-builder-selection" style={regionStyle(selection)}/>}
        </div></div>
        <details><summary>Seleccionar una región con teclado</summary><div className="visual-region-inputs">{['Izquierda', 'Arriba', 'Derecha', 'Abajo'].map((label, i) => <Field label={`${label} (%)`} key={label}><input type="number" min={0} max={100} step={.1} value={(selection || [.05, .05, .95, .8])[i] * 100} onChange={e => { const r = [...(selection || [.05, .05, .95, .8])] as Region; r[i] = Number(e.target.value) / 100; setSelection(r) }}/></Field>)}</div><button className="button secondary" disabled={!selection || busy || reference.data.role === 'HOLDOUT'} onClick={() => selection && propose(selection)}>Proponer desde esta región</button></details>
      </>}{reference.isLoading && <Loading/>}{reference.data?.status === 'FAILED' && <Notice>No se pudo leer esta referencia. Conserva el original y revisa el diagnóstico: {reference.data.error_code}</Notice>}
    </div><div className="visual-builder-rules">
      {test ? <><h3>Revisar este documento</h3><p>Corregir un valor conserva la propuesta automática y no modifica las reglas futuras.</p><button className="button secondary" onClick={() => { const pending = cells.find(({ path, cell }) => (path in changes || needsReview(path, cell, test.issues?.[referenceId])) && !confirmed.includes(path)); if (pending) focusCell(pending.path, pending.cell); else setNotice('No quedan celdas pendientes en esta referencia. Revisa las incidencias de tabla y aritmética.') }}>Siguiente incidencia</button>
        {Object.entries(groups).map(([group, label]) => <section key={group}><h3>{label}</h3>{cells.filter(c => c.path.startsWith('headers.') && (definition.fields.find((f: RecordData) => f.name === c.name)?.section || 'DOCUMENT') === group).map(reviewCell)}</section>)}<section><h3>Detalle</h3>{cells.filter(c => !c.path.startsWith('headers.')).map(reviewCell)}</section>
        {payload && Object.entries(payload.table_unparsed || {}).map(([name, rows]) => (rows as RecordData[]).some(row => !row.header_observation) && <Notice key={name}>{name}: {(rows as RecordData[]).filter(row => !row.header_observation).length} regiones sin interpretar. Ajusta la regla y vuelve a probar; la tabla no está completa.</Notice>)}
        <HeaderSourceConfirmation key={`${referenceId}:${test.review_version}`} review={test.header_review?.[referenceId]} busy={busy} pendingChanges={Object.keys(changes).length > 0 || confirmed.length > 0} sourceUrl={p => reference.data?.filename.toLowerCase().endsWith('.pdf') ? `/api/v1/documents/builder/references/${referenceId}/pages/${p}` : `/api/v1/documents/builder/references/${referenceId}/file`} onConfirm={async value => { setBusy(true); setError(null); try { setTest(await post(`/documents/builder/tests/${test.id}/review`, { reference_id: referenceId, expected_version: test.review_version, changes: {}, confirmed: [], reason: value.source_note, header_confirmations: [value] })); setNotice('Encabezado confirmado contra la fuente. El resultado automático y su región permanecen conservados.') } catch (error) { setError(error); throw error } finally { setBusy(false) } }}/>
        {payload?.arithmetic?.map((c: RecordData, i: number) => <Notice key={i}>{c.name}: {c.status === 'VALID' ? 'Importes consistentes' : 'Revisar discrepancia aritmética'} · esperado {c.expected ?? 'no calculado'} · observado {c.observed ?? 'no calculado'}</Notice>)}
        <Field label="Motivo de la revisión de valores"><input value={reason} maxLength={2000} onChange={e => setReason(e.target.value)}/></Field><button className="button primary" disabled={busy || !reason.trim() || !payload || !!test.activated_template_id} onClick={() => void act(async () => { setTest(await post(`/documents/builder/tests/${test.id}/review`, { reference_id: referenceId, expected_version: test.review_version, changes, confirmed, reason })); setChanges({}); setConfirmed([]); setReason(''); setNotice('Revisión registrada. Las reglas y resultados automáticos permanecen conservados.') })}>Guardar revisión de este documento</button><button className="button secondary" onClick={() => { setTest(null); setNotice('Ajusta la regla del borrador y vuelve a probar todas las referencias. Las pruebas anteriores permanecen conservadas.') }}>Corregir regla para futuros documentos</button>
      </> : <>
        {Object.entries(groups).map(([group, label]) => <section key={group}><h3>{label}</h3>{definition.fields.map((f: RecordData, index: number) => (f.section || 'DOCUMENT') === group && <fieldset key={index} className="visual-builder-card"><legend>{f.label || f.name}</legend>
          <Field label="Nombre del campo"><input value={f.name} onChange={e => updateField(index, { name: e.target.value })}/></Field><Field label="Tipo de dato"><select value={f.logical_type || 'STRING'} onChange={e => updateField(index, { logical_type: e.target.value })}>{Object.entries(kinds).map(([value, text]) => <option value={value} key={value}>{text}</option>)}</select></Field><Field label="Sección"><select value={f.section || 'DOCUMENT'} onChange={e => updateField(index, { section: e.target.value })}>{Object.entries(groups).map(([value, text]) => <option value={value} key={value}>{text}</option>)}</select></Field>
          {f.visual && <Field label="Etiqueta ancla"><input value={f.visual.label} onChange={e => updateField(index, { visual: { ...f.visual, label: e.target.value, label_aliases: [] }, visual_alternatives: [] })}/></Field>}
          {f.logical_type === 'DECIMAL' && <><Field label="Separador decimal"><select value={f.decimal_separator || '.'} onChange={e => updateField(index, { decimal_separator: e.target.value, decimal_format_explicit: true })}><option value=".">Punto</option><option value=",">Coma</option></select></Field><Field label="Separador de millares"><select value={f.thousands_separator || ''} onChange={e => updateField(index, { thousands_separator: e.target.value || null, decimal_format_explicit: true })}><option value="">Sin separador configurado</option><option value=".">Punto</option><option value=",">Coma</option><option value=" ">Espacio</option></select></Field></>}
          {f.logical_type === 'DATE' && <Field label="Formato de fecha"><select value={f.date_format || '%Y-%m-%d'} onChange={e => updateField(index, { date_format: e.target.value })}><option value="%Y-%m-%d">Año-mes-día</option><option value="%d/%m/%Y">Día/mes/año</option><option value="%m/%d/%Y">Mes/día/año</option></select></Field>}
          <FieldNormalization field={f} onChange={normalization => updateField(index, { normalization })}/>
          {['required', 'critical', 'business_key'].map((flag, n) => <label key={flag}><input type="checkbox" checked={!!f[flag]} onChange={e => updateField(index, { [flag]: e.target.checked })}/>{['Obligatorio', 'Crítico', 'Clave de negocio'][n]}</label>)}<button className="button secondary small" onClick={() => edit({ ...definition, fields: definition.fields.filter((_: RecordData, i: number) => i !== index) })}>Quitar campo</button>
        </fieldset>)}</section>)}
        <section><h3>Detalle</h3>{definition.tables.map((table: RecordData, index: number) => <fieldset key={index} className="visual-builder-card"><legend>{table.name}</legend><Field label="Nombre de tabla"><input value={table.name} onChange={e => updateTable(index, { name: e.target.value })}/></Field>{table.visual && <Field label="Etiqueta de fin del detalle"><input value={table.visual.stop_label} onChange={e => updateTable(index, { visual: { ...table.visual, stop_label: e.target.value } })}/></Field>}<p>La altura del ejemplo no limita las filas futuras.</p>{table.fields.map((field: RecordData, column: number) => <div key={column} className="visual-builder-column"><Field label={`Nombre columna ${column + 1}`}><input value={field.name} onChange={e => updateTable(index, { fields: table.fields.map((f: RecordData, j: number) => j === column ? { ...f, name: e.target.value } : f) })}/></Field><Field label={`Tipo columna ${column + 1}`}><select value={field.logical_type} onChange={e => updateTable(index, { fields: table.fields.map((f: RecordData, j: number) => j === column ? { ...f, logical_type: e.target.value } : f) })}>{Object.entries(kinds).map(([value, text]) => <option value={value} key={value}>{text}</option>)}</select></Field><FieldNormalization field={field} onChange={normalization => updateTable(index, { fields: table.fields.map((item: RecordData, j: number) => j === column ? { ...item, normalization } : item) })}/>{table.visual && <Field label={`Límite derecho columna ${column + 1} (%)`}><input type="number" min={0} max={100} step={.1} value={table.visual.column_bounds[column + 1] * 100} onChange={e => updateTable(index, { visual: { ...table.visual, column_bounds: table.visual.column_bounds.map((b: number, j: number) => j === column + 1 ? Number(e.target.value) / 100 : b) } })}/></Field>}</div>)}</fieldset>)}</section>
      </>}
    </div></div>
    {test && <section className="visual-builder-results"><h3>Prueba conjunta · instantánea {test.snapshot_hash.slice(0, 12)}</h3><p>La cobertura reconocida no demuestra exactitud automática. Las confirmaciones y el esfuerzo de corrección se muestran por separado.</p>{Object.entries(test.automatic_metrics).map(([id, metrics]) => { const m = metrics as RecordData; const final = test.reviewed_metrics[id]; return <div key={id}><strong>{references.data?.items.find(r => r.id === id)?.filename || id}</strong><p>Automático: {m.recognized}/{m.fields_and_cells} campos y celdas reconocidos · {m.rows} filas · {m.ambiguous} ambigüedades · {m.unparsed_regions} regiones sin interpretar.</p><p>Final revisado: {final.human_confirmed}/{final.fields_and_cells} confirmaciones · {final.corrected} correcciones. Exactitud: requiere oráculo independiente.</p><p>{test.issues[id].length} incidencias pendientes.</p></div> })}{test.comparisons?.map((c: RecordData) => <p key={c.association}>Asociación confirmada: {c.association} · {c.equal ? 'Valores automáticos coinciden' : 'Valores automáticos difieren; revisa los originales'}</p>)}<button className="button primary" disabled={busy || dirty || Object.values(test.issues).some(value => (value as string[]).length > 0)} onClick={() => void act(async () => { const result = await post(`/documents/builder/tests/${test.id}/activate`); setTest({ ...test, activated_template_id: result.id }); setNotice(`Revisión formal ${result.revision} activada desde esta instantánea. Los ejemplos siguen como referencias.`); refresh() })}>{test.activated_template_id ? 'Revisión activada' : 'Aprobar y activar esta instantánea'}</button></section>}
    <section className="document-review-policy"><h3>Política de revisión</h3>
      <Field label="Política de evidencia"><select disabled={busy} value={definition.validation_policy?.version || 'LEGACY'} onChange={event => edit({ ...definition, validation_policy: event.target.value === 'LEGACY' ? undefined : { version: 'EVIDENCE_V2', allow_noncritical_source_bound: true, critical_corroboration: 'INDEPENDENT_SOURCE', require_table_structure: true } })}><option value="LEGACY">Controles históricos de esta plantilla</option><option value="EVIDENCE_V2">Evidencia por clase de campo y riesgo</option></select></Field>
      {definition.validation_policy?.version === 'EVIDENCE_V2' && <p>Los datos críticos requieren corroboración independiente. El validador comprueba páginas, estructura, tipos y reglas declaradas; un JSON válido o respuestas repetidas no habilitan la publicación por sí solos.</p>}
      <Field label="Revisión antes de publicar"><select value={definition.publication?.review_policy || 'ALL'} disabled={busy} onChange={event => edit({ ...definition, publication: { ...definition.publication, review_policy: event.target.value } })}><option value="ALL">Confirmación humana de todos los valores</option><option value="CRITICAL_OR_UNCERTAIN">Valores críticos o inciertos</option><option value="AUTO_VERIFIED">Aprobación por controles independientes</option></select></Field><p>{definition.publication?.review_policy === 'AUTO_VERIFIED' ? 'El validador debe comprobar evidencia, completitud y coherencia antes de aprobar. Cualquier discrepancia pasa a revisión. Esta política no registra confirmaciones humanas.' : 'La política pertenece al borrador y requiere las mismas pruebas y aprobación antes de activarse.'}</p></section>
    <details className="document-advanced"><summary>Configuración técnica avanzada del borrador</summary><p>Reglas, formatos, publicación y comprobaciones aritméticas pertenecen a la misma instantánea.</p><textarea aria-label="Definición avanzada del borrador" rows={14} value={advanced ?? JSON.stringify(definition, null, 2)} onChange={e => setAdvanced(e.target.value)}/><button className="button secondary" disabled={!advanced} onClick={() => { if (advanced === null) return; try { edit(advancedDefinition(JSON.parse(advanced))); setAdvanced(null); setError(null) } catch (e) { setError(e) } }}>Aplicar cambios avanzados al borrador</button></details>
    {notice && <Notice>{notice}</Notice>}{!!(error || references.error || reference.error) && <ErrorState error={error || references.error || reference.error}/>}
  </div>
}
