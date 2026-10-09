import { useEffect, useRef } from 'react'

export function ColumnSelection({ title, selected, total, onChange }: { title: string; selected: number; total: number; onChange: (all: boolean) => void }) {
  const checkbox = useRef<HTMLInputElement>(null)
  const partial = selected > 0 && selected < total
  useEffect(() => { if (checkbox.current) checkbox.current.indeterminate = partial }, [partial])
  return <div className="classification-actions"><label className="checkbox-row"><input ref={checkbox} type="checkbox" aria-label={`Selección de ${title}`} aria-checked={partial ? 'mixed' : selected === total && total > 0} checked={selected === total && total > 0} onChange={event => onChange(event.target.checked)}/>{title}: {selected} / {total} seleccionadas</label>
    <button className="text-button" type="button" onClick={() => onChange(true)}>Seleccionar todas{title === 'todas las fuentes' ? '' : ` · ${title}`}</button>
    <button className="text-button" type="button" onClick={() => onChange(false)}>Deseleccionar todas{title === 'todas las fuentes' ? '' : ` · ${title}`}</button>
  </div>
}
