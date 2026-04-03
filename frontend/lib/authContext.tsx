import {
  GoogleAuthProvider,
  User,
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  sendEmailVerification,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut,
  updateProfile,
} from "firebase/auth";
import { ReactNode, createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { firebaseConfigured, getFirebaseClient, missingFirebaseEnv } from "./firebaseClient";
import { API_URL } from "../utils/urls";
import { clearAuthToken, clearHostToken, clearStreamContext } from "../utils/streamContext";

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  configured: boolean;
  missingEnv: readonly string[];
  getIdToken: (forceRefresh?: boolean) => Promise<string | null>;
  login: (email: string, password: string) => Promise<User>;
  loginWithGoogle: () => Promise<User>;
  signup: (email: string, password: string, displayName?: string) => Promise<User>;
  updateDisplayName: (displayName: string) => Promise<User>;
  sendPasswordReset: (email: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

type Props = { children: ReactNode };

export function AuthProvider({ children }: Props) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [_profileVersion, setProfileVersion] = useState(0);

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

  const loginWithGoogle = useCallback(async (): Promise<User> => {
    const client = getFirebaseClient();
    if (!client) throw new Error("firebase_not_configured");
    const provider = new GoogleAuthProvider();
    provider.setCustomParameters({ prompt: "select_account" });
    const cred = await signInWithPopup(client.auth, provider);
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
    // Send verification email immediately after account creation (best-effort, never blocks signup)
    try {
      await sendEmailVerification(cred.user);
    } catch {
      // Verification email failure is non-fatal
    }
    setUser(cred.user);
    return cred.user;
  }, []);

  const updateDisplayNameValue = useCallback(async (displayName: string): Promise<User> => {
    const client = getFirebaseClient();
    const nextValue = displayName.trim();
    if (!client) throw new Error("firebase_not_configured");
    if (nextValue.length < 2) throw new Error("Display name must be at least 2 characters.");
    const activeUser = client.auth.currentUser || user;
    if (!activeUser) throw new Error("auth_required");
    await updateProfile(activeUser, { displayName: nextValue });
    await activeUser.getIdToken(true);
    setUser(client.auth.currentUser || activeUser);
    setProfileVersion((value) => value + 1);
    return client.auth.currentUser || activeUser;
  }, [user]);

  const sendPasswordResetValue = useCallback(async (email: string): Promise<void> => {
    const cleanEmail = email.trim();
    if (!cleanEmail) throw new Error("Please enter your email address.");
    const response = await fetch(`${API_URL}/api/auth/password-reset`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ email: cleanEmail }),
    });
    if (!response.ok) {
      let detail = "";
      try {
        const payload = (await response.json()) as { detail?: string };
        detail = String(payload.detail || "").trim();
      } catch {}
      if (detail === "invalid_email") throw new Error("Please enter a valid email address.");
      if (detail === "password_reset_rate_limited") throw new Error("Too many reset attempts. Please try again later.");
      if (detail === "password_reset_email_not_configured") throw new Error("Password reset email is not configured yet.");
      if (detail === "password_reset_email_delivery_failed") {
        throw new Error("Password reset email could not be sent. Please try again later.");
      }
      if (detail === "password_reset_unavailable" || detail === "firebase_admin_not_configured" || detail === "auth_provider_unavailable") {
        throw new Error("Password reset is temporarily unavailable. Please try again later.");
      }
      throw new Error("Failed to start password reset.");
    }
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
      loginWithGoogle,
      signup,
      updateDisplayName: updateDisplayNameValue,
      sendPasswordReset: sendPasswordResetValue,
      logout,
    }),
    [getIdToken, loading, login, loginWithGoogle, logout, sendPasswordResetValue, signup, updateDisplayNameValue, user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
