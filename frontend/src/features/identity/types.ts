export interface Permission { code: string; group: string; label: string; dependencies: string[]; delegable: boolean }
export interface Role { id: string; name: string; description: string; active: boolean; deleted: boolean; protected: boolean; system_key: string | null; version: number; user_count: number; permissions: string[] }
export interface ExternalIdentity { id: string; provider: string; issuer: string; subject: string; email_at_link: string; linked_at: string; last_login_at: string | null }
export interface User { id: string; name: string; first_name: string | null; last_name: string | null; username: string; email: string; role: string; role_id: string; role_version: number; active: boolean; deleted: boolean; version: number; permissions: string[]; must_change_password: boolean; temporary_password_expires_at: string | null; password_changed_at?: string | null; external_identities: ExternalIdentity[]; last_login_at?: string | null }
export interface TemporaryCredentials { username: string; temporary_password: string; expires_at: string; must_change_password: true }
export interface UserCredentialIssue { user: User; temporary_credentials: TemporaryCredentials }
export interface Items<T> { items: T[]; total: number }
