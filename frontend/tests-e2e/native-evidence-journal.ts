import { createHash } from 'node:crypto'
import { mkdir, open, readFile } from 'node:fs/promises'
import { join } from 'node:path'

function canonical(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    const encoded = JSON.stringify(value)
    if (encoded === undefined) throw new Error('Native step inputs must be explicit JSON values')
    return encoded
  }
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  const record = value as Record<string, unknown>
  return '{' + Object.keys(record).sort().map(key => JSON.stringify(key) + ':' + canonical(record[key])).join(',') + '}'
}

async function readJson(path: string) {
  try { return JSON.parse(await readFile(path, 'utf8')) }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined; throw error }
}

async function writeDurableNew(path: string, value: unknown) {
  const stream = await open(path, 'wx')
  try { await stream.writeFile(JSON.stringify(value, null, 2) + '\n'); await stream.sync() }
  finally { await stream.close() }
}

// A stopped browser never repeats an unacknowledged native mutation. The owner
// reconciles its actual server ID before authorizing continuation of that step.
export class NativeEvidenceJournal {
  constructor(private readonly directory: string) {}

  async perform<T>(name: string, request: unknown, action: () => Promise<T>): Promise<T> {
    if (!/^[a-z0-9_-]{1,120}$/.test(name)) throw new Error('Invalid private native-evidence step name')
    await mkdir(this.directory, { recursive: true })
    const requestSha256 = createHash('sha256').update(canonical(request)).digest('hex')
    const intentPath = join(this.directory, name + '.intent.private.json')
    const ackPath = join(this.directory, name + '.ack.private.json')
    const acknowledged = await readJson(ackPath)
    if (acknowledged) {
      if (acknowledged.request_sha256 !== requestSha256 || acknowledged.status !== 'ACKNOWLEDGED') throw new Error('Native evidence acknowledgement belongs to different frozen inputs')
      return acknowledged.response as T
    }
    if (await readJson(intentPath)) throw new Error('A previous native mutation has no acknowledgement: reconcile its actual server ID; automatic repetition is forbidden')
    await writeDurableNew(intentPath, { status: 'INTENT', request_sha256: requestSha256, at_utc: new Date().toISOString() })
    const response = await action()
    await writeDurableNew(ackPath, { status: 'ACKNOWLEDGED', request_sha256: requestSha256, at_utc: new Date().toISOString(), response })
    return response
  }
}
