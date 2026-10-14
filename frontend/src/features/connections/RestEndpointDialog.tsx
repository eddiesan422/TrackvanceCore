import { useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api, post } from '../../api/client'
import { ErrorState, Field, Modal, Notice } from '../../components/ui'

type RestField = { name: string; path: string; logical_type: string; nullable: boolean }
type Existing = { id: string; name: string; version: number; options: Record<string, unknown> }

export function RestEndpointDialog({ mode, existing, close, saved }: { mode: 'SOURCE' | 'DESTINATION'; existing?: Existing; close: () => void; saved: (id: string) => void }) {
  const opts = existing?.options || {}
  const [name, setName] = useState(existing?.name || ''), [url, setUrl] = useState(String(opts.url || ''))
  const [auth, setAuth] = useState(String(opts.auth || 'NONE')), [header, setHeader] = useState(String(opts.api_key_header || 'X-API-Key'))
  const [secret, setSecret] = useState(''), [collection, setCollection] = useState(String(opts.collection || 'records'))
  const [fields, setFields] = useState<RestField[]>((opts.fields as RestField[] | undefined) || [{ name: 'id', path: 'id', logical_type: 'STRING', nullable: false }])
  const paging = (opts.pagination || {}) as Record<string, unknown>
  const [pagination, setPagination] = useState(String(paging.mode || 'PAGE')), [parameter, setParameter] = useState(String(paging.parameter || 'page'))
  const [sizeParameter, setSizeParameter] = useState(String(paging.size_parameter || 'page_size')), [nextCursor, setNextCursor] = useState(String(paging.next_cursor_path || 'next_cursor'))
  const [pageSize, setPageSize] = useState(Number(opts.page_size || 1000)), [idempotent, setIdempotent] = useState(Boolean(opts.idempotency_supported))
  const [maxRows, setMaxRows] = useState(Number(opts.max_rows || 5_000_000)), [maxPages, setMaxPages] = useState(Number(opts.max_pages || 10000))
  const [maxBytes, setMaxBytes] = useState(Number(opts.max_bytes || 2 * 1024 ** 3)), [timeout, setTimeout] = useState(Number(opts.total_timeout_seconds || 1800))
  const [tested, setTested] = useState(false)
  const revision = useRef(0)
  const root = mode === 'SOURCE' ? '/connections' : '/delivery/destinations'
  function payload() {
    const options = { url, auth, api_key_header: header, timeout_seconds: 15, ...(mode === 'SOURCE' ? {
      collection, fields, pagination: { mode: pagination, ...(pagination !== 'NONE' ? { parameter, size_parameter: sizeParameter, start: pagination === 'PAGE' ? 1 : 0 } : {}), ...(pagination === 'CURSOR' ? { next_cursor_path: nextCursor } : {}) },
      page_size: pageSize, max_rows: maxRows, max_pages: maxPages, max_bytes: maxBytes, total_timeout_seconds: timeout,
    } : { idempotency_supported: idempotent, idempotency_header: 'Idempotency-Key' }) }
    return { name, ...(mode === 'SOURCE' ? { source_type: 'REST' } : { sink_type: 'REST' }), options, ...(secret ? { password: secret } : {}) }
  }
  const test = useMutation({ mutationFn: (generation: number) => {
    if (generation !== revision.current) throw new Error('La configuracion cambio antes de la prueba.')
    return post(`${root}/test`, { ...payload(), ...(existing ? { [mode === 'SOURCE' ? 'connection_id' : 'destination_id']: existing.id } : {}) })
  }, onSuccess: (_, generation) => { if (generation === revision.current) setTested(true) } })
  const save = useMutation({ mutationFn: () => {
    const input = payload()
    if (!existing) return post<{ id: string }>(root, input)
    return api<{ id: string }>(`${root}/${existing.id}`, { method: 'PATCH', body: JSON.stringify({ name, options: input.options, ...(secret ? { password: secret } : {}), version: existing.version }) })
  }, onSuccess: result => saved(result.id) })
  const valid = Boolean(name.trim() && url && (auth === 'NONE' || secret || existing) && (mode !== 'SOURCE' || fields.every(field => field.name && field.path)))
  return <Modal open wide title={existing ? 'Editar endpoint REST' : mode === 'SOURCE' ? 'Nueva conexión REST' : 'Nuevo destino REST'} onOpenChange={open => { if (!open && !save.isPending) close() }}>
    <form className="form-stack" onChange={() => { revision.current += 1; setTested(false); test.reset() }} onSubmit={event => { event.preventDefault(); if (tested) save.mutate() }}>
      <fieldset disabled={save.isPending}><div className="form-grid">
        <Field label="Nombre"><input required value={name} maxLength={160} onChange={event => setName(event.target.value)}/></Field>
        <Field label="URL del endpoint" hint="HTTPS, sin credenciales ni parámetros en la URL. Las redes internas requieren autorización del operador."><input required type="url" maxLength={2048} value={url} onChange={event => setUrl(event.target.value)}/></Field>
        <Field label="Autenticación"><select value={auth} onChange={event => setAuth(event.target.value)}><option value="NONE">Ninguna</option><option value="API_KEY">API key en encabezado</option><option value="BEARER">Bearer token</option></select></Field>
        {auth === 'API_KEY' && <Field label="Encabezado API key"><input value={header} onChange={event => setHeader(event.target.value)}/></Field>}
        {auth !== 'NONE' && <Field label="Credencial" hint="Se guarda cifrada; vuelve a indicarla al cambiar URL o autenticación."><input type="password" autoComplete="new-password" value={secret} onChange={event => setSecret(event.target.value)}/></Field>}
        {mode === 'SOURCE' && <><Field label="Colección JSON" hint="Ruta de objeto; vacía para un arreglo en la raíz."><input value={collection} onChange={event => setCollection(event.target.value)}/></Field>
          <Field label="Paginación"><select value={pagination} onChange={event => { setPagination(event.target.value); setParameter(event.target.value === 'OFFSET' ? 'offset' : event.target.value === 'CURSOR' ? 'cursor' : 'page'); setSizeParameter(event.target.value === 'PAGE' ? 'page_size' : 'limit') }}><option value="NONE">Sin paginación</option><option value="PAGE">Por página</option><option value="OFFSET">Por desplazamiento</option><option value="CURSOR">Por cursor</option></select></Field>
          {pagination !== 'NONE' && <><Field label="Parámetro de página, offset o cursor"><input value={parameter} onChange={event => setParameter(event.target.value)}/></Field><Field label="Parámetro de tamaño"><input value={sizeParameter} onChange={event => setSizeParameter(event.target.value)}/></Field></>}
          {pagination === 'CURSOR' && <Field label="Ruta del siguiente cursor"><input value={nextCursor} onChange={event => setNextCursor(event.target.value)}/></Field>}
          <Field label="Filas por página"><input type="number" min={1} max={10000} value={pageSize} onChange={event => setPageSize(Number(event.target.value))}/></Field>
          <Field label="Máximo de filas"><input type="number" min={1} max={5000000} value={maxRows} onChange={event => setMaxRows(Number(event.target.value))}/></Field>
          <Field label="Máximo de páginas"><input type="number" min={1} max={100000} value={maxPages} onChange={event => setMaxPages(Number(event.target.value))}/></Field>
          <Field label="Máximo de bytes"><input type="number" min={1024} max={2147483648} value={maxBytes} onChange={event => setMaxBytes(Number(event.target.value))}/></Field>
          <Field label="Tiempo máximo total (segundos)"><input type="number" min={1} max={1800} value={timeout} onChange={event => setTimeout(Number(event.target.value))}/></Field>
        </>}
      </div></fieldset>
      {mode === 'SOURCE' ? <><h3>Campos seleccionados</h3><div className="table-scroll"><table><thead><tr><th>Nombre</th><th>Ruta JSON</th><th>Tipo explícito</th><th>Admite nulos</th><th/></tr></thead><tbody>{fields.map((field, index) => <tr key={index}>
        <td><input aria-label={`Nombre campo ${index + 1}`} value={field.name} onChange={event => setFields(current => current.map((item, i) => i === index ? { ...item, name: event.target.value } : item))}/></td>
        <td><input aria-label={`Ruta campo ${index + 1}`} value={field.path} onChange={event => setFields(current => current.map((item, i) => i === index ? { ...item, path: event.target.value } : item))}/></td>
        <td><select aria-label={`Tipo campo ${index + 1}`} value={field.logical_type} onChange={event => setFields(current => current.map((item, i) => i === index ? { ...item, logical_type: event.target.value } : item))}>{['STRING', 'INT64', 'DECIMAL', 'DATE', 'TIMESTAMP', 'BOOLEAN'].map(type => <option key={type}>{type}</option>)}</select></td>
        <td><input type="checkbox" aria-label={`Nulos campo ${index + 1}`} checked={field.nullable} onChange={event => setFields(current => current.map((item, i) => i === index ? { ...item, nullable: event.target.checked } : item))}/></td>
        <td><button type="button" className="text-button" disabled={fields.length === 1} onClick={() => { revision.current += 1; setTested(false); setFields(current => current.filter((_, i) => i !== index)) }}>Quitar</button></td>
      </tr>)}</tbody></table></div><button type="button" className="button secondary" disabled={fields.length >= 100} onClick={() => { revision.current += 1; setTested(false); setFields(current => [...current, { name: '', path: '', logical_type: 'STRING', nullable: true }]) }}>Añadir campo</button>
      <Notice>Conserva STRING para identificadores y ceros iniciales. No se expanden arreglos. Una extracción fallida no publica versiones parciales.</Notice></> : <><label><input type="checkbox" checked={idempotent} onChange={event => setIdempotent(event.target.checked)}/> El receptor garantiza idempotencia real para la misma identidad y payload</label><Notice>La prueba usa HEAD y no envía filas. Cada lote requiere confirmación explícita. HTTP 202 y respuestas inciertas requieren revisión externa; no se reenvían automáticamente.</Notice></>}
      {tested && <Notice success>Prueba de acceso completada con la configuración actual.</Notice>}{test.error && <ErrorState error={test.error}/>}{save.error && <ErrorState error={save.error}/>}
      <div className="modal-footer"><button type="button" className="button secondary" onClick={close} disabled={save.isPending}>Cancelar</button><button type="button" className="button secondary" onClick={() => test.mutate(revision.current)} disabled={!valid || test.isPending || save.isPending}>Probar {mode === 'SOURCE' ? 'lectura GET' : 'acceso HEAD'}</button><button className="button primary" disabled={!valid || !tested || test.isPending || save.isPending}>Guardar</button></div>
    </form>
  </Modal>
}
