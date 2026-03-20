import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useState } from "react";
import type { CSSProperties } from "react";

import { useAuth } from "../../lib/authContext";
import { fetchAuthMe } from "../../lib/backendAuth";

export default function AdminHome() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken } = useAuth();
  const [checking, setChecking] = useState(true);
  const [broadcastUrl, setBroadcastUrl] = useState("/host");

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      void router.replace(`/login?next=${encodeURIComponent("/admin")}`);
      return;
    }
    getIdToken()
      .then(async (token) => {
        if (!token) { void router.replace("/login"); return; }
        const me = await fetchAuthMe(token);
        if (!me.user.isMaster) { void router.replace("/"); return; }
        const membership = me.memberships?.find((m) => m.orgId === me.currentOrgId) ?? me.memberships?.[0];
        if (membership?.slug && membership?.orgId) {
          setBroadcastUrl(`/host/c/${encodeURIComponent(membership.slug)}/broadcast?orgId=${encodeURIComponent(membership.orgId)}`);
        }
        setChecking(false);
      })
      .catch(() => { void router.replace("/"); });
  }, [authLoading, user, getIdToken, router]);

  if (authLoading || checking) {
    return <div style={{ minHeight: "100vh", background: "#f5f6f8" }} />;
  }

  return (
    <div style={styles.page}>
      <div style={styles.nav}>
        <Link href={broadcastUrl} style={styles.navLink}>← Broadcast Dashboard</Link>
        <Link href="/" style={styles.navLink}>Home</Link>
      </div>
      <h1 style={styles.title}>Admin Tools</h1>
      <p style={styles.subtitle}>Quick links to maintenance views.</p>
      <ul style={styles.list}>
        <li style={styles.item}>
          <Link href="/admin/dashboard" style={styles.link}>System Dashboard</Link>
          <span style={styles.note}>Monitor all organizations, live rooms, billing status, and usage.</span>
        </li>
        <li style={styles.item}>
          <Link href="/admin/display" style={styles.link}>Display Speed</Link>
          <span style={styles.note}>Adjust how fast captions roll on the public display.</span>
        </li>
        <li style={styles.item}>
          <Link href="/admin/sermon-prep" style={styles.link}>Sermon Prep</Link>
          <span style={styles.note}>Paste Korean sermon text, draft translate, edit English, and save final pairs.</span>
        </li>
        <li style={styles.item}>
          <Link href="/admin/examples" style={styles.link}>Translation Examples</Link>
          <span style={styles.note}>Review, correct, dedupe, trim, export few-shots.</span>
        </li>
        <li style={styles.item}>
          <Link href="/admin/prompt" style={styles.link}>Custom Prompt</Link>
          <span style={styles.note}>Edit admin guidance appended to the translator prompt.</span>
        </li>
      </ul>
    </div>
  );
}

const styles: Record<string, CSSProperties> = {
  page: {
    padding: "32px",
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    background: "#f5f6f8",
    minHeight: "100vh",
  },
  nav: {
    display: "flex",
    gap: "12px",
    marginBottom: "24px",
  },
  navLink: {
    fontSize: "14px",
    color: "#6b7280",
    textDecoration: "none",
    padding: "6px 12px",
    background: "white",
    borderRadius: 8,
    border: "1px solid #e5e7eb",
  },
  title: {
    fontSize: "26px",
    fontWeight: 700,
    marginBottom: "8px",
  },
  subtitle: {
    color: "#4b5563",
    marginBottom: "16px",
  },
  list: {
    listStyle: "none",
    padding: 0,
    margin: 0,
  },
  item: {
    background: "white",
    borderRadius: 12,
    padding: "16px 18px",
    boxShadow: "0 10px 30px rgba(15,23,42,0.06)",
    display: "flex",
    gap: "10px",
    alignItems: "center",
    marginBottom: 10,
  },
  link: {
    fontWeight: 600,
    color: "#111827",
    textDecoration: "none",
  },
  note: {
    color: "#6b7280",
    fontSize: "14px",
  },
};
