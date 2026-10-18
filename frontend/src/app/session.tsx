import { createContext, useContext } from 'react'
import type { Session } from '../api/client'

export const AuthContext = createContext<Session | null>(null)
export function useSession() { return useContext(AuthContext) }
export function usePermission(permission: string) {
  const session = useSession()
  return !!session?.user.permissions?.includes(permission)
}

export const operationReadPermissions = ['datasets:read', 'intake:read', 'recon:read', 'sentinel:read', 'delivery:read', 'documents:read', 'exceptions:read']
export function useOperationRead() {
  const session = useSession()
  return operationReadPermissions.some(permission => session?.user.permissions.includes(permission))
}
