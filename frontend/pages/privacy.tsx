import Head from "next/head";
import Link from "next/link";

const LAST_UPDATED = "2026-06-16";

const COLORS = {
  pageBg: "#ede5d8",
  cardBg: "rgba(255,255,255,0.64)",
  cardBorder: "rgba(120,98,78,0.14)",
  ink: "#22344c",
  body: "#3a3631",
  muted: "#5f6f86",
  accent: "#855763",
} as const;

type Section = {
  id: string;
  title: string;
  paragraphs: Array<string | { list: string[] }>;
};

const SECTIONS: Section[] = [
  {
    id: "summary",
    title: "Summary",
    paragraphs: [
      "Worship Translation provides live, machine-assisted translation for church services. This page describes what information we collect when you sign up, host a service, or listen to one, why we collect it, and the third parties that help us deliver the service.",
      "We collect only what we need to run the service, do not sell your information, and let you delete your data on request.",
    ],
  },
  {
    id: "who-we-are",
    title: "Who we are",
    paragraphs: [
      'Worship Translation ("we", "us", "the service") is a software-as-a-service product operated at worshiptranslation.com. If you need to reach us about this policy, email support@worshiptranslation.com or use the contact form.',
    ],
  },
  {
    id: "information-we-collect",
    title: "Information we collect",
    paragraphs: [
      "We collect three categories of information:",
      {
        list: [
          "Account information you provide: name, email address, the name of your church or organization, and the password or third-party sign-in identifier (Google) you use to log in.",
          "Service content: audio your host streams to us during a live service, the Korean transcript produced by our speech-to-text vendor, the English translation produced by our translation vendor, the sermon scripts and reviewed translations you upload, and any Google Docs you choose to import.",
          "Operational data: billing and subscription metadata from our payment processor (we do not see your full card number), counts of translated minutes and tokens used, basic device and browser information from server logs, and timestamps of actions taken in the app.",
        ],
      },
    ],
  },
  {
    id: "how-we-use",
    title: "How we use your information",
    paragraphs: [
      "We use the information above to:",
      {
        list: [
          "Authenticate you, run the live translation pipeline, deliver translated text to listeners, and improve translation accuracy for your services.",
          "Bill you for paid plans and prevent abuse of free-tier usage.",
          "Respond to support requests and notify you of important account, billing, or security events.",
          "Diagnose technical problems and improve product reliability.",
        ],
      },
      "We do not use your sermon content or audio to train machine-learning models.",
    ],
  },
  {
    id: "third-parties",
    title: "Third parties we share data with",
    paragraphs: [
      "We rely on the following sub-processors to operate the service. Each receives only the information needed for its specific function:",
      {
        list: [
          "Google Firebase Authentication — email/password and Google sign-in, identity verification.",
          "Google Cloud Firestore (us-central1) — primary database for accounts, organizations, sermons, and session state.",
          "Google Cloud Run — hosting for the application backend.",
          "Deepgram — real-time Korean speech-to-text transcription of host audio.",
          "OpenAI — machine translation of Korean transcripts into English.",
          "Google Cloud Text-to-Speech — optional spoken playback of translations.",
          "Google Docs API — reads sermon documents you explicitly choose via the Google Picker.",
          "Stripe — payment processing for subscription billing. We do not store card numbers.",
          "Resend — transactional email delivery (account verification, password reset, billing notifications).",
          "Vercel — hosting for the application frontend.",
        ],
      },
    ],
  },
  {
    id: "google-user-data",
    title: "Google user data",
    paragraphs: [
      "When you connect a Google account to import a Google Doc, we request the documents.readonly and drive.readonly OAuth scopes. We use these scopes only for the function you initiated: showing the Google Drive Picker so you can select a document, then fetching that document's contents one time so we can translate it.",
      "We do not store your Google access token on our servers. The token is held in your browser session only and is discarded when you sign out. We do not use information obtained through Google APIs for any purpose other than fulfilling the import you started, and we do not transfer that information to third parties except as required for the service to function (translation and storage of the resulting sermon, as described above).",
    ],
  },
  {
    id: "retention",
    title: "Data retention",
    paragraphs: [
      "Account and organization records are retained while your account is active. If you delete your account, we delete the associated personal data within 30 days, except where we are required by law (e.g., for tax or billing records) to retain it longer.",
      "Live translation transcripts and stored sermons are retained until you delete them. You can delete a sermon, a service, or your entire organization from the admin dashboard.",
      "Operational logs are retained for up to 90 days for security and debugging purposes, then automatically purged.",
    ],
  },
  {
    id: "your-rights",
    title: "Your rights",
    paragraphs: [
      "Depending on where you live, you may have the right to access, correct, export, or delete the personal data we hold about you, and to object to certain processing. To exercise any of these rights, email support@worshiptranslation.com. We will respond within 30 days.",
      "If you connected a Google account, you can revoke our access at any time at https://myaccount.google.com/permissions.",
    ],
  },
  {
    id: "children",
    title: "Children's privacy",
    paragraphs: [
      "The service is not directed at children under 13 and we do not knowingly collect personal data from them. If you believe a child has provided personal information to us, contact support@worshiptranslation.com and we will delete it.",
    ],
  },
  {
    id: "security",
    title: "Security",
    paragraphs: [
      "We use HTTPS for all traffic, scope database access to the application backend only, and follow least-privilege practices for our cloud services. No internet service can be guaranteed perfectly secure, but if we become aware of a security incident affecting your data we will notify you without undue delay.",
    ],
  },
  {
    id: "changes",
    title: "Changes to this policy",
    paragraphs: [
      "We may update this policy as the service evolves. When we make material changes we will update the date below and, when appropriate, notify account holders by email.",
    ],
  },
  {
    id: "contact",
    title: "Contact",
    paragraphs: [
      "Questions about this policy? Email support@worshiptranslation.com or use the contact form.",
    ],
  },
];

export default function PrivacyPage() {
  return (
    <>
      <Head>
        <title>Privacy Policy — Worship Translation</title>
        <meta
          name="description"
          content="How Worship Translation collects, uses, and shares information when you use our live church translation service."
        />
        <link rel="canonical" href="https://worshiptranslation.com/privacy" />
      </Head>
      <main
        style={{
          minHeight: "100vh",
          background: COLORS.pageBg,
          color: COLORS.body,
          fontFamily:
            "'Source Sans 3', system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
          padding: "48px 24px 96px",
        }}
      >
        <div style={{ maxWidth: 760, margin: "0 auto" }}>
          <nav
            style={{
              display: "flex",
              gap: 16,
              fontSize: "0.85rem",
              marginBottom: 32,
              color: COLORS.muted,
            }}
          >
            <Link href="/" style={{ color: COLORS.accent }}>
              ← Home
            </Link>
            <span>•</span>
            <Link href="/terms" style={{ color: COLORS.muted }}>
              Terms of Service
            </Link>
            <span>•</span>
            <Link href="/contact" style={{ color: COLORS.muted }}>
              Contact
            </Link>
          </nav>

          <header style={{ marginBottom: 40 }}>
            <h1
              style={{
                fontFamily: "'Cormorant Garamond', Georgia, serif",
                fontSize: "2.6rem",
                color: COLORS.ink,
                margin: "0 0 12px 0",
                lineHeight: 1.15,
              }}
            >
              Privacy Policy
            </h1>
            <p style={{ margin: 0, color: COLORS.muted, fontSize: "0.95rem" }}>
              Last updated: {LAST_UPDATED}
            </p>
          </header>

          <article
            style={{
              background: COLORS.cardBg,
              border: `1px solid ${COLORS.cardBorder}`,
              borderRadius: 16,
              padding: "32px 28px",
              lineHeight: 1.7,
              fontSize: "1rem",
            }}
          >
            {SECTIONS.map((section) => (
              <section key={section.id} id={section.id} style={{ marginBottom: 32 }}>
                <h2
                  style={{
                    fontFamily: "'Cormorant Garamond', Georgia, serif",
                    fontSize: "1.5rem",
                    color: COLORS.ink,
                    margin: "0 0 12px 0",
                  }}
                >
                  {section.title}
                </h2>
                {section.paragraphs.map((p, idx) =>
                  typeof p === "string" ? (
                    <p key={idx} style={{ margin: "0 0 12px 0" }}>
                      {p}
                    </p>
                  ) : (
                    <ul
                      key={idx}
                      style={{
                        margin: "0 0 12px 0",
                        paddingLeft: 24,
                        display: "grid",
                        gap: 8,
                      }}
                    >
                      {p.list.map((item, i) => (
                        <li key={i}>{item}</li>
                      ))}
                    </ul>
                  )
                )}
              </section>
            ))}
          </article>
        </div>
      </main>
    </>
  );
}
