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
    id: "agreement",
    title: "1. Agreement",
    paragraphs: [
      "These Terms of Service (the \"Terms\") govern your access to and use of Worship Translation, the live church translation service available at worshiptranslation.com (the \"Service\"). By creating an account or using the Service, you agree to these Terms. If you do not agree, do not use the Service.",
    ],
  },
  {
    id: "the-service",
    title: "2. The Service",
    paragraphs: [
      "Worship Translation provides machine-assisted live translation of spoken Korean into English (and other supported languages over time) for church services. The Service uses third-party speech recognition and large-language-model translation providers, and is intended to assist human-led worship — not to replace human interpreters or to produce certified translations.",
      "Translation quality depends on audio quality, speaker style, vocabulary, and provider availability. Output may contain errors. You are responsible for reviewing translations before relying on them for any consequential purpose.",
    ],
  },
  {
    id: "accounts",
    title: "3. Accounts and responsibilities",
    paragraphs: [
      "You must be at least 18 years old, or the age of majority in your jurisdiction, to create an account. You are responsible for keeping your login credentials confidential and for any activity that occurs under your account.",
      "Organization owners are responsible for the actions of users they invite to their organization, including hosts and admins.",
    ],
  },
  {
    id: "acceptable-use",
    title: "4. Acceptable use",
    paragraphs: [
      "You agree not to:",
      {
        list: [
          "Use the Service for any unlawful purpose or in violation of applicable export-control, sanctions, or intellectual-property laws.",
          "Upload content you do not have the right to translate or process (for example, copyrighted sermons or scripts without the rights holder's permission).",
          "Attempt to reverse-engineer, scrape, or circumvent rate limits, billing controls, or authentication.",
          "Use the Service to harass, defame, or harm others, or to produce or distribute content that is illegal where you operate.",
          "Resell or sublicense access to the Service without our written permission.",
        ],
      },
      "We may suspend or terminate accounts that violate these rules.",
    ],
  },
  {
    id: "your-content",
    title: "5. Your content",
    paragraphs: [
      "You retain ownership of the sermon scripts, audio, transcripts, and translations you upload or create using the Service (\"Your Content\"). You grant us a limited, non-exclusive, royalty-free license to host, process, and transmit Your Content solely to provide the Service to you and to your authorized listeners.",
      "We do not use Your Content to train machine-learning models. We do not sell Your Content.",
      "You represent that you have all rights necessary to provide Your Content to us under this license.",
    ],
  },
  {
    id: "third-party",
    title: "6. Third-party services",
    paragraphs: [
      "The Service relies on third-party providers, including Google Cloud, Firebase, Deepgram, OpenAI, Stripe, Resend, and Vercel. Your use of features that interact with those providers may be subject to their separate terms. We are not responsible for outages, errors, or policy changes by these providers, but we will make reasonable efforts to maintain availability and notify you of material disruptions.",
      "If you connect a Google account to import documents, our use of information obtained through Google APIs is described in our Privacy Policy and complies with Google's Limited Use requirements.",
    ],
  },
  {
    id: "billing",
    title: "7. Billing and subscriptions",
    paragraphs: [
      "Paid plans are billed in advance through Stripe at the price and cadence shown at checkout. Plans renew automatically until you cancel. You can cancel at any time from your billing dashboard; cancellation takes effect at the end of the current billing period and the Service remains available until then.",
      "Free-trial usage is subject to monthly minute and service caps. Exceeding a cap may block additional sessions until the next billing period or until you upgrade.",
      "We do not refund partial months unless required by applicable law. If we materially change pricing for an existing plan, we will give you at least 30 days' notice before the change takes effect.",
    ],
  },
  {
    id: "intellectual-property",
    title: "8. Intellectual property",
    paragraphs: [
      "We retain all rights in the Service, including the software, design, branding, and documentation. These Terms do not grant you any rights in our trademarks or trade dress. Feedback you send us about the Service may be used by us without restriction or compensation.",
    ],
  },
  {
    id: "disclaimers",
    title: "9. Disclaimers",
    paragraphs: [
      "THE SERVICE IS PROVIDED \"AS IS\" AND \"AS AVAILABLE\" WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT. WE DO NOT WARRANT THAT TRANSLATIONS WILL BE ACCURATE, THAT THE SERVICE WILL BE UNINTERRUPTED, OR THAT IT WILL BE FREE OF ERRORS OR HARMFUL COMPONENTS.",
      "WE DO NOT PROVIDE LEGAL, MEDICAL, FINANCIAL, OR THEOLOGICAL ADVICE. TRANSLATIONS PRODUCED BY THE SERVICE ARE NOT CERTIFIED INTERPRETATIONS AND SHOULD NOT BE RELIED ON IN CONTEXTS THAT REQUIRE A QUALIFIED HUMAN INTERPRETER.",
    ],
  },
  {
    id: "liability",
    title: "10. Limitation of liability",
    paragraphs: [
      "TO THE MAXIMUM EXTENT PERMITTED BY LAW, OUR TOTAL LIABILITY ARISING OUT OF OR RELATED TO THESE TERMS OR YOUR USE OF THE SERVICE WILL NOT EXCEED THE AMOUNTS YOU PAID US FOR THE SERVICE IN THE 12 MONTHS BEFORE THE EVENT GIVING RISE TO THE CLAIM, OR ONE HUNDRED U.S. DOLLARS (US$100), WHICHEVER IS GREATER.",
      "WE WILL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, OR FOR LOST PROFITS OR REVENUES, EVEN IF WE HAVE BEEN ADVISED OF THE POSSIBILITY. SOME JURISDICTIONS DO NOT ALLOW THESE EXCLUSIONS; IN THOSE JURISDICTIONS OUR LIABILITY IS LIMITED TO THE EXTENT PERMITTED BY LAW.",
    ],
  },
  {
    id: "indemnification",
    title: "11. Indemnification",
    paragraphs: [
      "You agree to defend, indemnify, and hold us harmless from any claim, loss, or expense (including reasonable legal fees) arising from your use of the Service, Your Content, or your breach of these Terms.",
    ],
  },
  {
    id: "termination",
    title: "12. Termination",
    paragraphs: [
      "You may stop using the Service or delete your account at any time. We may suspend or terminate your access if you breach these Terms, fail to pay, or if continued provision of the Service would expose us to material legal risk. On termination, Sections 5, 8, 9, 10, 11, and 13 survive.",
    ],
  },
  {
    id: "law",
    title: "13. Governing law and disputes",
    paragraphs: [
      "These Terms are governed by the laws of the State of Texas, United States, without regard to its conflict-of-laws rules. Any dispute will be resolved in the state or federal courts located in Dallas County, Texas, and you consent to the personal jurisdiction of those courts.",
      "If you are using the Service from outside the United States, mandatory consumer-protection laws in your jurisdiction may still apply to you.",
    ],
  },
  {
    id: "changes",
    title: "14. Changes to these Terms",
    paragraphs: [
      "We may update these Terms as the Service evolves. When we make material changes we will update the date below and, when appropriate, notify account holders by email. Continued use of the Service after a change takes effect constitutes acceptance of the revised Terms.",
    ],
  },
  {
    id: "contact",
    title: "15. Contact",
    paragraphs: [
      "Questions about these Terms? Email support@worshiptranslation.com or use the contact form.",
    ],
  },
];

export default function TermsPage() {
  return (
    <>
      <Head>
        <title>Terms of Service — Worship Translation</title>
        <meta
          name="description"
          content="The terms that govern your use of Worship Translation, the live church translation service."
        />
        <link rel="canonical" href="https://worshiptranslation.com/terms" />
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
            <Link href="/privacy" style={{ color: COLORS.muted }}>
              Privacy Policy
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
              Terms of Service
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
