export type UserRole = "admin" | "user";

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  created_at: string;
  role: UserRole;
}

export interface LoginInput {
  email: string;
  password: string;
  remember_me: boolean;
}

export interface RegisterInput {
  email: string;
  password: string;
  display_name: string | null;
}

export const authKeys = {
  currentUser: ["auth", "current-user"] as const,
};
