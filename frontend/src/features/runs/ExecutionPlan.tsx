import type { RecordData } from '../../api/client'
import { Badge, number } from '../../components/ui'

const reasons: Record<string, string> = {
  STANDARD_MEMORY_BUDGET: 'La carga estimada cabe en el presupuesto de Polars.',
  SPARK_BOUNDED_EXECUTION: 'La carga necesita procesamiento por particiones con memoria acotada.',
  EXPLICIT_ENGINE_SELECTION: 'Se conserva el motor seleccionado por el usuario.',
  ENGINE_UNAVAILABLE: 'PySpark o Java compatible no está disponible en esta instalación.',
  RESOURCE_MEMORY_INSUFFICIENT: 'La carga estimada excede el presupuesto de Polars.',
  RESOURCE_SPARK_MEMORY_INSUFFICIENT: 'Los recursos Spark requeridos exceden el presupuesto configurado.',
  RESOURCE_DISK_INSUFFICIENT: 'No hay espacio temporal libre suficiente para completar la ejecución.',
}

export function ExecutionPlan({ plan }: { plan: RecordData }) {
  return <section className="panel"><div className="panel-heading"><div><h2>Plan de ejecución</h2><p>{reasons[plan.reason_code] || plan.reason_code}</p></div><Badge value={plan.allowed === false ? 'FAILED_PRECONDITION' : 'APPROVED'}>{plan.allowed === false ? 'Bloqueado' : 'Permitido'}</Badge></div>
    <dl className="detail-list"><div><dt>Selección</dt><dd>{plan.requested_engine === 'AUTO' ? 'Automático' : plan.requested_engine}</dd></div><div><dt>Motor efectivo</dt><dd>{plan.engine} · {plan.engine_version || 'Versión no disponible'}</dd></div><div><dt>Datos de entrada</dt><dd>{number((plan.estimated_input_bytes || 0) / 1024 ** 2)} MiB</dd></div><div><dt>Memoria de la carga estimada</dt><dd>{number((plan.estimated_working_set_bytes || 0) / 1024 ** 2)} MiB</dd></div><div><dt>Espacio temporal requerido</dt><dd>{number((plan.required_temp_bytes || 0) / 1024 ** 2)} MiB</dd></div>{plan.spark_parameters && <><div><dt>Modo Spark</dt><dd>{plan.deployment_mode} · {plan.spark_parameters.master}</dd></div><div><dt>Presupuesto Spark</dt><dd>{number((plan.spark_memory_budget_bytes || 0) / 1024 ** 2)} MiB</dd></div><div><dt>Memoria Spark estimada</dt><dd>{number((plan.spark_estimated_working_set_bytes || 0) / 1024 ** 2)} MiB</dd></div></>}</dl><p className="muted">Las estimaciones del plan se verifican junto con los límites efectivos del proceso. Una selección explícita conserva los controles de recursos.</p>
  </section>
}
