import { ArrowDown, ArrowUp } from 'lucide-react'
import { Notice } from '../../components/ui'

export function PrimaryKeyPicker({ mode, columns, keys, historical, onMode, onKeys }: {
  mode: 'DEFINE' | 'NONE'
  columns: { target_name: string; source_name: string }[]
  keys: string[]
  historical: boolean
  onMode: (mode: 'DEFINE' | 'NONE') => void
  onKeys: (keys: string[]) => void
}) {
  function move(index: number, offset: number) {
    const next = [...keys]
    ;[next[index], next[index + offset]] = [next[index + offset], next[index]]
    onKeys(next)
  }
  return <fieldset className="delivery-key-picker"><legend>Clave primaria de la nueva tabla</legend>
    <label><input type="radio" name="primary-key-mode" checked={mode === 'DEFINE'} onChange={() => onMode('DEFINE')}/> Definir clave primaria</label>
    <label><input type="radio" name="primary-key-mode" checked={mode === 'NONE'} onChange={() => onMode('NONE')}/> Crear sin clave primaria</label>
    <p>Una clave primaria exige valores únicos y no nulos en toda la población. Identificador conserva texto y ceros iniciales; no garantiza unicidad. Crear una PK no ejecuta UPSERT.</p>
    {historical && <Notice>Configuración histórica con contrato v1: conserva su comportamiento anterior. Elegir una clave primaria publica un nuevo contrato v2.</Notice>}
    {mode === 'NONE' ? <Notice>La tabla se creará sin clave primaria y podrá admitir filas repetidas. Esta elección no comprueba ni garantiza unicidad.</Notice> : <>
      <p>Selecciona nombres finales del mapping. Las columnas elegidas se crearán NOT NULL. El preflight comprueba tipos, límites de clave y collation del destino sin cambios persistentes.</p>
      {columns.map(column => <label key={column.source_name}><input type="checkbox" aria-label={`Clave primaria ${column.target_name}`} checked={keys.includes(column.target_name)} onChange={event => onKeys(event.target.checked ? [...keys, column.target_name] : keys.filter(key => key !== column.target_name))}/><span><strong>{column.target_name}</strong><small>Origen: {column.source_name} {keys.includes(column.target_name) ? '· NOT NULL' : ''}</small></span></label>)}
      {!!keys.length && <ol aria-label="Orden de la clave primaria">{keys.map((key, index) => <li key={key}><strong>{key} · NOT NULL</strong> <button type="button" className="icon-button" aria-label={`Subir clave ${key}`} disabled={index === 0} onClick={() => move(index, -1)}><ArrowUp size={14}/></button><button type="button" className="icon-button" aria-label={`Bajar clave ${key}`} disabled={index === keys.length - 1} onClick={() => move(index, 1)}><ArrowDown size={14}/></button></li>)}</ol>}
      {!keys.length && <small className="mapping-error">Selecciona al menos una columna de clave primaria.</small>}
      {keys.length > 32 && <small className="mapping-error">La clave primaria admite hasta 32 columnas.</small>}
    </>}
  </fieldset>
}
