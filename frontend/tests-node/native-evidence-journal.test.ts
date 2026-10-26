import { mkdtemp, realpath, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, join, resolve, sep } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { NativeEvidenceJournal } from '../tests-e2e/native-evidence-journal'

const ownedDirectories: string[] = []
async function journal() {
  const path = await mkdtemp(join(tmpdir(), 'trackvance-native-journal-'))
  ownedDirectories.push(path)
  return new NativeEvidenceJournal(path)
}
afterEach(async () => {
  const temporaryRoot = await realpath(tmpdir())
  for (const path of ownedDirectories.splice(0)) {
    const target = await realpath(path)
    if (!target.startsWith(temporaryRoot + sep) || !basename(target).startsWith('trackvance-native-journal-') || resolve(target) === resolve(temporaryRoot)) throw new Error('Refusing cleanup outside the exact owned journal test directory')
    await rm(target, { recursive: true })
  }
})

describe('Native mutation evidence is durable before continuation', () => {
  it('reuses an acknowledged native ID and rejects changed frozen inputs without another action', async () => {
    const evidence = await journal()
    const action = vi.fn(async () => ({ id: 'native-run-one', status: 'QUEUED' }))
    const first = await evidence.perform('intake-run', { source_version: 'version-one', configuration_id: 'contract-one' }, action)
    const resumed = await evidence.perform('intake-run', { configuration_id: 'contract-one', source_version: 'version-one' }, action)
    expect(resumed).toEqual(first)
    expect(action).toHaveBeenCalledTimes(1)
    await expect(evidence.perform('intake-run', { source_version: 'different-version', configuration_id: 'contract-one' }, action)).rejects.toThrow('different frozen inputs')
    expect(action).toHaveBeenCalledTimes(1)
  })

  it('stops after an unacknowledged action instead of repeating an uncertain server mutation', async () => {
    const evidence = await journal()
    const action = vi.fn(async () => { throw new Error('Response lost after a potentially committed server action') })
    await expect(evidence.perform('delivery-run', { configuration_id: 'delivery-one' }, action)).rejects.toThrow('Response lost')
    await expect(evidence.perform('delivery-run', { configuration_id: 'delivery-one' }, action)).rejects.toThrow('reconcile its actual server ID')
    expect(action).toHaveBeenCalledTimes(1)
  })
})
