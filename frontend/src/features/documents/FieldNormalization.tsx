import { useState } from 'react'
import type { RecordData } from '../../api/client'
import { Field } from '../../components/ui'

export default function FieldNormalization({ field, onChange }: { field: RecordData; onChange: (value: RecordData | undefined) => void }) {
  const [currency, setCurrency] = useState('')
  const [aliases, setAliases] = useState('')
  const normalization = field.normalization
  const update = (value: RecordData) => onChange({ ...normalization, ...value })
  const equivalents = normalization?.currency_equivalences || {}
  const aliasValues = aliases.split(';').map(value => value.trim()).filter(Boolean)
  const explicitAliases = aliasValues.length > 0 && aliasValues.every(value => /[\p{L}\p{N}]/u.test(value))
  return <div className="document-field-normalization">
    <label><input type="checkbox" checked={!!normalization} onChange={event => onChange(event.target.checked ? { version: 1, numeric_semantics: 'SIGNED', rounding: 'EXACT' } : undefined)}/> Declarar normalización de {field.name}</label>
    {normalization && <>
      <p>El texto observado se conserva. Estas reglas pertenecen a la revisión de plantilla y se prueban antes de activarla.</p>
      {field.logical_type === 'DECIMAL' && <>
        <Field label={`Signo del importe ${field.name}`}><select value={normalization.numeric_semantics || 'SIGNED'} onChange={event => update({ numeric_semantics: event.target.value })}><option value="SIGNED">Conservar importe firmado</option><option value="DEDUCTION_MAGNITUDE">Magnitud de descuento o deducción</option></select></Field>
        <Field label={`Decimales de ${field.name}`} hint="Vacío conserva la precisión observada."><input type="number" min={0} max={18} value={normalization.decimal_scale ?? ''} onChange={event => update({ decimal_scale: event.target.value === '' ? undefined : Number(event.target.value) })}/></Field>
        <Field label={`Redondeo de ${field.name}`}><select value={normalization.rounding || 'EXACT'} onChange={event => update({ rounding: event.target.value })}><option value="EXACT">Exacto: rechazar pérdida de precisión</option><option value="HALF_UP">Mitades hacia arriba</option><option value="HALF_EVEN">Mitades al par</option></select></Field>
        {normalization.numeric_semantics === 'DEDUCTION_MAGNITUDE' && <small>Esta declaración transforma una deducción firmada en su magnitud. No aplica a otros importes y no modifica el texto observado.</small>}
      </>}
      {field.logical_type === 'STRING' && <fieldset className="document-currency-equivalences"><legend>Equivalencias declaradas de moneda</legend>
        <p>Declara códigos, nombres o representaciones con contexto, como US$. Un símbolo aislado no permite asumir la moneda.</p>
        {Object.entries(equivalents).map(([code, values]) => <div key={code}><strong>{code}</strong><span>{(values as string[]).join('; ')}</span><button type="button" className="button secondary small" onClick={() => update({ currency_equivalences: Object.fromEntries(Object.entries(equivalents).filter(([key]) => key !== code)) })}>Quitar equivalencias de {code}</button></div>)}
        <Field label={`Código de moneda para ${field.name}`}><input value={currency} maxLength={3} placeholder="COP" onChange={event => setCurrency(event.target.value.toUpperCase())}/></Field>
        <Field label={`Representaciones equivalentes de ${field.name}`} hint="Separa las representaciones con punto y coma."><input value={aliases} placeholder="COP; COP (peso colombiano)" onChange={event => setAliases(event.target.value)}/></Field>
        <button type="button" className="button secondary small" disabled={!/^[A-Z]{3}$/.test(currency) || !explicitAliases} onClick={() => { update({ currency_equivalences: { ...equivalents, [currency]: [...new Set(aliasValues)] } }); setCurrency(''); setAliases('') }}>Añadir equivalencias de moneda</button>
      </fieldset>}
    </>}
  </div>
}
