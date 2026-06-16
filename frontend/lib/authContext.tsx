import {
  GoogleAuthProvider,
  User,
  createUserWithEmailAndPassword,
  deleteUser,
  getAdditionalUserInfo,
  linkWithPopup,
  onAuthStateChanged,
  reauthenticateWithPopup,
  sendEmailVerification,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut,
  updateProfile,
} from "firebase/auth";
import { ReactNode, createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { firebaseConfigured, getFirebaseClient, missingFirebaseEnv, skipEmailVerification } from "./firebaseClient";
import { API_URL } from "../utils/urls";
import { clearAuthToken, clearHostToken, clearStreamContext } from "../utils/streamContext";

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  configured: boolean;
  missingEnv: readonly string[];
  getIdToken: (forceRefresh?: boolean) => Promise<string | null>;
  getGoogleAccessToken: () => string | null;
  /** True if the current user has google.com in providerData. */
  hasGoogleLinked: () => boolean;
  /** Links Google as an additional provider on the current user (for
   *  email/password accounts), or re-authorizes if already linked but the
   *  cached access token is gone/expired. Returns the fresh access token. */
  connectGoogleForDocs: () => Promise<string>;
  login: (email: string, password: string) => Promise<User>;
  loginWithGoogle: () => Promise<User>;
  signupWithGoogle: () => Promise<User>;
  signup: (email: string, password: string, displayName?: string) => Promise<User>;
  updateDisplayName: (displayName: string) => Promise<User>;
  sendPasswordReset: (email: string) => Promise<void>;
  logout: () => Promise<void>;
};

const GOOGLE_DOCS_SCOPE = "https://www.googleapis.com/auth/documents.readonly";
// Drive scope lets us open the Google Picker so users browse their docs
// instead of pasting URLs; readonly is the narrowest scope Picker will accept
// to list arbitrary user files.
const GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly";
const GOOGLE_TOKEN_STORAGE_KEY = "wt:google_access_token";

const AuthContext = createContext<AuthContextValue | null>(null);

type Props = { children: ReactNode };

function authError(code: string, message: string): Error & { code: string } {
  const err = new Error(message) as Error & { code: string };
  err.code = code;
  return err;
}

export function AuthProvider({ children }: Props) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [, setProfileVersion] = useState(0);

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

  const buildGoogleProvider = useCallback(() => {
    const provider = new GoogleAuthProvider();
    provider.setCustomParameters({ prompt: "select_account" });
    // Docs scope lets the backend read content via the Docs API; Drive scope
    // lets the frontend open Google Picker so users browse their files. Token
    // is stashed in sessionStorage and forwarded as X-Google-Access-Token at
    // ingest time.
    provider.addScope(GOOGLE_DOCS_SCOPE);
    provider.addScope(GOOGLE_DRIVE_SCOPE);
    return provider;
  }, []);

  const stashGoogleAccessToken = useCallback((token: string | null | undefined) => {
    if (!token || typeof window === "undefined") return;
    try {
      window.sessionStorage.setItem(GOOGLE_TOKEN_STORAGE_KEY, token);
    } catch {}
  }, []);

  const continueWithGoogle = useCallback(async (allowNewUser: boolean): Promise<User> => {
    const client = getFirebaseClient();
    if (!client) throw new Error("firebase_not_configured");
    const provider = buildGoogleProvider();
    const cred = await signInWithPopup(client.auth, provider);
    const isNewUser = Boolean(getAdditionalUserInfo(cred)?.isNewUser);
    if (!allowNewUser && isNewUser) {
      try {
        await deleteUser(cred.user);
      } catch {
        // Best-effort cleanup; the user is still signed out below.
      }
      await signOut(client.auth);
      setUser(null);
      throw authError("auth/google-signup-required", "Please sign up before using Google sign-in.");
    }
    const oauthCred = GoogleAuthProvider.credentialFromResult(cred);
    stashGoogleAccessToken(oauthCred?.accessToken);
    setUser(cred.user);
    return cred.user;
  }, [buildGoogleProvider, stashGoogleAccessToken]);

  const getGoogleAccessToken = useCallback((): string | null => {
    if (typeof window === "undefined") return null;
    try {
      return window.sessionStorage.getItem(GOOGLE_TOKEN_STORAGE_KEY);
    } catch {
      return null;
    }
  }, []);

  const hasGoogleLinked = useCallback((): boolean => {
    const activeUser = user || getFirebaseClient()?.auth.currentUser || null;
    return Boolean(activeUser?.providerData?.some((p) => p.providerId === "google.com"));
  }, [user]);

  const connectGoogleForDocs = useCallback(async (): Promise<string> => {
    const client = getFirebaseClient();
    if (!client) throw new Error("firebase_not_configured");
    const activeUser = user || client.auth.currentUser;
    if (!activeUser) throw new Error("auth_required");

    const provider = buildGoogleProvider();
    const alreadyLinked = activeUser.providerData.some((p) => p.providerId === "google.com");
    // linkWithPopup fails if google.com is already linked (auth/provider-already-linked).
    // For that case, reauthenticate to get a fresh access token instead.
    const cred = alreadyLinked
      ? await reauthenticateWithPopup(activeUser, provider)
      : await linkWithPopup(activeUser, provider);
    const oauthCred = GoogleAuthProvider.credentialFromResult(cred);
    const accessToken = oauthCred?.accessToken;
    if (!accessToken) {
      throw new Error("google_access_token_missing");
    }
    stashGoogleAccessToken(accessToken);
    setUser(client.auth.currentUser);
    return accessToken;
  }, [buildGoogleProvider, stashGoogleAccessToken, user]);

  const loginWithGoogle = useCallback(async (): Promise<User> => {
    return continueWithGoogle(false);
  }, [continueWithGoogle]);

  const signupWithGoogle = useCallback(async (): Promise<User> => {
    return continueWithGoogle(true);
  }, [continueWithGoogle]);

  const signup = useCallback(async (email: string, password: string, displayName?: string): Promise<User> => {
    const client = getFirebaseClient();
    if (!client) throw new Error("firebase_not_configured");
    const cred = await createUserWithEmailAndPassword(client.auth, email, password);
    if (displayName?.trim()) {
      await updateProfile(cred.user, { displayName: displayName.trim() });
    }
    await cred.user.getIdToken(true);
    if (!skipEmailVerification) {
      // Send verification email immediately after account creation (best-effort, never blocks signup)
      try {
        await sendEmailVerification(cred.user);
      } catch {
        // Verification email failure is non-fatal
      }
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
      getGoogleAccessToken,
      hasGoogleLinked,
      connectGoogleForDocs,
      login,
      loginWithGoogle,
      signupWithGoogle,
      signup,
      updateDisplayName: updateDisplayNameValue,
      sendPasswordReset: sendPasswordResetValue,
      logout,
    }),
    [
      connectGoogleForDocs,
      getGoogleAccessToken,
      getIdToken,
      hasGoogleLinked,
      loading,
      login,
      loginWithGoogle,
      logout,
      sendPasswordResetValue,
      signup,
      signupWithGoogle,
      updateDisplayNameValue,
      user,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
