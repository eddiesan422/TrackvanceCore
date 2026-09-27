export interface Permission { code: string; group: string; label: string; dependencies: string[]; delegable: boolean }
export interface Role { id: string; name: string; description: string; active: boolean; deleted: boolean; protected: boolean; system_key: string | null; version: number; user_count: number; permissions: string[] }
export interface CredentialDelivery { id: string; status: string; error_code: string | null; created_at: string; sent_at: string | null; failed_at: string | null }
export interface ExternalIdentity { id: string; provider: string; issuer: string; subject: string; email_at_link: string; linked_at: string; last_login_at: string | null }
export interface User { id: string; name: string; first_name: string | null; last_name: string | null; username: string; email: string; role: string; role_id: string; role_version: number; active: boolean; deleted: boolean; version: number; permissions: string[]; must_change_password: boolean; temporary_password_expires_at: string | null; credential_delivery: CredentialDelivery | null; external_identities: ExternalIdentity[]; last_login_at?: string | null }
export interface Items<T> { items: T[]; total: number }
