import type { NextConfig } from "next";

// D-022: standalone output is what makes the multi-stage Dockerfile work —
// `next build` traces the minimal set of node_modules a production server
// actually needs into .next/standalone, so the runtime image doesn't carry
// the full node_modules tree. Didn't exist in v2 until now (v1 has the
// identical line in sap_monitoring/frontend/next.config.ts).
const nextConfig: NextConfig = {
  output: "standalone",
};

export default nextConfig;
