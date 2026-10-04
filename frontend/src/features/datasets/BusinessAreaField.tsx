import { useState } from 'react'
import type { RecordData } from '../../api/client'
import { Field } from '../../components/ui'

const defaultAreas = ['Operaciones', 'Finanzas', 'Logística', 'Ventas']
const newArea = '__new_domain__'
const collator = new Intl.Collator('es', { numeric: true, sensitivity: 'base' })

export function availableBusinessAreas(datasets: RecordData[]) {
  const unique = new Map<string, string>()
  for (const area of [...defaultAreas, ...datasets.map(dataset => String(dataset.domain || ''))]) {
    const key = area.trim().normalize('NFC').toLocaleLowerCase('es')
    if (key && !unique.has(key)) unique.set(key, area)
  }
  return [...unique.values()].sort(collator.compare)
}

export function businessAreaError(value: string) {
  if (!value.trim()) return 'Indica un área de negocio.'
  if (Array.from(value.trim()).length > 80) return 'El área de negocio admite hasta 80 caracteres.'
  return ''
}

export function BusinessAreaField({ value, onChange, datasets = [], disabled = false, fixedValue }: {
  value: string; onChange: (value: string) => void; datasets?: RecordData[]; disabled?: boolean; fixedValue?: string
}) {
  const [adding, setAdding] = useState(false)
  const fixed = fixedValue !== undefined
  const options = fixed ? [fixedValue] : availableBusinessAreas(datasets)
  const error = adding && !fixed ? businessAreaError(value) : ''
  return <div className="area-field-stack">
    <Field label="Área de negocio" hint={fixed ? 'Las nuevas versiones conservan el área del dataset existente.' : undefined}>
      <select required={!fixed} value={fixed ? fixedValue : adding ? newArea : value} disabled={disabled || fixed} onChange={event => {
        const create = event.target.value === newArea
        setAdding(create)
        onChange(create ? '' : event.target.value)
      }}>
        {options.map(area => <option key={area} value={area}>{area || 'Sin área registrada'}</option>)}
        {!fixed && <option value={newArea}>＋ Agregar nueva área</option>}
      </select>
    </Field>
    {adding && !fixed && <><Field label="Nueva área de negocio" hint="Máximo 80 caracteres.">
      <input required autoFocus maxLength={160} value={value} disabled={disabled} aria-invalid={!!error}
        onChange={event => onChange(event.target.value)} placeholder="Ej. Riesgos"/>
    </Field>{error && <small role="alert">{error}</small>}</>}
  </div>
}
