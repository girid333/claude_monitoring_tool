import type { NextConfig } from "next";

// D-022: standalone output is what makes the multi-stage Dockerfile work —
// `next build` traces the minimal set of node_modules a production server
// actually needs into .next/standalone, so the runtime image doesn't carry
// the full node_modules tree. Didn't exist in v2 until now (v1 has the
// identical line in sap_monitoring/frontend/next.config.ts).
const nextConfig: NextConfig = {
  output: "standalone",
  // D-050: record/replay pages moved under /replay. Old bookmarks keep
  // working; "/" itself is now the Sentinel landing page (Landscape is /basis).
  async redirects() {
    return [
      { source: "/recorder", destination: "/replay/recorder", permanent: false },
      { source: "/apiflow", destination: "/replay/apiflow", permanent: false },
      { source: "/sapgui", destination: "/replay/sapgui", permanent: false },
    ];
  },
};

export default nextConfig;
