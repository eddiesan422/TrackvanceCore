import type { RecordData } from '../../api/client'

export type SinkType = 'POSTGRESQL' | 'SQLSERVER'
export type TargetMode = 'EXISTING_TABLE' | 'CREATE_TABLE'
export type WriteStrategy = 'APPEND' | 'OVERWRITE' | 'UPSERT' | 'CREATE_AND_LOAD'
export type DeliveryColumnType = 'STRING' | 'INT64' | 'DECIMAL' | 'DATE' | 'TIMESTAMP' | 'BOOLEAN'

export interface DestinationConfig {
  host: string
  port: number
  database: string
  username: string
  connect_timeout: number
  query_timeout: number
  sslmode?: string
  encryption?: string
}

export interface DeliveryDestination {
  id: string
  name: string
  sink_type: SinkType
  enabled: boolean
  deleted?: boolean
  version: number
  host: string
  port: number
  database: string
  username: string
  options: Record<string, unknown>
  destination_version_id?: string
  current_version_id?: string
  config_hash?: string
  last_test_status?: string | null
  last_test_at?: string | null
  last_test_message?: string | null
  created_at?: string
  updated_at?: string
}

export interface DestinationInput {
  name: string
  sink_type: SinkType
  host: string
  port: number
  database: string
  username: string
  options: Record<string, unknown>
  password?: string
  destination_id?: string
}

export interface DestinationTestResult {
  status: 'SUCCESS' | 'FAILED'
  code?: string
  message: string
  tested_at: string
}

export interface DatasetColumn {
  name: string
  logical_type: string
  native_type?: string | null
  nullable?: boolean
  numeric?: boolean
  semantic_tag?: string | null
  precision?: number | null
  scale?: number | null
  length?: number | null
}

export interface DatasetVersion {
  id: string
  version: number
  filename?: string
  row_count?: number
  created_at?: string
  source_type?: string
  schema?: DatasetColumn[]
  canonical_artifact_id?: string
  sha256?: string
}

export interface DatasetRecord {
  id: string
  name: string
  versions?: DatasetVersion[]
}

export interface TargetColumn {
  name: string
  native_type: string
  logical_type: DeliveryColumnType
  nullable: boolean
  has_default?: boolean
  identity?: boolean
  generated?: boolean
  precision?: number | null
  scale?: number | null
  length?: number | null
}

export interface TableMetadata {
  schema_name: string
  table_name: string
  columns: TargetColumn[]
  constraints: { type: 'PRIMARY_KEY' | 'UNIQUE'; columns: string[]; unique?: boolean }[]
  destination_version_id?: string
}

export interface ColumnMapping {
  source_name: string
  source_type: string
  target_name: string
  target_type: DeliveryColumnType
  ordinal: number
  nullable: boolean
  selected: boolean
  precision?: number
  scale?: number
  length?: number
}

export interface DeliveryTarget {
  mode: TargetMode
  schema_name: string
  table_name: string
  create_schema: boolean
}

export interface DeliveryDraft {
  schema_version: 1
  dataset_version_id: string
  destination_id: string
  destination_version_id: string
  target: DeliveryTarget
  columns: Omit<ColumnMapping, 'selected' | 'source_type'>[]
  write_strategy: WriteStrategy
  upsert_keys: string[]
}

export interface DeliveryPreview {
  dataset_version_id: string
  columns: { source_name: string; target_name: string; ordinal: number; source_type: string; target_type: string }[]
  source_rows: Record<string, unknown>[]
  destination_rows: Record<string, unknown>[]
  sampled_rows: number
}

export interface PreflightCheck {
  code: string
  status: 'PASS' | 'FAIL' | 'WARN' | 'WARNING'
  message: string
}

export interface DeliveryPreflight {
  status: 'PASS' | 'FAIL'
  checks: PreflightCheck[]
  warnings?: string[]
  schema_fingerprint?: string
}

export interface DeliveryConfiguration extends RecordData {
  id: string
  name: string
  version: number
  dataset_id: string
  dataset_version_id?: string
  dataset_name?: string
  destination_id?: string
  destination_version_id?: string
  destination_name?: string
  config: RecordData
  status?: string
  created_at?: string
}

export function destinationVersionId(destination?: DeliveryDestination | null) {
  return destination?.destination_version_id || destination?.current_version_id || ''
}

export function collectionItems<T>(value: unknown): T[] {
  if (Array.isArray(value)) return value as T[]
  if (value && typeof value === 'object' && 'items' in value) {
    const items = (value as { items?: unknown }).items
    return Array.isArray(items) ? items as T[] : []
  }
  return []
}
