import type { NextConfig } from "next";

const firebaseProjectId = (process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID || "").trim();
const firebaseAuthHelperOrigin = firebaseProjectId ? `https://${firebaseProjectId}.firebaseapp.com` : "";

const nextConfig: NextConfig = {
  // Keep dev and production artifacts separate so a local dev server is not
  // broken by running `next build` in the same workspace.
  distDir: process.env.NODE_ENV === "development" ? ".next-dev" : ".next",
  reactStrictMode: true,
  devIndicators: false,
  async rewrites() {
    if (!firebaseAuthHelperOrigin) return [];
    return [
      {
        source: "/__/auth/:path*",
        destination: `${firebaseAuthHelperOrigin}/__/auth/:path*`,
      },
    ];
  },
};

export default nextConfig;
