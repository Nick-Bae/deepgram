import { useState, useEffect, useCallback } from "react";
import { getSttKeyterms, setSttKeyterms, getSttReplacements, setSttReplacements, SttReplacementItem } from "../lib/sttKeyterms";

const DEFAULT_TERM_COUNT = 96; // 30 worship + 66 Bible books
const KEYTERM_LIMIT = 100;
const ORG_CUSTOM_MAX = 50;
const REPLACEMENTS_MAX = 50;
const DEFAULT_REPLACEMENTS = [
  { find: "할렐루아", replace: "할렐루야" },
  { find: "아-멘", replace: "아멘" },
  { find: "예수 님", replace: "예수님" },
  { find: "하나 님", replace: "하나님" },
  { find: "성령 님", replace: "성령님" },
  { find: "목사 님", replace: "목사님" },
  { find: "전도 사", replace: "전도사" },
  { find: "장로 님", replace: "장로님" },
];

type Props = {
  orgId: string;
  idToken: string;
};

export default function SttKeytermsEditor({ orgId, idToken }: Props) {
  const [keyterms, setKeyterms] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const [replacements, setReplacements] = useState<SttReplacementItem[]>([]);
  const [replacementsSaved, setReplacementsSaved] = useState(false);
  const [replacementsSaving, setReplacementsSaving] = useState(false);
  const [replacementsError, setReplacementsError] = useState<string | null>(null);

  const sermonSlots = Math.max(0, KEYTERM_LIMIT - keyterms.length - DEFAULT_TERM_COUNT);
  const totalActive = Math.min(
    keyterms.length + DEFAULT_TERM_COUNT,
    KEYTERM_LIMIT
  );

  useEffect(() => {
    setLoading(true);
    Promise.all([
      getSttKeyterms(orgId, idToken),
      getSttReplacements(orgId, idToken),
    ])
      .then(([kt, rp]) => {
        setKeyterms(kt.keyterms);
        setReplacements(rp.replacements);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [orgId, idToken]);

  const handleAdd = useCallback(() => {
    const term = input.trim();
    if (!term) return;
    if (keyterms.includes(term)) {
      setInput("");
      return;
    }
    if (keyterms.length >= ORG_CUSTOM_MAX) {
      setError(`Maximum ${ORG_CUSTOM_MAX} custom terms allowed.`);
      return;
    }
    setKeyterms((prev) => [...prev, term]);
    setInput("");
    setSaved(false);
  }, [input, keyterms]);

  const handleRemove = useCallback((term: string) => {
    setKeyterms((prev) => prev.filter((t) => t !== term));
    setSaved(false);
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await setSttKeyterms(orgId, keyterms, idToken);
      setKeyterms(res.keyterms);
      setSaved(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [orgId, keyterms, idToken]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleAdd();
      }
    },
    [handleAdd]
  );

  const handleAddReplacement = useCallback(() => {
    if (replacements.length >= REPLACEMENTS_MAX) return;
    setReplacements((prev) => [...prev, { find: "", replace: "" }]);
    setReplacementsSaved(false);
  }, [replacements.length]);

  const handleReplacementChange = useCallback(
    (index: number, field: "find" | "replace", value: string) => {
      setReplacements((prev) =>
        prev.map((r, i) => (i === index ? { ...r, [field]: value } : r))
      );
      setReplacementsSaved(false);
    },
    []
  );

  const handleRemoveReplacement = useCallback((index: number) => {
    setReplacements((prev) => prev.filter((_, i) => i !== index));
    setReplacementsSaved(false);
  }, []);

  const handleSaveReplacements = useCallback(async () => {
    setReplacementsSaving(true);
    setReplacementsError(null);
    try {
      const valid = replacements.filter((r) => r.find.trim());
      const res = await setSttReplacements(orgId, valid, idToken);
      setReplacements(res.replacements);
      setReplacementsSaved(true);
    } catch (e: unknown) {
      setReplacementsError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setReplacementsSaving(false);
    }
  }, [orgId, replacements, idToken]);

  if (loading) {
    return <div className="text-sm text-gray-500">Loading STT keywords...</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-gray-900">STT Keywords</h3>
        <span className="text-xs text-gray-500">
          {totalActive} / {KEYTERM_LIMIT} keyterms active
        </span>
      </div>

      {error && (
        <div className="text-sm text-red-600 bg-red-50 rounded px-3 py-2">
          {error}
        </div>
      )}

      {/* Church Custom Tier */}
      <div>
        <div className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">
          Church Custom ({keyterms.length} / {ORG_CUSTOM_MAX})
        </div>
        <div className="flex flex-wrap gap-2 min-h-[36px] mb-2">
          {keyterms.map((term) => (
            <span
              key={term}
              className="inline-flex items-center gap-1 bg-blue-100 text-blue-800 text-sm px-2 py-0.5 rounded"
            >
              {term}
              <button
                onClick={() => handleRemove(term)}
                className="text-blue-500 hover:text-blue-700 leading-none"
                aria-label={`Remove ${term}`}
              >
                ×
              </button>
            </span>
          ))}
          {keyterms.length === 0 && (
            <span className="text-xs text-gray-400 italic">
              No custom terms yet — add pastor names, series titles, etc.
            </span>
          )}
        </div>
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Add a term (e.g. 담임목사, 오병이어)"
            className="flex-1 border border-gray-300 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            disabled={keyterms.length >= ORG_CUSTOM_MAX}
          />
          <button
            onClick={handleAdd}
            disabled={!input.trim() || keyterms.length >= ORG_CUSTOM_MAX}
            className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Add
          </button>
        </div>
      </div>

      {/* Sermon Script Tier */}
      <div className="bg-gray-50 rounded px-3 py-2 text-xs text-gray-500">
        <span className="font-medium">Sermon Script</span> — Loaded automatically
        from today&apos;s sermon prep ({sermonSlots > 0 ? `up to ${sermonSlots} slots available` : "no slots remaining"})
      </div>

      {/* Default Tier */}
      <details className="text-xs text-gray-500">
        <summary className="cursor-pointer select-none hover:text-gray-700">
          Default — {DEFAULT_TERM_COUNT} built-in terms (worship + all 66 Bible books)
        </summary>
        <p className="mt-1 text-gray-400">
          예수님, 하나님, 성령님, 할렐루야, 아멘 · · · 창세기 through 요한계시록 (always included)
        </p>
      </details>

      <div className="flex items-center gap-3 pt-1">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-4 py-1.5 bg-gray-900 text-white text-sm rounded hover:bg-gray-700 disabled:opacity-50"
        >
          {saving ? "Saving..." : "Save"}
        </button>
        {saved && (
          <span className="text-xs text-green-600">Saved. Takes effect on next session start.</span>
        )}
      </div>

      {/* STT Corrections Section */}
      <div className="border-t pt-4 mt-2 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-gray-900">STT Corrections</h3>
          <span className="text-xs text-gray-500">
            {replacements.length} custom · {DEFAULT_REPLACEMENTS.length} built-in
          </span>
        </div>

        {replacementsError && (
          <div className="text-sm text-red-600 bg-red-50 rounded px-3 py-2">
            {replacementsError}
          </div>
        )}

        {/* Built-in corrections (read-only) */}
        <details className="text-xs text-gray-500">
          <summary className="cursor-pointer select-none hover:text-gray-700">
            Built-in corrections ({DEFAULT_REPLACEMENTS.length} patterns — known nova-3 Korean errors)
          </summary>
          <div className="mt-2 space-y-1">
            {DEFAULT_REPLACEMENTS.map((r) => (
              <div key={r.find} className="flex items-center gap-2 text-gray-400">
                <span className="font-mono">{r.find}</span>
                <span>→</span>
                <span className="font-mono">{r.replace}</span>
              </div>
            ))}
          </div>
        </details>

        {/* Custom corrections table */}
        <div>
          <div className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">
            Custom Corrections ({replacements.length} / {REPLACEMENTS_MAX})
          </div>
          {replacements.length > 0 && (
            <div className="space-y-2 mb-2">
              {replacements.map((r, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    type="text"
                    value={r.find}
                    onChange={(e) => handleReplacementChange(i, "find", e.target.value)}
                    placeholder="Find (e.g. 예수 님)"
                    className="flex-1 border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                  <span className="text-gray-400 text-sm">→</span>
                  <input
                    type="text"
                    value={r.replace}
                    onChange={(e) => handleReplacementChange(i, "replace", e.target.value)}
                    placeholder="Replace (e.g. 예수님)"
                    className="flex-1 border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                  <button
                    onClick={() => handleRemoveReplacement(i)}
                    className="text-gray-400 hover:text-red-500 text-sm leading-none px-1"
                    aria-label="Remove"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
          {replacements.length === 0 && (
            <p className="text-xs text-gray-400 italic mb-2">
              No custom corrections — add church-specific STT fixes below.
            </p>
          )}
          <button
            onClick={handleAddReplacement}
            disabled={replacements.length >= REPLACEMENTS_MAX}
            className="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            + Add correction
          </button>
        </div>

        <p className="text-xs text-gray-400">
          Find terms are case-insensitive. Leave replace empty to delete the term from transcripts.
        </p>

        <div className="flex items-center gap-3">
          <button
            onClick={handleSaveReplacements}
            disabled={replacementsSaving}
            className="px-4 py-1.5 bg-gray-900 text-white text-sm rounded hover:bg-gray-700 disabled:opacity-50"
          >
            {replacementsSaving ? "Saving..." : "Save Corrections"}
          </button>
          {replacementsSaved && (
            <span className="text-xs text-green-600">Saved. Takes effect on next session start.</span>
          )}
        </div>
      </div>
    </div>
  );
}
