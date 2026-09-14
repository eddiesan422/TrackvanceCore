import { createContext, useContext } from 'react'
import type { Session } from '../api/client'

export const AuthContext = createContext<Session | null>(null)
export function useSession() { return useContext(AuthContext) }
export function usePermission(permission: string) {
  const session = useSession()
  return !!session?.user.permissions?.includes(permission)
}
