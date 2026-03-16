import { useRouter } from "next/router";
import { FormEvent, useEffect, useRef, useState } from "react";

import StudioAccessLayout, {
  buildStudioButtonStyle,
  buildStudioNoticeStyle,
  studioChipStyle,
  studioFieldStyle,
  studioHelperTextStyle,
  studioLabelStyle,
  studioLabelTextStyle,
} from "../../components/StudioAccessLayout";
import { bootstrapOwnerOrg, checkChurchSlugAvailability, fetchAuthMe } from "../../lib/backendAuth";
import { useAuth } from "../../lib/authContext";
import { normalizeChurchSlug } from "../../lib/churchSlug";
import { buildDashboardHref, persistDashboardContext, pickPreferredMembership } from "../../lib/dashboardRoute";
import { clearHostToken, persistAuthToken, persistStreamContext } from "../../utils/streamContext";

export default function CreateChurchOnboardingPage() {
  const router = useRouter();
  const { user, loading, configured, missingEnv, getIdToken } = useAuth();

  const [busy, setBusy] = useState(false);
  const [checkingMembership, setCheckingMembership] = useState(true);
  const [churchName, setChurchName] = useState("");
  const [churchSlug, setChurchSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [slugAvailabilityBusy, setSlugAvailabilityBusy] = useState(false);
  const [slugAvailable, setSlugAvailable] = useState<boolean | null>(null);
  const [slugSuggestions, setSlugSuggestions] = useState<string[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const slugCheckSeqRef = useRef(0);
  const normalizedSlug = normalizeChurchSlug(churchSlug);
  const slugExample = "chicago-ark";

  useEffect(() => {
    if (slugTouched) return;
    setChurchSlug(normalizeChurchSlug(churchName));
  }, [churchName, slugTouched]);

  useEffect(() => {
    if (!normalizedSlug) {
      setSlugAvailabilityBusy(false);
      setSlugAvailable(null);
      setSlugSuggestions([]);
      return;
    }
    setSlugAvailabilityBusy(true);
    const seq = slugCheckSeqRef.current + 1;
    slugCheckSeqRef.current = seq;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const payload = await checkChurchSlugAvailability(normalizedSlug);
          if (slugCheckSeqRef.current !== seq) return;
          setSlugAvailable(Boolean(payload.available));
          const suggestions = (payload.suggestions || [])
            .map((value) => normalizeChurchSlug(value))
            .filter((value, index, rows) => Boolean(value) && value !== normalizedSlug && rows.indexOf(value) === index)
            .slice(0, 3);
          setSlugSuggestions(suggestions);
        } catch {
          if (slugCheckSeqRef.current !== seq) return;
          setSlugAvailable(null);
          setSlugSuggestions([]);
        } finally {
          if (slugCheckSeqRef.current === seq) {
            setSlugAvailabilityBusy(false);
          }
        }
      })();
    }, 260);
    return () => clearTimeout(timer);
  }, [normalizedSlug]);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login?next=%2Fonboarding%2Fcreate-church");
      return;
    }
    if (!configured) {
      setCheckingMembership(false);
      return;
    }
    let cancelled = false;
    const run = async () => {
      try {
        const token = await getIdToken();
        if (!token || cancelled) return;
        persistAuthToken(token);
        const me = await fetchAuthMe(token);
        const primary = pickPreferredMembership(me);
        if (!primary || cancelled) return;
        persistDashboardContext(primary);
        await router.replace(buildDashboardHref(primary));
      } catch (err) {
        if (!cancelled && err instanceof Error) {
          setErrorMsg(err.message);
        }
      } finally {
        if (!cancelled) setCheckingMembership(false);
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [configured, getIdToken, loading, router, user]);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!configured) return;
    const safeSlug = normalizeChurchSlug(churchSlug);
    if (!safeSlug) {
      setErrorMsg("Church slug is required.");
      return;
    }
    if (slugAvailable === false) {
      setErrorMsg("That church URL slug is already in use. Choose one of the suggestions.");
      return;
    }
    setBusy(true);
    setErrorMsg(null);
    try {
      const token = await getIdToken(true);
      if (!token) {
        throw new Error("Please sign in again.");
      }
      persistAuthToken(token);
      const created = await bootstrapOwnerOrg(token, {
        churchName: churchName.trim(),
        churchSlug: safeSlug,
        timezone: "America/Chicago",
        source: "ko",
        target: "en",
      });
      const org = created.org;
      const serviceKey = created.services?.[0]?.serviceKey || "sun-11am";
      clearHostToken();
      persistStreamContext({
        orgId: org.orgId,
        serviceKey,
        churchSlug: org.slug,
      });
      const params = new URLSearchParams();
      params.set("orgId", org.orgId);
      params.set("serviceKey", serviceKey);
      await router.replace(`/host/c/${encodeURIComponent(org.slug)}/broadcast?${params.toString()}`);
    } catch (err) {
      if (err instanceof Error && err.message.toLowerCase().includes("slug")) {
        try {
          const payload = await checkChurchSlugAvailability(safeSlug);
          setSlugAvailable(Boolean(payload.available));
          const suggestions = (payload.suggestions || [])
            .map((value) => normalizeChurchSlug(value))
            .filter((value, index, rows) => Boolean(value) && value !== safeSlug && rows.indexOf(value) === index)
            .slice(0, 3);
          setSlugSuggestions(suggestions);
        } catch {
          // no-op; keep original error message.
        }
      }
      setErrorMsg(err instanceof Error ? err.message : "Failed to create church.");
    } finally {
      setBusy(false);
    }
  };

  const disabled = !configured || busy || checkingMembership || !normalizedSlug || slugAvailabilityBusy || slugAvailable === false;

  return (
    <StudioAccessLayout
      pageTitle="Create Church | Worship"
      pageDescription="Create your church organization before starting services."
      panelEyebrow="Organization Setup"
      panelTitle="Create Your Church"
      panelDescription="Create your organization before starting services."
      infoEyebrow="First-Time Setup"
      infoTitle="Reserve the church URL and open the dashboard in one step."
      infoDescription="This is the first-time organization setup flow for signed-in users who do not belong to a church yet."
      infoItems={[
        {
          title: "Church URL validation",
          description: "The slug check runs before the workspace is created so the public-facing URL is safe to use immediately. Keep it simple because it cannot be changed later.",
        },
        {
          title: "Immediate host routing",
          description: "As soon as the organization is created, the session stores the first service and routes into the broadcast dashboard.",
        },
        {
          title: "No duplicate onboarding",
          description: "If the account already belongs to a church, this page redirects back to the correct dashboard instead of creating another org.",
        },
      ]}
      headerActions={[
        { href: "/", label: "Back Home" },
        { href: "/contact", label: "Contact Us", accent: true },
      ]}
    >
      {!configured ? (
        <div style={buildStudioNoticeStyle("error")}>
          Firebase config is missing in <code>frontend/.env.local</code>: {missingEnv.join(", ")}
        </div>
      ) : null}

      {checkingMembership ? <p style={buildStudioNoticeStyle("info")}>Checking existing memberships...</p> : null}
      {errorMsg ? <p style={buildStudioNoticeStyle("error")}>Error: {errorMsg}</p> : null}

      <form onSubmit={onSubmit} style={{ display: "grid", gap: 12 }}>
        <label style={studioLabelStyle}>
          <span style={studioLabelTextStyle}>Church name</span>
          <input value={churchName} onChange={(e) => setChurchName(e.target.value)} required style={studioFieldStyle} />
        </label>
        <label style={studioLabelStyle}>
          <span style={studioLabelTextStyle}>Church URL slug</span>
          <input
            value={churchSlug}
            onChange={(e) => {
              setSlugTouched(true);
              setChurchSlug(normalizeChurchSlug(e.target.value));
            }}
            required
            pattern="[a-z0-9-]+"
            title="Use lowercase letters, numbers, and hyphens."
            placeholder={slugExample}
            style={studioFieldStyle}
          />
          <div
            style={{
              borderRadius: 16,
              border: "1px solid rgba(224,163,86,0.34)",
              background: "linear-gradient(145deg, rgba(252,247,236,0.94), rgba(244,233,208,0.86))",
              color: "#7a5525",
              padding: "12px 14px",
              display: "grid",
              gap: 6,
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
            }}
          >
            <p style={{ margin: 0, fontSize: 12, fontWeight: 800, letterSpacing: "0.18em", textTransform: "uppercase" }}>
              Important
            </p>
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7 }}>
              Keep the slug simple. Use English lowercase letters, numbers, and hyphens only.
            </p>
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7 }}>
              This becomes part of your public church URL and <strong>cannot be changed later</strong>.
            </p>
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7 }}>
              Example slug: <strong>{slugExample}</strong>
            </p>
            <div
              style={{
                borderRadius: 12,
                border: "1px solid rgba(196,140,70,0.34)",
                background: "rgba(255,255,255,0.56)",
                padding: "10px 12px",
                display: "grid",
                gap: 6,
              }}
            >
              <p style={{ margin: 0, fontSize: 11, fontWeight: 800, letterSpacing: "0.18em", textTransform: "uppercase", color: "#8b6533" }}>
                Preview URL
              </p>
              <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7, wordBreak: "break-all" }}>
                Example: <strong>/c/{slugExample}/s/sun-11am</strong>
              </p>
              {normalizedSlug ? (
                <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7, wordBreak: "break-all", color: "#5d4017" }}>
                  Yours: <strong>/c/{normalizedSlug}/s/sun-11am</strong>
                </p>
              ) : null}
            </div>
          </div>
          {slugAvailabilityBusy ? <span style={studioHelperTextStyle}>Checking slug availability...</span> : null}
          {!slugAvailabilityBusy && slugAvailable === true ? <span style={{ ...studioHelperTextStyle, color: "#2f6d4f" }}>Slug is available.</span> : null}
          {!slugAvailabilityBusy && slugAvailable === false ? <span style={{ ...studioHelperTextStyle, color: "#a33d51" }}>That slug is already taken.</span> : null}
          {!slugAvailabilityBusy && slugAvailable === false && slugSuggestions.length ? (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {slugSuggestions.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => {
                    setSlugTouched(true);
                    setChurchSlug(suggestion);
                    setErrorMsg(null);
                  }}
                  style={{ ...studioChipStyle, cursor: "pointer" }}
                >
                  Use {suggestion}
                </button>
              ))}
            </div>
          ) : null}
        </label>
        <button type="submit" disabled={disabled} style={buildStudioButtonStyle({ disabled })}>
          {busy ? "Creating Church..." : checkingMembership ? "Checking..." : "Create Church"}
        </button>
      </form>
    </StudioAccessLayout>
  );
}
