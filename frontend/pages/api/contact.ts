import type { NextApiRequest, NextApiResponse } from "next";

import { API_URL } from "../../utils/urls";

type ContactTopic = "billing" | "setup" | "translation" | "bug" | "sales" | "account" | "other";

type ContactPayload = {
  name: string;
  email: string;
  organization: string;
  topic: ContactTopic;
  message: string;
  website: string;
  turnstileToken: string;
  idToken: string;
};

type ContactResponse =
  | { ok: true }
  | { ok: false; error: string };

type AuthenticatedSender = {
  authenticated: boolean;
  uid?: string;
  email?: string | null;
  displayName?: string | null;
};

type RateStore = {
  ipHourly: Map<string, number[]>;
  emailDaily: Map<string, number[]>;
};

const TOPIC_LABELS: Record<ContactTopic, string> = {
  billing: "Billing",
  setup: "Church Setup",
  translation: "Translation Quality",
  bug: "Bug Report",
  sales: "Sales / Demo",
  account: "Account Access",
  other: "Other",
};

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/i;

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function getRateStore(): RateStore {
  const scope = globalThis as typeof globalThis & { __contactRateStore?: RateStore };
  if (!scope.__contactRateStore) {
    scope.__contactRateStore = {
      ipHourly: new Map(),
      emailDaily: new Map(),
    };
  }
  return scope.__contactRateStore;
}

function normalizeInlineText(value: unknown, maxLength: number): string {
  if (typeof value !== "string") return "";
  return value.replace(/\s+/g, " ").trim().slice(0, maxLength);
}

function normalizeMultilineText(value: unknown, maxLength: number): string {
  if (typeof value !== "string") return "";
  return value
    .replace(/\r\n/g, "\n")
    .replace(/\u0000/g, "")
    .trim()
    .slice(0, maxLength);
}

function parsePayload(req: NextApiRequest): ContactPayload {
  const raw = typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body || {};
  return {
    name: normalizeInlineText(raw?.name, 120),
    email: normalizeInlineText(raw?.email, 160).toLowerCase(),
    organization: normalizeInlineText(raw?.organization, 160),
    topic: normalizeInlineText(raw?.topic, 32) as ContactTopic,
    message: normalizeMultilineText(raw?.message, 4000),
    website: normalizeInlineText(raw?.website, 200),
    turnstileToken: normalizeInlineText(raw?.turnstileToken, 4096),
    idToken: normalizeInlineText(raw?.idToken, 4096),
  };
}

function getClientIp(req: NextApiRequest): string {
  const forwarded = req.headers["x-forwarded-for"];
  if (typeof forwarded === "string" && forwarded.trim()) {
    return forwarded.split(",")[0]?.trim() || "unknown";
  }
  const realIp = req.headers["x-real-ip"];
  if (typeof realIp === "string" && realIp.trim()) return realIp.trim();
  return req.socket.remoteAddress || "unknown";
}

function countLinks(text: string): number {
  return (text.match(/(?:https?:\/\/|www\.)/gi) || []).length;
}

function checkRateLimit(store: Map<string, number[]>, key: string, now: number, windowMs: number, limit: number): boolean {
  const existing = (store.get(key) || []).filter((entry) => now - entry < windowMs);
  if (existing.length >= limit) {
    store.set(key, existing);
    return false;
  }
  existing.push(now);
  store.set(key, existing);
  return true;
}

async function verifyAuthenticatedSender(idToken: string): Promise<AuthenticatedSender> {
  if (!idToken) return { authenticated: false };
  try {
    const res = await fetch(`${API_URL.replace(/\/+$/, "")}/api/auth/me`, {
      headers: {
        Authorization: `Bearer ${idToken}`,
      },
    });
    if (!res.ok) return { authenticated: false };
    const data = (await res.json()) as { user?: { uid?: string; email?: string | null; displayName?: string | null } };
    return {
      authenticated: Boolean(data?.user?.uid),
      uid: data?.user?.uid,
      email: data?.user?.email ?? null,
      displayName: data?.user?.displayName ?? null,
    };
  } catch {
    return { authenticated: false };
  }
}

async function verifyTurnstile(turnstileToken: string, ip: string): Promise<boolean> {
  const secret = (process.env.TURNSTILE_SECRET_KEY || "").trim();
  if (!secret) return true;
  if (!turnstileToken) return false;

  try {
    const body = new URLSearchParams();
    body.set("secret", secret);
    body.set("response", turnstileToken);
    if (ip && ip !== "unknown") body.set("remoteip", ip);

    const res = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body,
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { success?: boolean };
    return Boolean(data.success);
  } catch {
    return false;
  }
}

async function sendContactEmail(payload: {
  name: string;
  email: string;
  organization: string;
  topic: ContactTopic;
  message: string;
  ip: string;
  userAgent: string;
  authenticated: boolean;
  sender?: AuthenticatedSender;
}) {
  const resendApiKey = (process.env.RESEND_API_KEY || "").trim();
  const toEmail = (process.env.CONTACT_TO_EMAIL || process.env.SUPPORT_TO_EMAIL || "").trim();
  const fromEmail = (process.env.CONTACT_FROM_EMAIL || "").trim();

  if (!resendApiKey || !toEmail || !fromEmail) {
    throw new Error("contact_not_configured");
  }

  const topicLabel = TOPIC_LABELS[payload.topic] || TOPIC_LABELS.other;
  const title = `[Contact] ${topicLabel} from ${payload.name}`;
  const text = [
    title,
    `Email: ${payload.email}`,
    payload.organization ? `Organization: ${payload.organization}` : "",
    `Authenticated sender: ${payload.authenticated ? "yes" : "no"}`,
    payload.sender?.uid ? `User ID: ${payload.sender.uid}` : "",
    payload.ip ? `IP: ${payload.ip}` : "",
    payload.userAgent ? `User Agent: ${payload.userAgent}` : "",
    "",
    payload.message,
  ]
    .filter(Boolean)
    .join("\n");

  const html = `
    <div style="font-family: Arial, sans-serif; line-height: 1.6; color: #0f172a;">
      <h2 style="margin-bottom: 12px;">${escapeHtml(title)}</h2>
      <p><strong>Name:</strong> ${escapeHtml(payload.name)}</p>
      <p><strong>Email:</strong> ${escapeHtml(payload.email)}</p>
      ${payload.organization ? `<p><strong>Organization:</strong> ${escapeHtml(payload.organization)}</p>` : ""}
      <p><strong>Authenticated sender:</strong> ${payload.authenticated ? "yes" : "no"}</p>
      ${payload.sender?.uid ? `<p><strong>User ID:</strong> ${escapeHtml(payload.sender.uid)}</p>` : ""}
      ${payload.ip ? `<p><strong>IP:</strong> ${escapeHtml(payload.ip)}</p>` : ""}
      ${payload.userAgent ? `<p><strong>User Agent:</strong> ${escapeHtml(payload.userAgent)}</p>` : ""}
      <hr style="margin: 18px 0; border: none; border-top: 1px solid #cbd5e1;" />
      <p><strong>Message</strong></p>
      <pre style="white-space: pre-wrap; font-family: Arial, sans-serif; margin: 0;">${escapeHtml(payload.message)}</pre>
    </div>
  `.trim();

  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${resendApiKey}`,
    },
    body: JSON.stringify({
      from: fromEmail,
      to: [toEmail],
      reply_to: payload.email,
      subject: title,
      text,
      html,
      tags: [
        { name: "source", value: "worship-contact-form" },
        { name: "topic", value: payload.topic },
      ],
    }),
  });

  if (!res.ok) {
    throw new Error(`resend_failed_${res.status}`);
  }
}

export default async function handler(req: NextApiRequest, res: NextApiResponse<ContactResponse>) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ ok: false, error: "Method not allowed." });
  }

  try {
    const payload = parsePayload(req);

    if (payload.website) {
      return res.status(200).json({ ok: true });
    }

    if (!payload.name || payload.name.length < 2) {
      return res.status(400).json({ ok: false, error: "Please enter your name." });
    }
    if (!EMAIL_PATTERN.test(payload.email)) {
      return res.status(400).json({ ok: false, error: "Please enter a valid email address." });
    }
    if (!(payload.topic in TOPIC_LABELS)) {
      return res.status(400).json({ ok: false, error: "Please choose a valid topic." });
    }
    if (!payload.message || payload.message.length < 20) {
      return res.status(400).json({ ok: false, error: "Please enter a more detailed message." });
    }
    if (countLinks(payload.message) > 3) {
      return res.status(400).json({ ok: false, error: "Please remove extra links from the message." });
    }

    const ip = getClientIp(req);
    const now = Date.now();
    const sender = await verifyAuthenticatedSender(payload.idToken);
    const rateStore = getRateStore();
    const ipAllowed = checkRateLimit(rateStore.ipHourly, ip, now, 60 * 60 * 1000, sender.authenticated ? 6 : 3);
    const emailAllowed = checkRateLimit(rateStore.emailDaily, payload.email, now, 24 * 60 * 60 * 1000, sender.authenticated ? 10 : 5);

    if (!ipAllowed || !emailAllowed) {
      return res.status(429).json({ ok: false, error: "Too many contact requests. Please try again later." });
    }

    if (!sender.authenticated) {
      const turnstilePassed = await verifyTurnstile(payload.turnstileToken, ip);
      if (!turnstilePassed) {
        return res.status(400).json({ ok: false, error: "Spam protection check failed. Please try again." });
      }
    }

    await sendContactEmail({
      name: payload.name,
      email: payload.email,
      organization: payload.organization,
      topic: payload.topic,
      message: payload.message,
      ip,
      userAgent: String(req.headers["user-agent"] || ""),
      authenticated: sender.authenticated,
      sender,
    });

    return res.status(200).json({ ok: true });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err || "");
    if (message === "contact_not_configured") {
      return res.status(503).json({ ok: false, error: "Support inbox is not configured yet." });
    }
    return res.status(500).json({ ok: false, error: "Failed to submit contact request." });
  }
}
