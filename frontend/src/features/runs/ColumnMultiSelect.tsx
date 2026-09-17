import { useMemo, useState } from 'react'
import { Check, ChevronDown, Search, X } from 'lucide-react'

export interface DatasetColumn {
  name: string
  logical_type?: string | null
  native_type?: string | null
  semantic_tag?: string | null
  nullable?: boolean | null
  numeric?: boolean
}

const numericType = /^(?:U?INT(?:EGER)?|LONG|SHORT|BYTE|FLOAT|DOUBLE|DECIMAL|NUMERIC|NUMBER|REAL|BIGINT|SMALLINT)/i

export function isNumericColumn(column: DatasetColumn) {
  if (typeof column.numeric === 'boolean') return column.numeric
  return numericType.test(String(column.logical_type || column.native_type || '').replaceAll(' ', ''))
}

function columnType(column: DatasetColumn) {
  return column.logical_type || column.native_type || 'Tipo no disponible'
}

export function ColumnMultiSelect({
  label,
  hint,
  columns,
  selected,
  onChange,
  disabled = false,
  numericFirst = false,
  numericOnly = false,
  showSelectAll = true,
}: {
  label: string
  hint: string
  columns: DatasetColumn[]
  selected: string[]
  onChange: (value: string[]) => void
  disabled?: boolean
  numericFirst?: boolean
  numericOnly?: boolean
  showSelectAll?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const options = useMemo(() => {
    const available = numericOnly ? columns.filter(isNumericColumn) : [...columns]
    const known = new Set(available.map(column => column.name))
    const current = new Map(columns.map(column => [column.name, column]))
    const historical = selected
      .filter(name => !known.has(name))
      .map(name => current.get(name) || ({ name, logical_type: 'No disponible en la versión actual', numeric: false }))
    return [...available, ...historical].sort((left, right) => {
      if (numericFirst && isNumericColumn(left) !== isNumericColumn(right)) return isNumericColumn(left) ? -1 : 1
      return left.name.localeCompare(right.name, 'es', { sensitivity: 'base' })
    })
  }, [columns, numericFirst, numericOnly, selected])
  const eligibleNames = useMemo(
    () => columns.filter(column => !numericOnly || isNumericColumn(column)).map(column => column.name),
    [columns, numericOnly],
  )
  const visible = options.filter(column => `${column.name} ${columnType(column)}`.toLowerCase().includes(search.trim().toLowerCase()))
  const toggle = (name: string) => onChange(selected.includes(name) ? selected.filter(value => value !== name) : [...selected, name])
  const selectedEligible = eligibleNames.filter(name => selected.includes(name)).length
  const allEligibleSelected = eligibleNames.length > 0 && selectedEligible === eligibleNames.length
  const someEligibleSelected = selectedEligible > 0 && !allEligibleSelected
  const toggleAll = () => {
    const eligible = new Set(eligibleNames)
    onChange(allEligibleSelected
      ? selected.filter(name => !eligible.has(name))
      : [...eligibleNames, ...selected.filter(name => !eligible.has(name))])
  }

  return <fieldset className="column-multi" disabled={disabled}>
    <legend>{label}</legend>
    <button
      type="button"
      className="column-multi-trigger"
      aria-expanded={open}
      aria-label={`${label}: ${open ? 'cerrar selector' : 'abrir selector'}`}
      onClick={() => setOpen(value => !value)}
    >
      <span className={selected.length ? 'column-selection' : 'column-placeholder'}>
        {selected.length ? <>{selected.slice(0, 2).map(name => <code key={name}>{name}</code>)}{selected.length > 2 && <small>+{selected.length - 2}</small>}</> : 'Selecciona una o más columnas'}
      </span>
      <ChevronDown size={16}/>
    </button>
    {open && <div className="column-multi-menu">
      <div className="column-search"><Search size={15}/><input value={search} onChange={event => setSearch(event.target.value)} aria-label={`Buscar en ${label}`} placeholder="Buscar columna…"/>{search && <button type="button" aria-label={`Limpiar búsqueda de ${label}`} onClick={() => setSearch('')}><X size={13}/></button>}</div>
      <div className="column-options" role="group" aria-label={`Opciones de ${label}`}>
        {showSelectAll && <label className={`column-select-all ${allEligibleSelected ? 'selected' : ''}`}>
          <input
            type="checkbox"
            checked={allEligibleSelected}
            disabled={!eligibleNames.length}
            aria-label={`Todos (${eligibleNames.length} columnas)`}
            ref={element => { if (element) element.indeterminate = someEligibleSelected }}
            onChange={toggleAll}
          />
          <span><strong>Todos</strong><small>{eligibleNames.length ? `${eligibleNames.length} columnas elegibles` : 'Sin columnas elegibles'}</small></span>
          {allEligibleSelected && <Check size={14}/>}
        </label>}
        {visible.length ? visible.map(column => {
          const checked = selected.includes(column.name)
          const unavailable = !columns.some(item => item.name === column.name)
          const incompatible = numericOnly && !unavailable && !isNumericColumn(column)
          return <label className={checked ? 'selected' : ''} key={column.name}>
            <input type="checkbox" checked={checked} aria-label={`${column.name} (${columnType(column)})`} onChange={() => toggle(column.name)}/>
            <span><strong>{column.name}</strong><small>{columnType(column)}{column.semantic_tag ? ` · ${column.semantic_tag}` : ''}</small></span>
            {unavailable ? <em>Histórica</em> : incompatible ? <em>No numérica</em> : isNumericColumn(column) ? <em>Numérica</em> : null}
            {checked && <Check size={14}/>}
          </label>
        }) : <p>{search ? 'No hay columnas que coincidan con la búsqueda.' : numericOnly ? 'El esquema no contiene columnas numéricas.' : 'El esquema no contiene columnas.'}</p>}
      </div>
      {selected.length > 0 && <button type="button" className="column-clear" onClick={() => onChange([])}>Limpiar selección</button>}
    </div>}
    <small>{hint}</small>
  </fieldset>
}
