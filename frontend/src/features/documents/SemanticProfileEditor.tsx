import type { RecordData } from '../../api/client'
import { Field } from '../../components/ui'

export type SemanticProfileDraft = {
  schema_version?: string; id: string; name: string; revision: number; document_type: string; description: string
  fields: RecordData[]; tables: RecordData[]
}
export const semanticFieldTypes: Record<string, string> = { STRING: 'Texto', INT64: 'Entero', DECIMAL: 'Decimal', DATE: 'Fecha', BOOLEAN: 'Sí o no' }
const newField = (number: number): RecordData => ({ name: `field_${number}`, label: `Campo ${number}`, description: '', logical_type: 'STRING', required: true, critical: false, business_key: false })

function Fields({ values, onChange, disabled, scope, maximum }: { values: RecordData[]; onChange: (values: RecordData[]) => void; disabled: boolean; scope: string; maximum: number }) {
  const change = (index: number, patch: RecordData) => onChange(values.map((field, position) => position === index ? { ...field, ...patch } : field))
  return <>
    {values.map((field, index) => <fieldset className="document-semantic-field" key={index} disabled={disabled}>
      <legend>{scope} · campo {index + 1}</legend>
      <div className="document-engine-fields">
        <Field label={`Nombre de columna ${scope} ${index + 1}`} hint="Letra inicial; después letras, números o guion bajo."><input value={field.name} required pattern="[A-Za-z][A-Za-z0-9_]{0,62}" maxLength={63} onChange={event => change(index, { name: event.target.value })}/></Field>
        <Field label={`Información a extraer ${scope} ${index + 1}`}><input value={field.label} required maxLength={160} onChange={event => change(index, { label: event.target.value })}/></Field>
        <Field label={`Tipo de dato ${scope} ${index + 1}`}><select value={field.logical_type} onChange={event => change(index, { logical_type: event.target.value })}>{Object.entries(semanticFieldTypes).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></Field>
      </div>
      <Field label={`Descripción del campo ${scope} ${index + 1}`} hint="Describe su significado y el contexto que lo identifica."><textarea value={field.description || ''} maxLength={1000} onChange={event => change(index, { description: event.target.value })}/></Field>
      <div className="document-semantic-field-options">
        <label><input type="checkbox" checked={!!field.required} onChange={event => change(index, { required: event.target.checked })}/> Obligatorio · {scope} {index + 1}</label>
        <label><input type="checkbox" checked={!!field.critical} onChange={event => change(index, { critical: event.target.checked })}/> Dato crítico · {scope} {index + 1}</label>
        <label><input type="checkbox" checked={!!field.business_key} onChange={event => change(index, { business_key: event.target.checked })}/> Clave de negocio · {scope} {index + 1}</label>
      </div>
      {field.logical_type === 'DECIMAL' && <div className="document-engine-fields">
        <Field label={`Separador decimal ${scope} ${index + 1}`}><select value={field.decimal_separator || '.'} onChange={event => change(index, { decimal_separator: event.target.value })}><option value=".">Punto</option><option value=",">Coma</option></select></Field>
        <Field label={`Separador de miles ${scope} ${index + 1}`}><select value={field.thousands_separator ?? ''} onChange={event => change(index, { thousands_separator: event.target.value || null })}><option value="">Sin separador</option><option value=".">Punto</option><option value=",">Coma</option><option value=" ">Espacio</option></select></Field>
      </div>}
      {field.logical_type === 'DATE' && <Field label={`Formato de fecha ${scope} ${index + 1}`} hint="Por ejemplo, %Y-%m-%d o %d/%m/%Y."><input value={field.date_format || '%Y-%m-%d'} maxLength={40} required onChange={event => change(index, { date_format: event.target.value })}/></Field>}
      <button type="button" className="button secondary small" disabled={values.length <= 1} onClick={() => onChange(values.filter((_, position) => position !== index))}>Quitar campo {scope} {index + 1}</button>
    </fieldset>)}
    <button type="button" className="button secondary small" disabled={disabled || values.length >= maximum} onClick={() => {
      let next = values.length + 1
      while (values.some(field => field.name === `field_${next}`)) next++
      onChange([...values, newField(next)])
    }}>Añadir campo a {scope}</button>
  </>
}

export default function SemanticProfileEditor({ value, onChange, disabled }: { value: SemanticProfileDraft; onChange: (value: SemanticProfileDraft) => void; disabled: boolean }) {
  const changeTable = (index: number, patch: RecordData) => onChange({ ...value, tables: value.tables.map((table, position) => position === index ? { ...table, ...patch } : table) })
  return <div className="document-semantic-editor" aria-label="Personalizar información del perfil">
    <p>Este perfil personalizado tendrá su propio contrato guardado. Los documentos y datasets anteriores conservan sus revisiones.</p>
    <div className="document-engine-fields">
      <Field label="Nombre del perfil personalizado"><input value={value.name} required maxLength={140} disabled={disabled} onChange={event => onChange({ ...value, name: event.target.value })}/></Field>
      <Field label="Revisión del perfil personalizado" hint="Cada revisión guardada identifica un contrato inmutable."><input type="number" value={value.revision} min={1} max={100000} required disabled={disabled} onChange={event => onChange({ ...value, revision: Number(event.target.value) })}/></Field>
      <Field label="Tipo de documento del perfil"><select value={value.document_type} disabled={disabled} onChange={event => onChange({ ...value, document_type: event.target.value })}><option value="INVOICE">Factura</option><option value="STATEMENT">Extracto</option><option value="NARRATIVE">Narrativo</option><option value="CERTIFICATE">Certificado o formulario</option><option value="CUSTOM">Otro documento</option></select></Field>
    </div>
    <Field label="Información solicitada al documento"><textarea value={value.description} required maxLength={2000} disabled={disabled} onChange={event => onChange({ ...value, description: event.target.value })}/></Field>
    <h3>Campos del documento</h3>
    <Fields values={value.fields} onChange={fields => onChange({ ...value, fields })} disabled={disabled} scope="documento" maximum={100}/>
    <h3>Tablas y colecciones</h3>
    {value.tables.map((table, index) => <fieldset className="document-semantic-collection" key={index} disabled={disabled}><legend>Colección {index + 1}</legend>
      <div className="document-engine-fields">
        <Field label={`Nombre de colección ${index + 1}`}><input value={table.name} required pattern="[A-Za-z][A-Za-z0-9_]{0,62}" maxLength={63} onChange={event => changeTable(index, { name: event.target.value })}/></Field>
        <Field label={`Forma de colección ${index + 1}`}><select value={table.kind || 'TABLE'} onChange={event => changeTable(index, { kind: event.target.value })}><option value="TABLE">Tabla impresa</option><option value="COLLECTION">Colección de información solicitada</option></select></Field>
      </div>
      <Field label={`Información de colección ${index + 1}`} hint="Describe qué constituye una fila y qué información debe conservar."><textarea value={table.description} required maxLength={1000} onChange={event => changeTable(index, { description: event.target.value })}/></Field>
      <label><input type="checkbox" checked={!!table.required} onChange={event => changeTable(index, { required: event.target.checked })}/> Colección obligatoria {index + 1}</label>
      <Fields values={table.fields} onChange={fields => changeTable(index, { fields })} disabled={disabled} scope={`colección ${index + 1}`} maximum={40}/>
      <button type="button" className="button secondary small" onClick={() => onChange({ ...value, tables: value.tables.filter((_, position) => position !== index) })}>Quitar colección {index + 1}</button>
    </fieldset>)}
    <button type="button" className="button secondary" disabled={disabled || value.tables.length >= 10} onClick={() => {
      let next = value.tables.length + 1
      while (value.tables.some(table => table.name === `collection_${next}`)) next++
      onChange({ ...value, tables: [...value.tables, { name: `collection_${next}`, description: '', kind: 'COLLECTION', required: false, fields: [newField(1)] }] })
    }}>Añadir tabla o colección</button>
  </div>
}
