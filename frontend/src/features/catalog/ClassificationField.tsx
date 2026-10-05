import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, post } from '../../api/client'
import { usePermission } from '../../app/session'
import { ErrorState, Field, Modal, Notice } from '../../components/ui'

export type Classification = { macro_domain_id: string | null; domain_id: string | null }
export type GovernanceEntity = { id: string; name: string; description?: string; active: boolean; version: number; macro_domain_id?: string }
export const unclassified: Classification = { macro_domain_id: null, domain_id: null }

/** Classification is shared metadata. Changing a macro domain explicitly clears its incompatible child. */
export function ClassificationField({ value, onChange, disabled = false, legacyValue, fixed = false }: {
  value: Classification; onChange: (value: Classification) => void; disabled?: boolean; legacyValue?: string; fixed?: boolean
}) {
  const cache = useQueryClient(), canManage = usePermission('domains:manage')
  const [creating, setCreating] = useState<'macro' | 'domain' | null>(null), [name, setName] = useState('')
  const macros = useQuery({ queryKey: ['governance-macrodomains', 'active'], queryFn: () => api<{ items: GovernanceEntity[] }>('/governance/macrodomains?active=true&limit=200'), enabled: !fixed })
  const domains = useQuery({ queryKey: ['governance-domains', value.macro_domain_id, 'active'], queryFn: () => api<{ items: GovernanceEntity[] }>(`/governance/domains?macro_domain_id=${encodeURIComponent(value.macro_domain_id!)}&active=true&limit=200`), enabled: !!value.macro_domain_id && !fixed })
  const create = useMutation({ mutationFn: () => post<GovernanceEntity>(creating === 'macro' ? '/governance/macrodomains' : '/governance/domains', { name: name.trim(), ...(creating === 'domain' ? { macro_domain_id: value.macro_domain_id } : {}) }), onSuccess: entity => {
    void cache.invalidateQueries({ queryKey: ['governance-macrodomains'] }); void cache.invalidateQueries({ queryKey: ['governance-domains'] }); void cache.invalidateQueries({ queryKey: ['catalog-tree'] })
    onChange(creating === 'macro' ? { macro_domain_id: entity.id, domain_id: null } : { ...value, domain_id: entity.id }); setCreating(null); setName('')
  } })
  return <div className="classification-fields">
    {fixed ? <Notice>La clasificación vigente pertenece al dataset y se conserva al cargar versiones. Puedes administrarla desde Catálogo.</Notice> : <>
      <div className="form-grid"><Field label="Macrodominio (opcional)"><select value={value.macro_domain_id || ''} disabled={disabled || macros.isPending} onChange={event => onChange({ macro_domain_id: event.target.value || null, domain_id: null })}><option value="">Sin macrodominio</option>{(macros.data?.items || []).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
        <Field label="Dominio (opcional)" hint="Solo se muestran dominios del macrodominio seleccionado."><select value={value.domain_id || ''} disabled={disabled || !value.macro_domain_id || domains.isPending} onChange={event => onChange({ ...value, domain_id: event.target.value || null })}><option value="">Sin dominio</option>{(domains.data?.items || []).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field></div>
      {canManage && <div className="classification-actions"><button className="text-button" type="button" disabled={disabled} onClick={() => { setCreating('macro'); setName(''); create.reset() }}>Crear macrodominio</button><button className="text-button" type="button" disabled={disabled || !value.macro_domain_id} onClick={() => { setCreating('domain'); setName(''); create.reset() }}>Crear dominio</button></div>}
      {(!value.macro_domain_id || !value.domain_id) && <small>Clasificación incompleta: aparecerá en Pendientes de clasificación. Puedes cargar y versionar sin completarla.</small>}
      {macros.error && <ErrorState error={macros.error} retry={() => { void macros.refetch() }}/>} {domains.error && <ErrorState error={domains.error} retry={() => { void domains.refetch() }}/>} </>}
    {legacyValue && <small>Área heredada: {legacyValue}. Es información histórica.</small>}
    <Modal open={!!creating} onOpenChange={open => { if (!open && !create.isPending) setCreating(null) }} title={creating === 'macro' ? 'Crear macrodominio' : 'Crear dominio'} description="Esta entidad compartida estará disponible para otros datasets. Se comprueban duplicados en el servidor.">
      <form className="form-stack" onSubmit={event => { event.preventDefault(); event.stopPropagation(); if (name.trim()) create.mutate() }}><Field label="Nombre"><input autoFocus required maxLength={160} value={name} disabled={create.isPending} onChange={event => setName(event.target.value)}/></Field>{create.error && <ErrorState error={create.error}/>}<div className="modal-footer"><button type="button" className="button secondary" disabled={create.isPending} onClick={() => setCreating(null)}>Cancelar</button><button className="button primary" disabled={!name.trim() || create.isPending}>{create.isPending ? 'Creando…' : 'Crear'}</button></div></form>
    </Modal>
  </div>
}
