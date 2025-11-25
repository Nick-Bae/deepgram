import { useEffect, useMemo, useState } from "react";
import type { CSSProperties, ChangeEvent } from "react";
import { API_URL } from "../../utils/urls";

type Example = {
  timestamp: string;
  source_lang: string;
  target_lang: string;
  stt_text: string;
  auto_translation?: string;
  final_translation?: string;
  corrected?: boolean;
};

type ListResponse = {
  total: number;
  invalid: number;
  items: Example[];
};

const PAGE_SIZE = 50;
const toMessage = (err: unknown) => {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  try {
    return JSON.stringify(err);
  } catch {
    return "Unknown error";
  }
};

export default function ExamplesAdmin() {
  const [source, setSource] = useState("ko");
  const [target, setTarget] = useState("en");
  const [corrected, setCorrected] = useState<"all" | "true" | "false">("all");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<"desc" | "asc">("desc");
  const [data, setData] = useState<ListResponse | null>(null);
  const [items, setItems] = useState<Example[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [page, setPage] = useState(0); // 0-based

  const query = useMemo(() => {
    const params = new URLSearchParams();
    if (source) params.set("source", source);
    if (target) params.set("target", target);
    if (search) params.set("search", search);
    if (corrected !== "all") params.set("corrected", corrected === "true" ? "true" : "false");
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(page * PAGE_SIZE));
    params.set("sort", sort);
    return params.toString();
  }, [source, target, search, corrected, page, sort]);

  const load = async () => {
    setLoading(true);
    setMessage(null);
    try {
      const res = await fetch(`${API_URL}/api/examples?${query}`);
      if (!res.ok) throw new Error(await res.text());
      const json = await res.json();
      setData(json);
      setItems(json.items || []);
    } catch (err: unknown) {
      setMessage(toMessage(err) || "Failed to load");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  const updateRow = async (example: Example) => {
    const final_translation = example.final_translation || example.auto_translation || "";
    if (!final_translation.trim()) return;
    setMessage("Saving…");
    try {
      const res = await fetch(`${API_URL}/api/examples/update`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          timestamp: example.timestamp,
          final_translation,
          corrected: true,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      setMessage("Saved");
      load();
    } catch (err: unknown) {
      setMessage(toMessage(err) || "Save failed");
    }
  };

  const deleteRow = async (timestamp: string) => {
    if (!window.confirm("Delete this row?")) return;
    setMessage("Deleting…");
    try {
      const res = await fetch(`${API_URL}/api/examples`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timestamp }),
      });
      if (!res.ok) throw new Error(await res.text());
      setMessage("Deleted");
      load();
    } catch (err: unknown) {
      setMessage(toMessage(err) || "Delete failed");
    }
  };

  const cleanLog = async () => {
    setMessage("Cleaning…");
    try {
      const res = await fetch(`${API_URL}/api/examples/clean`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dedupe: true, keep: 400 }),
      });
      if (!res.ok) throw new Error(await res.text());
      const j = await res.json();
      setMessage(`Cleaned: ${j.before} → ${j.after}`);
      load();
    } catch (err: unknown) {
      setMessage(toMessage(err) || "Clean failed");
    }
  };

  const exportFewshot = async () => {
    setMessage("Exporting…");
    try {
      const res = await fetch(`${API_URL}/api/examples/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, target, max: 6, include_auto: true }),
      });
      if (!res.ok) throw new Error(await res.text());
      const j = await res.json();
      setMessage(`Exported ${j.exported} → ${j.output}`);
    } catch (err: unknown) {
      setMessage(toMessage(err) || "Export failed");
    }
  };

  const handleEdit = (timestamp: string, value: string) => {
    setItems((prev) => prev.map((it) => (it.timestamp === timestamp ? { ...it, final_translation: value } : it)));
  };

  const rows = items;
  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  const canPrev = page > 0;
  const canNext = page + 1 < totalPages;

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Translation Examples Admin</h1>

      <div style={styles.filters}>
        <label style={styles.label}>
          Source
          <input value={source} onChange={(e) => setSource(e.target.value)} style={styles.input} />
        </label>
        <label style={styles.label}>
          Target
          <input value={target} onChange={(e) => setTarget(e.target.value)} style={styles.input} />
        </label>
        <label style={styles.label}>
          Corrected
          <select
            value={corrected}
            onChange={(e: ChangeEvent<HTMLSelectElement>) =>
              setCorrected(e.target.value as "all" | "true" | "false")
            }
            style={styles.input}
          >
            <option value="all">All</option>
            <option value="true">Corrected</option>
            <option value="false">Auto only</option>
          </select>
        </label>
        <label style={styles.label}>
          Sort
          <select
            value={sort}
            onChange={(e: ChangeEvent<HTMLSelectElement>) => setSort(e.target.value as "desc" | "asc")}
            style={styles.input}
          >
            <option value="desc">Newest first</option>
            <option value="asc">Oldest first</option>
          </select>
        </label>
        <label style={{ ...styles.label, flex: 2 }}>
          Search
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="stt / translation contains"
            style={styles.input}
          />
        </label>
        <button onClick={load} style={styles.button} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
        <button onClick={cleanLog} style={styles.button}>Clean (dedupe + keep 400)</button>
        <button onClick={exportFewshot} style={styles.button}>Export few-shot</button>
      </div>

      <div style={styles.pagination}>
        <button
          style={styles.button}
          disabled={!canPrev || loading}
          onClick={() => setPage((p) => Math.max(0, p - 1))}
        >
          ◀ Prev
        </button>
        <span style={styles.pageInfo}>
          Page {page + 1} / {totalPages}
        </span>
        <button
          style={styles.button}
          disabled={!canNext || loading}
          onClick={() => setPage((p) => p + 1)}
        >
          Next ▶
        </button>
      </div>

      {message && <div style={styles.message}>{message}</div>}

      <div style={styles.counters}>
        <div>Rows: {data?.total ?? 0}</div>
        <div>Invalid dropped on load: {data?.invalid ?? 0}</div>
      </div>

      <div style={styles.tableWrap}>
        <table style={styles.table}>
          <thead>
            <tr>
              <th style={styles.th}>Timestamp</th>
              <th style={styles.th}>STT Text</th>
              <th style={styles.th}>Auto Translation</th>
              <th style={styles.th}>Final Translation (editable)</th>
              <th style={styles.th}>Corrected</th>
              <th style={styles.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item, idx) => (
              <tr key={`${item.timestamp}-${idx}`}>
                <td style={styles.tdSmall}>{item.timestamp}</td>
                <td style={styles.td}>{item.stt_text}</td>
                <td style={styles.tdAlt}>{item.auto_translation}</td>
                <td style={styles.td}>
                  <textarea
                    value={item.final_translation || item.auto_translation || ""}
                    onChange={(e) => handleEdit(item.timestamp, e.target.value)}
                    style={styles.textarea}
                  />
                </td>
                <td style={styles.tdCenter}>{item.corrected ? "Yes" : "No"}</td>
                <td style={styles.tdCenter}>
                  <button onClick={() => updateRow(item)} style={styles.smallButton}>Save</button>
                  <button onClick={() => deleteRow(item.timestamp)} style={styles.smallButton}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const styles: Record<string, CSSProperties> = {
  page: {
    padding: "24px",
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    background: "#f5f6f8",
    minHeight: "100vh",
    color: "#111827",
  },
  title: {
    marginBottom: "16px",
    fontSize: "24px",
    fontWeight: 700,
  },
  filters: {
    display: "flex",
    gap: "12px",
    alignItems: "flex-end",
    flexWrap: "wrap",
    marginBottom: "12px",
  },
  label: {
    display: "flex",
    flexDirection: "column",
    gap: "6px",
    minWidth: "120px",
  },
  input: {
    padding: "8px 10px",
    borderRadius: 6,
    border: "1px solid #d0d5dd",
    background: "white",
    color: "#111827",
  },
  button: {
    padding: "10px 14px",
    borderRadius: 8,
    border: "none",
    background: "#111827",
    color: "white",
    cursor: "pointer",
  },
  smallButton: {
    padding: "6px 10px",
    margin: "2px",
    borderRadius: 6,
    border: "none",
    background: "#2563eb",
    color: "white",
    cursor: "pointer",
  },
  message: {
    padding: "10px 12px",
    background: "#e0f2fe",
    color: "#075985",
    borderRadius: 8,
    marginBottom: "12px",
  },
  pagination: {
    display: "flex",
    alignItems: "center",
    gap: "12px",
    marginBottom: "12px",
  },
  pageInfo: {
    color: "#374151",
    fontWeight: 600,
  },
  counters: {
    display: "flex",
    gap: "16px",
    marginBottom: "8px",
    color: "#374151",
  },
  tableWrap: {
    overflowX: "auto",
    background: "white",
    borderRadius: 12,
    boxShadow: "0 10px 30px rgba(15, 23, 42, 0.06)",
  },
  table: {
    width: "100%",
    borderCollapse: "collapse",
  },
  th: {
    textAlign: "left",
    padding: "12px",
    borderBottom: "1px solid #e5e7eb",
    background: "#f9fafb",
    fontWeight: 600,
    fontSize: "14px",
  },
  td: {
    padding: "12px",
    borderBottom: "1px solid #f1f5f9",
    verticalAlign: "top",
    fontSize: "14px",
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    color: "#111827",
  },
  tdAlt: {
    padding: "12px",
    borderBottom: "1px solid #f1f5f9",
    verticalAlign: "top",
    fontSize: "14px",
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    background: "#f8fafc",
    color: "#111827",
  },
  tdSmall: {
    padding: "12px",
    borderBottom: "1px solid #f1f5f9",
    width: "180px",
    fontSize: "12px",
    color: "#6b7280",
  },
  tdCenter: {
    padding: "12px",
    borderBottom: "1px solid #f1f5f9",
    textAlign: "center",
    whiteSpace: "nowrap",
  },
  textarea: {
    width: "100%",
    minHeight: "90px",
    borderRadius: 8,
    border: "1px solid #d0d5dd",
    padding: "10px",
    fontSize: "14px",
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    background: "#f8fafc",
    color: "#111827",
  },
};
