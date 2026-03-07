import {
  User,
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
  updateProfile,
} from "firebase/auth";
import { ReactNode, createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { firebaseConfigured, getFirebaseClient, missingFirebaseEnv } from "./firebaseClient";
import { clearAuthToken, clearHostToken, clearStreamContext } from "../utils/streamContext";

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  configured: boolean;
  missingEnv: readonly string[];
  getIdToken: (forceRefresh?: boolean) => Promise<string | null>;
  login: (email: string, password: string) => Promise<User>;
  signup: (email: string, password: string, displayName?: string) => Promise<User>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

type Props = { children: ReactNode };

export function AuthProvider({ children }: Props) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!firebaseConfigured) {
      setLoading(false);
      return;
    }
    const client = getFirebaseClient();
    if (!client) {
      setLoading(false);
      return;
    }
    const unsubscribe = onAuthStateChanged(client.auth, (nextUser) => {
      setUser(nextUser);
      setLoading(false);
    });
    return () => unsubscribe();
  }, []);

  const getIdToken = useCallback(
    async (forceRefresh = false): Promise<string | null> => {
      if (!firebaseConfigured) return null;
      const activeUser = user || getFirebaseClient()?.auth.currentUser || null;
      if (!activeUser) return null;
      return activeUser.getIdToken(forceRefresh);
    },
    [user],
  );

  const login = useCallback(async (email: string, password: string): Promise<User> => {
    const client = getFirebaseClient();
    if (!client) throw new Error("firebase_not_configured");
    const cred = await signInWithEmailAndPassword(client.auth, email, password);
    setUser(cred.user);
    return cred.user;
  }, []);

  const signup = useCallback(async (email: string, password: string, displayName?: string): Promise<User> => {
    const client = getFirebaseClient();
    if (!client) throw new Error("firebase_not_configured");
    const cred = await createUserWithEmailAndPassword(client.auth, email, password);
    if (displayName?.trim()) {
      await updateProfile(cred.user, { displayName: displayName.trim() });
    }
    await cred.user.getIdToken(true);
    setUser(cred.user);
    return cred.user;
  }, []);

  const logout = useCallback(async (): Promise<void> => {
    const client = getFirebaseClient();
    try {
      if (client) await signOut(client.auth);
    } finally {
      clearHostToken();
      clearAuthToken();
      clearStreamContext();
      setUser(null);
      if (typeof window !== "undefined") {
        try {
          window.sessionStorage.clear();
        } catch {}
        if (window.location.pathname !== "/" || window.location.search) {
          window.location.replace("/");
        }
      }
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      configured: firebaseConfigured,
      missingEnv: missingFirebaseEnv,
      getIdToken,
      login,
      signup,
      logout,
    }),
    [getIdToken, loading, login, logout, signup, user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
