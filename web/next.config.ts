import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  // The browser only talks to this origin; Next forwards shop calls to FastAPI,
  // so no CORS configuration is needed and the PayPal secret never reaches web/.
  async rewrites() {
    const api = process.env.API_URL ?? "http://127.0.0.1:8000";
    // explicit routes only, so a future backend route under /api/shop is not exposed by accident
    return [
      { source: "/api/shop/products", destination: `${api}/api/shop/products` },
      { source: "/api/shop/orders", destination: `${api}/api/shop/orders` },
      {
        source: "/api/shop/orders/:id/capture",
        destination: `${api}/api/shop/orders/:id/capture`,
      },
    ];
  },
  async headers() {
    // shortcut: no CSP yet; add one once it can be tested against the PayPal popup flow
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
