// Loads the Google Picker SDK on demand and opens a picker filtered to
// Google Docs. The user picks a doc; we resolve with its canonical /edit URL
// (matching what the existing backend ingestion path already accepts).

const GAPI_SCRIPT_URL = "https://apis.google.com/js/api.js";
const GIS_SCRIPT_URL = "https://accounts.google.com/gsi/client";

type PickerDoc = {
  id: string;
  name?: string;
  url?: string;
  mimeType?: string;
};

type PickerResponse = {
  action: "picked" | "cancel" | "loaded" | string;
  docs?: PickerDoc[];
};

type Gapi = {
  load: (api: string, opts: { callback: () => void; onerror?: () => void }) => void;
};

type PickerBuilder = {
  addView: (view: unknown) => PickerBuilder;
  setOAuthToken: (token: string) => PickerBuilder;
  setDeveloperKey: (key: string) => PickerBuilder;
  setCallback: (cb: (data: PickerResponse) => void) => PickerBuilder;
  setTitle: (title: string) => PickerBuilder;
  build: () => { setVisible: (v: boolean) => void };
};

type GooglePicker = {
  PickerBuilder: new () => PickerBuilder;
  ViewId: { DOCUMENTS: unknown };
  Action: { PICKED: string; CANCEL: string };
};

declare global {
  interface Window {
    gapi?: Gapi;
    google?: { picker?: GooglePicker };
  }
}

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (typeof document === "undefined") {
      reject(new Error("not_in_browser"));
      return;
    }
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing) {
      // Script tag exists; assume it has loaded (gapi/google globals are
      // checked separately by the caller before use).
      resolve();
      return;
    }
    const tag = document.createElement("script");
    tag.src = src;
    tag.async = true;
    tag.defer = true;
    tag.onload = () => resolve();
    tag.onerror = () => reject(new Error(`failed_to_load: ${src}`));
    document.head.appendChild(tag);
  });
}

let pickerLoaded: Promise<void> | null = null;

async function ensurePickerLoaded(): Promise<void> {
  if (pickerLoaded) return pickerLoaded;
  pickerLoaded = (async () => {
    await Promise.all([loadScript(GAPI_SCRIPT_URL), loadScript(GIS_SCRIPT_URL)]);
    const gapi = window.gapi;
    if (!gapi) throw new Error("gapi_unavailable");
    await new Promise<void>((resolve, reject) => {
      gapi.load("picker", {
        callback: () => resolve(),
        onerror: () => reject(new Error("picker_load_failed")),
      });
    });
    if (!window.google?.picker) throw new Error("picker_globals_missing");
  })();
  return pickerLoaded;
}

export type PickResult = {
  id: string;
  name: string;
  url: string;
};

export type OpenPickerOptions = {
  accessToken: string;
  apiKey: string;
  title?: string;
};

/** Open Google Picker filtered to Docs. Resolves with the chosen doc, or
 *  null when the user cancels. Rejects on configuration/loading errors. */
export async function openGoogleDocsPicker(
  options: OpenPickerOptions
): Promise<PickResult | null> {
  if (!options.accessToken) throw new Error("missing_access_token");
  if (!options.apiKey) throw new Error("missing_api_key");

  await ensurePickerLoaded();
  const pickerNs = window.google?.picker;
  if (!pickerNs) throw new Error("picker_globals_missing");

  return new Promise<PickResult | null>((resolve, reject) => {
    try {
      const picker = new pickerNs.PickerBuilder()
        .addView(pickerNs.ViewId.DOCUMENTS)
        .setOAuthToken(options.accessToken)
        .setDeveloperKey(options.apiKey)
        .setTitle(options.title ?? "Select a Google Doc")
        .setCallback((data) => {
          if (data.action === pickerNs.Action.PICKED) {
            const doc = data.docs?.[0];
            if (!doc) {
              resolve(null);
              return;
            }
            const url = doc.url ?? `https://docs.google.com/document/d/${doc.id}/edit`;
            resolve({ id: doc.id, name: doc.name ?? doc.id, url });
          } else if (data.action === pickerNs.Action.CANCEL) {
            resolve(null);
          }
        })
        .build();
      picker.setVisible(true);
    } catch (err) {
      reject(err instanceof Error ? err : new Error(String(err)));
    }
  });
}
