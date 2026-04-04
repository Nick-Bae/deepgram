import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  devIndicators: false,
  async rewrites() {
    return [
      {
        source: "/__/auth/:path*",
        destination: "https://sturdy-dogfish-472313-k6.firebaseapp.com/__/auth/:path*",
      },
    ];
  },
};

export default nextConfig;
