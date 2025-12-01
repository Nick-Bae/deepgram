import Link from "next/link";
import type { CSSProperties } from "react";

export default function AdminHome() {
  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Admin Tools</h1>
      <p style={styles.subtitle}>Quick links to maintenance views.</p>
      <ul style={styles.list}>
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
