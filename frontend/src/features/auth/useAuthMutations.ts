import { useMutation, useQueryClient } from "@tanstack/react-query";

import { login, logout, register } from "./api";
import {
  clearAuthenticatedIdentity,
  setAuthenticatedIdentity,
} from "./cacheBoundary";
import type { LoginInput, RegisterInput } from "./model";

export function useLoginMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: LoginInput) => login(input),
    onSuccess: (user) => {
      setAuthenticatedIdentity(queryClient, user);
    },
  });
}

export function useRegisterMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: RegisterInput) => register(input),
    onSuccess: (user) => {
      setAuthenticatedIdentity(queryClient, user);
    },
  });
}

export function useLogoutMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: logout,
    onSuccess: () => {
      clearAuthenticatedIdentity(queryClient);
    },
  });
}
