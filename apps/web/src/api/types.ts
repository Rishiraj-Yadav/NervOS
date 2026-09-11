export interface SetupStatus {
  setup_complete: boolean;
}

export interface User {
  id: number;
  username: string;
  role: string;
  is_active: boolean;
}

export interface Credentials {
  username: string;
  password: string;
}

export interface ApiErrorDetail {
  code: string;
  message: string;
}

export function isSetupStatus(value: unknown): value is SetupStatus {
  return isRecord(value) && typeof value.setup_complete === "boolean";
}

export function isUser(value: unknown): value is User {
  return (
    isRecord(value) &&
    Number.isInteger(value.id) &&
    typeof value.username === "string" &&
    typeof value.role === "string" &&
    typeof value.is_active === "boolean"
  );
}

export function parseApiError(value: unknown): ApiErrorDetail | null {
  if (!isRecord(value) || !isRecord(value.error)) {
    return null;
  }
  const { code, message } = value.error;
  return typeof code === "string" && typeof message === "string"
    ? { code, message }
    : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
