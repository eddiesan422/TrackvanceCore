import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ErrorState, Field, Notice } from '../../components/ui'

export type RestField = { name: string; path: string; logical_type: string; nullable: boolean; null_policy?: string; missing_policy?: string }
type ProposalField = RestField & { value_count: number; null_count: number; missing_count: number; incompatible_count: number; array_count: number; examples: string[]; warnings: string[]; selectable: boolean }
type Candidate = { path: string; fields: ProposalField[]; warnings: string[]; sampled_rows: number }
type Proposal = { draft_id: string; collections: Candidate[]; warnings: string[]; selection_required: boolean }

export function RestDiscovery({ draft, fields, apply }: { draft: Record<string, unknown>; fields: RestField[]; apply: (collection: string, fields: RestField[]) => void }) {
  const key = JSON.stringify(draft), currentKey = useRef(key), controller = useRef<AbortController | null>(null)
  currentKey.current = key
  const [proposal, setProposal] = useState<Proposal | null>(null), [collection, setCollection] = useState<string | null>(null), [selected, setSelected] = useState<string[]>([])
  useEffect(() => { controller.current?.abort(); setProposal(null); setCollection(null); setSelected([]) }, [key])
  useEffect(() => () => controller.current?.abort(), [])
  const discover = useMutation({ mutationFn: async ({ input, inputKey }: { input: Record<string, unknown>; inputKey: string }) => {
    controller.current?.abort(); controller.current = new AbortController()
    const draftId = crypto.randomUUID()
    const result = await api<Proposal>('/connections/discover-rest', { method: 'POST', signal: controller.current.signal, body: JSON.stringify({ ...input, draft_id: draftId }) })
    if (currentKey.current !== inputKey || result.draft_id !== draftId) throw new Error('El borrador cambió; consulta de nuevo su estructura.')
    return result
  }, onSuccess: result => {
    setProposal(result)
    const only = result.collections.length === 1 ? result.collections[0] : null
    setCollection(only?.path ?? null); setSelected(only?.fields.filter(f => f.selectable).slice(0, 100).map(f => f.path) || [])
  } })
  const candidate = proposal?.collections.find(item => item.path === collection)
  const changes = candidate?.fields.filter(item => fields.some(field => field.path === item.path && (field.name !== item.name || field.logical_type !== item.logical_type))) || []
  return <section className="panel form-stack" onChange={event => event.stopPropagation()}>
    <h3>Detectar estructura del borrador</h3><p>Consulta segura desde el servidor, hasta 100 registros y 30 segundos. La propuesta no guarda conexiones ni crea datasets.</p>
    <div><button className="button secondary" type="button" disabled={discover.isPending} onClick={() => discover.mutate({ input: draft, inputKey: key })}>{discover.isPending ? 'Consultando API…' : 'Consultar API y detectar estructura'}</button>{discover.isPending && <button className="text-button" type="button" onClick={() => controller.current?.abort()}>Cancelar consulta</button>}</div>
    {discover.error && <ErrorState error={discover.error}/>}
    {proposal && <>{proposal.warnings.length > 0 && <Notice>Advertencias: {proposal.warnings.join(', ')}</Notice>}{proposal.collections.length === 0 ? <Notice>No se observó una colección de objetos. Configura el mapping manualmente.</Notice> : <>
      <Field label="Seleccionar colección"><select value={collection === null ? '__select__' : collection} onChange={event => { const value = event.target.value; setCollection(value); setSelected(proposal.collections.find(item => item.path === value)?.fields.filter(f => f.selectable).slice(0, 100).map(f => f.path) || []) }}><option value="__select__">Selecciona una colección</option>{proposal.collections.map(item => <option key={item.path} value={item.path}>{item.path || '(raíz)'} · {item.sampled_rows} registros observados</option>)}</select></Field>
      {candidate && <>{candidate.sampled_rows === 0 ? <Notice>La muestra está vacía; no permite inferir campos. Conserva o configura manualmente el mapping.</Notice> : <>
        {candidate.warnings.length > 0 && <Notice>{candidate.warnings.join(', ')}</Notice>}
        {changes.length > 0 && <Notice>Propuestas distintas en {changes.map(field => field.path).join(', ')}. Al aplicar se conservan las decisiones de campos existentes; revisa sus tipos y nombres manualmente.</Notice>}
        <div className="table-scroll"><table aria-label="Propuesta de estructura REST"><thead><tr><th>Incluir</th><th>Ruta / nombre propuesto</th><th>Tipo</th><th>Valores / nulos / ausentes</th><th>Ejemplos y advertencias</th></tr></thead><tbody>{candidate.fields.map(field => <tr key={field.path}><td><input aria-label={`Incluir ${field.path}`} type="checkbox" checked={selected.includes(field.path)} disabled={!field.selectable || !selected.includes(field.path) && selected.length >= 100} onChange={event => setSelected(items => event.target.checked ? [...items, field.path] : items.filter(path => path !== field.path))}/></td><td>{field.path}<small className="table-subtitle">{field.name}</small></td><td>{field.logical_type}</td><td>{field.value_count} / {field.null_count} / {field.missing_count}</td><td>{field.examples.join(' · ')}<small className="table-subtitle">{field.warnings.join(', ')}</small></td></tr>)}</tbody></table></div>
        <button className="button secondary" type="button" disabled={!selected.length} onClick={() => {
          const added = candidate.fields.filter(field => selected.includes(field.path) && !fields.some(item => item.path === field.path)).map(({ name, path, logical_type, nullable, null_policy, missing_policy }) => ({ name, path, logical_type, nullable, null_policy, missing_policy }))
          const aliases = new Set(fields.map(field => field.name))
          for (const field of added) { const base = field.name; let suffix = 1; while (aliases.has(field.name)) field.name = `${base}_${++suffix}`; aliases.add(field.name) }
          apply(candidate.path, [...fields, ...added].slice(0, 100)); setProposal(null)
        }}>Aplicar propuesta al formulario</button><p className="muted">Aplicar permite revisar y editar el mapping; guardar sigue siendo una acción separada. Los arreglos no se expanden.</p>
      </>}</>}
    </>}</>}
  </section>
}
