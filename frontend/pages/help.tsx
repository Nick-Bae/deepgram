import Head from "next/head";
import Link from "next/link";

const LAST_UPDATED = "2026-05-09";

const COLORS = {
  pageBg: "#ede5d8",
  cardBg: "rgba(255,255,255,0.64)",
  cardBorder: "rgba(120,98,78,0.14)",
  ink: "#22344c",
  body: "#3a3631",
  muted: "#5f6f86",
  accent: "#855763",
  accentSoft: "rgba(133,87,99,0.10)",
  highlightBg: "rgba(247,197,107,0.16)",
  highlightInk: "#946010",
  highlightBorder: "rgba(247,197,107,0.36)",
} as const;

type FAQItem = {
  question: string;
  answer: string[];
};

type FAQGroup = {
  id: string;
  title: string;
  intro: string;
  items: FAQItem[];
};

const FAQ_GROUPS: FAQGroup[] = [
  {
    id: "getting-started",
    title: "Getting started",
    intro:
      "Basic questions about creating an account, starting a live translation session, and sharing it with your congregation.",
    items: [
      {
        question: "What is Worship Translation?",
        answer: [
          "Worship Translation is a live translation tool designed for worship services, Bible studies, and church gatherings.",
          "The speaker uses a microphone, the app listens to the spoken Korean, translates it into English, and displays the translation for listeners in real time.",
        ],
      },
      {
        question: "Do listeners need to create an account?",
        answer: [
          "No. Listeners do not need an account.",
          "The host starts the broadcast and shares the listener link or QR code. Listeners can open the link on their phone, tablet, or another screen.",
        ],
      },
      {
        question: "How do I start a live translation session?",
        answer: [
          "Sign in to your account, open your church or service dashboard, and start a broadcast.",
          "After the broadcast starts, share the listener URL or QR code with your congregation.",
          "Before the service begins, speak a few test sentences to confirm that the Korean input and English translation are both working.",
        ],
      },
      {
        question: "What should I test before Sunday worship?",
        answer: [
          "Test the microphone, internet connection, listener page, and display screen before the service begins.",
          "Use the same computer, browser, microphone, projector, and network that you plan to use during worship.",
          "A short test on Saturday or at least 20-30 minutes before the service can prevent most Sunday morning issues.",
        ],
      },
    ],
  },
  {
    id: "microphone",
    title: "Microphone and speech input",
    intro:
      "Questions about microphone permission, sound input, and improving speech recognition accuracy.",
    items: [
      {
        question: "Why is the app not picking up my voice?",
        answer: [
          "First, check whether the browser is allowed to use your microphone. In Chrome, you can usually check this from the microphone icon in the address bar.",
          "Second, confirm that the correct microphone is selected. Sometimes the browser uses the laptop microphone instead of the sound board or external microphone.",
          "Third, check your computer's sound input settings and make sure the microphone is not muted.",
          "After changing the microphone setting, refresh the page and start the broadcast again.",
        ],
      },
      {
        question: "Which browser works best?",
        answer: [
          "Chrome is usually the safest choice for the host computer.",
          "For listeners, Chrome works well on Android and desktop. Safari is usually the safest choice on iPhone.",
        ],
      },
      {
        question: "How can I improve translation accuracy?",
        answer: [
          "Use a clear microphone input and avoid placing the microphone too far from the speaker.",
          "Reduce background noise when possible. Music, crowd noise, and echo can lower speech recognition accuracy.",
          "Speak naturally, but avoid speaking too fast when reading important announcements, Scripture, or key sermon points.",
          "For Korean sermons, short and clear sentence units usually produce better English translations.",
        ],
      },
      {
        question:
          'Why does the translation sometimes use a general subject like "we"?',
        answer: [
          "Korean often omits the subject, but English usually requires one.",
          'When the subject is not clear from the sentence, the translation may choose a general subject such as "we."',
          "The app is designed to use recent sentence context to reduce this problem, but very short or disconnected sentences may still need interpretation.",
        ],
      },
    ],
  },
  {
    id: "live-translation",
    title: "Live translation behavior",
    intro:
      "Questions about delay, freezing, reconnecting, and what to do when translation stops during service.",
    items: [
      {
        question: "Why is there a short delay?",
        answer: [
          "A short delay is normal because the app has to listen to the sentence, process the speech, translate it, and send it to listeners.",
          "The goal is not word-by-word translation, but clear sentence-level or clause-level translation that listeners can actually understand.",
        ],
      },
      {
        question: "What should I do if the translation freezes?",
        answer: [
          "Wait a few seconds first. The app may reconnect automatically if there was a brief network interruption.",
          "If the feed does not recover, refresh the host page and restart the broadcast.",
          "If listeners are still connected to the old room, share the new listener link or QR code again.",
        ],
      },
      {
        question: "What happens if my internet connection drops?",
        answer: [
          "A short Wi-Fi drop may temporarily stop the translation feed.",
          "When possible, use a wired internet connection for the host computer, especially during worship.",
          "If you must use Wi-Fi, place the host computer close to the router or access point.",
        ],
      },
      {
        question: "Can I use this for long worship services?",
        answer: [
          "Yes, but you should test the full setup before using it in a real service.",
          "For longer services, make sure the host computer is plugged into power, the screen does not go to sleep, and the internet connection is stable.",
        ],
      },
    ],
  },
  {
    id: "listener",
    title: "Listener page and audio",
    intro:
      "Questions for people who open the listener link on their own devices.",
    items: [
      {
        question: "Why can listeners see the text but cannot hear audio?",
        answer: [
          "Most browsers require the listener to tap the page once before audio can play.",
          "Ask the listener to tap the screen, check the device volume, and make sure the browser tab is not muted.",
          "On iPhone, also check Silent Mode and Bluetooth output. Sometimes the sound is being sent to AirPods or another connected device.",
        ],
      },
      {
        question: "Should the congregation listen to translated audio during worship?",
        answer: [
          "In many worship settings, text display on a large screen may be better than everyone listening to audio at the same time.",
          "Audio can be useful for people using headphones, small group settings, overflow rooms, or situations where the listener cannot clearly see the screen.",
          "For Sunday worship, many churches may prefer displaying the English translation visually while the speaker's voice remains primary in the room.",
        ],
      },
      {
        question: "Why do some listeners see a connecting message?",
        answer: [
          "Confirm that the host broadcast is actually live.",
          "Ask the listener to refresh the page once.",
          "If the listener is using church Wi-Fi, ask them to try mobile data. Some networks block WebSocket connections.",
          "If the listener link was typed manually, resend the link or QR code because room links can be case-sensitive.",
        ],
      },
    ],
  },
  {
    id: "display",
    title: "Display, projector, PowerPoint, and OBS",
    intro:
      "Questions about showing translations on a large screen or combining subtitles with presentation slides.",
    items: [
      {
        question: "How can I show the translation on a big screen?",
        answer: [
          "Open the display page on the computer connected to the projector or TV.",
          "Use full-screen mode in the browser for a clean view.",
          "Test font size, contrast, and screen placement from the back of the room before the service begins.",
        ],
      },
      {
        question: "Can I show PowerPoint and translation subtitles together?",
        answer: [
          "Yes. The simplest method is to use a split-screen layout: PowerPoint on one side and the translation display on the other side.",
          "For a more polished look, use OBS and place the translation page as a browser source over the PowerPoint slides.",
          "This allows the translation to appear like a lower-third subtitle at the bottom of the screen.",
        ],
      },
      {
        question: "Do I need OBS?",
        answer: [
          "No. OBS is not required.",
          "OBS is helpful when you want a professional broadcast-style layout, such as PowerPoint full screen with subtitles overlaid at the bottom.",
          "For simpler church setups, opening the display page directly in a browser may be enough.",
        ],
      },
      {
        question: "What is the best setup for Sunday worship?",
        answer: [
          "For most churches, the best setup is: one host computer for microphone and translation, and one display screen or projector for the congregation.",
          "If the same person controls PowerPoint and translation, test the workflow carefully so switching windows does not interrupt the service.",
          "If possible, have one person monitor translation while another person controls slides.",
        ],
      },
    ],
  },
  {
    id: "account-billing",
    title: "Account, plans, and usage",
    intro:
      "Questions about accounts, trial usage, church setup, and future plan limits.",
    items: [
      {
        question: "Can I try the service before paying?",
        answer: [
          "Yes. The app may be available for testing while it is still being improved.",
          "During the testing period, some churches may receive expanded or unlimited access so they can try it in real worship settings and provide feedback.",
        ],
      },
      {
        question: "Why will there eventually be a charge?",
        answer: [
          "Live translation has ongoing costs, including server hosting, speech-to-text processing, AI translation, and text-to-speech audio.",
          "The goal is to keep the price reasonable while keeping the service stable enough for church use.",
        ],
      },
      {
        question: "What happens if my usage limit is reached?",
        answer: [
          "If your plan has a usage limit, the broadcast may stop when the limit is reached.",
          "Before using the app for a full worship service, check that your account has enough available minutes or the correct access level.",
        ],
      },
    ],
  },
  {
    id: "support",
    title: "Support",
    intro:
      "What to include when you need help, report a bug, or request a quick demo.",
    items: [
      {
        question: "How can I get help?",
        answer: [
          "Use the Contact support link and include your church name, your email address, what happened, and when it happened.",
          "If the issue happened during a live service, include the approximate time and whether the problem was on the host page, listener page, display page, or audio output.",
        ],
      },
      {
        question: "What information should I send when reporting a problem?",
        answer: [
          "Please include the device type, browser, microphone type, and whether you were using Wi-Fi or wired internet.",
          'Also include a short description such as: "listeners could connect but audio did not play," or "Korean text appeared but English did not update."',
          "A screenshot is helpful when there is an error message.",
        ],
      },
      {
        question: "Can I request a quick demo?",
        answer: [
          "Yes. If your church team wants a quick walkthrough, you can request a short Zoom demo.",
          "A group demo can be helpful for pastors, media team members, and volunteers who will use the app during worship.",
        ],
      },
    ],
  },
];

const tocLinkStyle = {
  display: "inline-flex",
  alignItems: "center",
  padding: "8px 14px",
  borderRadius: 999,
  background: "rgba(255,255,255,0.64)",
  border: `1px solid ${COLORS.cardBorder}`,
  color: COLORS.ink,
  fontSize: 13,
  fontWeight: 700,
  textDecoration: "none",
  letterSpacing: "0.02em",
} as const;

const sectionStyle = {
  display: "grid",
  gap: 14,
  scrollMarginTop: 24,
} as const;

const sectionHeadingStyle = {
  margin: 0,
  fontSize: 22,
  fontWeight: 800,
  color: COLORS.ink,
  letterSpacing: "-0.015em",
} as const;

const introStyle = {
  margin: 0,
  fontSize: 15,
  color: COLORS.body,
  lineHeight: 1.7,
} as const;

const contactCtaStyle = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  padding: "11px 18px",
  borderRadius: 999,
  background: COLORS.accent,
  color: "#fff",
  fontWeight: 700,
  fontSize: 14,
  textDecoration: "none",
  boxShadow: "0 10px 22px rgba(133,87,99,0.22)",
  letterSpacing: "0.02em",
} as const;

function FAQCard({ item }: { item: FAQItem }) {
  return (
    <details
      style={{
        background: COLORS.cardBg,
        border: `1px solid ${COLORS.cardBorder}`,
        borderRadius: 16,
        padding: "0 18px",
        boxShadow:
          "0 10px 24px rgba(122,101,79,0.07), inset 0 1px 0 rgba(255,255,255,0.72)",
      }}
    >
      <summary
        style={{
          cursor: "pointer",
          padding: "17px 0",
          fontSize: 16,
          fontWeight: 800,
          color: COLORS.ink,
          lineHeight: 1.45,
          listStylePosition: "outside",
        }}
      >
        {item.question}
      </summary>

      <div
        style={{
          borderTop: `1px solid ${COLORS.cardBorder}`,
          padding: "14px 0 18px",
          display: "grid",
          gap: 10,
        }}
      >
        {item.answer.map((paragraph, index) => (
          <p
            key={index}
            style={{
              margin: 0,
              color: COLORS.body,
              fontSize: 15,
              lineHeight: 1.72,
            }}
          >
            {paragraph}
          </p>
        ))}
      </div>
    </details>
  );
}

function HighlightBox() {
  return (
    <aside
      style={{
        padding: "16px 18px",
        borderRadius: 18,
        background: COLORS.highlightBg,
        border: `1px solid ${COLORS.highlightBorder}`,
        color: COLORS.highlightInk,
        display: "grid",
        gap: 8,
      }}
    >
      <p style={{ margin: 0, fontSize: 15, fontWeight: 800 }}>
        Before Sunday service
      </p>
      <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.65 }}>
        Test the microphone, listener link, display page, and internet
        connection before worship begins. Most live-service issues are easier
        to fix before people enter the sanctuary.
      </p>
    </aside>
  );
}

export default function HelpPage() {
  return (
    <>
      <Head>
        <title>FAQ - Worship Translation</title>
        <meta
          name="description"
          content="Frequently asked questions for Worship Translation: setup, microphone, live translation, listener audio, display, OBS, PowerPoint, billing, and support."
        />
      </Head>

      <main
        style={{
          minHeight: "100vh",
          background: COLORS.pageBg,
          color: COLORS.body,
          fontFamily: "'Manrope', 'Segoe UI', sans-serif",
          padding: "40px 16px 80px",
        }}
      >
        <div style={{ maxWidth: 840, margin: "0 auto", display: "grid", gap: 28 }}>
          <nav
            aria-label="Help page navigation"
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              flexWrap: "wrap",
            }}
          >
            <Link
              href="/"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                textDecoration: "none",
                color: COLORS.ink,
                fontWeight: 800,
                fontSize: 14,
                letterSpacing: "-0.01em",
                padding: "8px 14px",
                borderRadius: 999,
                background: "rgba(255,255,255,0.64)",
                border: `1px solid ${COLORS.cardBorder}`,
              }}
            >
              <span style={{ color: COLORS.accent }}>←</span>
              <span>Worship Translation</span>
            </Link>

            <button
              type="button"
              onClick={() => {
                if (typeof window !== "undefined") {
                  if (window.history.length > 1) window.history.back();
                  else window.location.href = "/";
                }
              }}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "8px 14px",
                borderRadius: 999,
                background: "transparent",
                border: `1px solid ${COLORS.cardBorder}`,
                color: COLORS.muted,
                fontSize: 13,
                fontWeight: 700,
                cursor: "pointer",
                fontFamily: "inherit",
              }}
            >
              Back
            </button>
          </nav>

          <header style={{ display: "grid", gap: 8 }}>
            <span
              style={{
                fontSize: 11,
                fontWeight: 800,
                letterSpacing: "0.22em",
                textTransform: "uppercase",
                color: COLORS.accent,
              }}
            >
              Worship Translation · FAQ
            </span>

            <h1
              style={{
                margin: 0,
                fontSize: 36,
                fontWeight: 800,
                color: COLORS.ink,
                letterSpacing: "-0.025em",
                lineHeight: 1.1,
              }}
            >
              Frequently Asked Questions
            </h1>

            <p style={{ margin: 0, fontSize: 15, color: COLORS.body, lineHeight: 1.7 }}>
              Find answers about setup, microphone input, live translation,
              listener audio, projector display, PowerPoint, OBS, plans, and
              support.
            </p>

            <p style={{ margin: 0, fontSize: 13, color: COLORS.muted }}>
              Last updated: {LAST_UPDATED}
            </p>
          </header>

          <HighlightBox />

          <nav
            aria-label="FAQ categories"
            style={{ display: "flex", flexWrap: "wrap", gap: 8 }}
          >
            {FAQ_GROUPS.map((group) => (
              <a key={group.id} href={`#${group.id}`} style={tocLinkStyle}>
                {group.title}
              </a>
            ))}
          </nav>

          {FAQ_GROUPS.map((group) => (
            <section
              key={group.id}
              id={group.id}
              aria-labelledby={`${group.id}-heading`}
              style={sectionStyle}
            >
              <div style={{ display: "grid", gap: 6 }}>
                <h2 id={`${group.id}-heading`} style={sectionHeadingStyle}>
                  {group.title}
                </h2>
                <p style={introStyle}>{group.intro}</p>
              </div>

              <div style={{ display: "grid", gap: 10 }}>
                {group.items.map((item) => (
                  <FAQCard key={item.question} item={item} />
                ))}
              </div>
            </section>
          ))}

          <footer
            style={{
              marginTop: 10,
              padding: "22px 24px",
              borderRadius: 22,
              background: "rgba(255,255,255,0.52)",
              border: `1px solid ${COLORS.cardBorder}`,
              boxShadow:
                "0 10px 22px rgba(122,101,79,0.08), inset 0 1px 0 rgba(255,255,255,0.78)",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 18,
              flexWrap: "wrap",
            }}
          >
            <div style={{ minWidth: 0 }}>
              <p
                style={{
                  margin: 0,
                  fontSize: 16,
                  fontWeight: 800,
                  color: COLORS.ink,
                }}
              >
                Still need help?
              </p>
              <p
                style={{
                  margin: "4px 0 0",
                  fontSize: 14,
                  color: COLORS.muted,
                  lineHeight: 1.5,
                }}
              >
                Contact support with your church name, device, browser, and a
                short description of the issue.
              </p>
            </div>

            <Link href="/contact" style={contactCtaStyle}>
              Contact support
            </Link>
          </footer>
        </div>
      </main>
    </>
  );
}
