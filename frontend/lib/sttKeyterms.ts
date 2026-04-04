import { API_URL } from "../utils/urls";

export type SttKeytermsResponse = {
  orgId: string;
  keyterms: string[];
};

export type SttReplacementItem = { find: string; replace: string };
export type SttReplacementsResponse = { orgId: string; replacements: SttReplacementItem[] };

export async function getSttKeyterms(
  orgId: string,
  idToken: string
): Promise<SttKeytermsResponse> {
  const res = await fetch(`${API_URL}/api/org/${encodeURIComponent(orgId)}/stt-keyterms`, {
    headers: { Authorization: `Bearer ${idToken}` },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Failed to fetch STT keyterms (${res.status})`);
  }
  return res.json();
}

export async function setSttKeyterms(
  orgId: string,
  keyterms: string[],
  idToken: string
): Promise<SttKeytermsResponse> {
  const res = await fetch(`${API_URL}/api/org/${encodeURIComponent(orgId)}/stt-keyterms`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${idToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ keyterms }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Failed to save STT keyterms (${res.status})`);
  }
  return res.json();
}

export async function getSttReplacements(
  orgId: string,
  idToken: string
): Promise<SttReplacementsResponse> {
  const res = await fetch(
    `${API_URL}/api/org/${encodeURIComponent(orgId)}/stt-replacements`,
    { headers: { Authorization: `Bearer ${idToken}` } }
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Failed to fetch STT replacements (${res.status})`);
  }
  return res.json();
}

export async function setSttReplacements(
  orgId: string,
  replacements: SttReplacementItem[],
  idToken: string
): Promise<SttReplacementsResponse> {
  const res = await fetch(
    `${API_URL}/api/org/${encodeURIComponent(orgId)}/stt-replacements`,
    {
      method: "PUT",
      headers: {
        Authorization: `Bearer ${idToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ replacements }),
    }
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Failed to save STT replacements (${res.status})`);
  }
  return res.json();
}
