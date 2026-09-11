import { apiRequest, apiRequestNoContent } from "./client";
import {
  isSetupStatus,
  isUser,
  type Credentials,
  type SetupStatus,
  type User,
} from "./types";

export function getSetupStatus(): Promise<SetupStatus> {
  return apiRequest("/setup/status", isSetupStatus);
}

export function getCurrentUser(): Promise<User> {
  return apiRequest("/auth/me", isUser);
}

export function setup(credentials: Credentials): Promise<User> {
  return apiRequest("/setup", isUser, { method: "POST", body: credentials });
}

export const createInitialAdmin = setup;

export function login(credentials: Credentials): Promise<User> {
  return apiRequest("/auth/login", isUser, { method: "POST", body: credentials });
}

export function logout(): Promise<void> {
  return apiRequestNoContent("/auth/logout", { method: "POST" });
}
