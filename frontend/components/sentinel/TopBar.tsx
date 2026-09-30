"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/* Routes: the existing Landscape/Core BASIS UI moves to /basis, the WebGUI
   recorder + data runs to /replay. Adjust hrefs here if they live elsewhere. */
export const ROUTES = { home: "/", basis: "/basis", health: "/health", replay: "/replay" };

export default function TopBar() {
  const path = usePathname() || "/";
  // "as const": the app's tsconfig is non-strict, which would widen "page"
  // to string and fail aria-current's type. Systems/Runs/Reports/RFC sit in
  // Monitoring's sidebar (D-050), so they highlight Monitoring too.
  const MONITORING_PAGES = ["/systems", "/runs", "/reports", "/rfc"];
  const cur = (href: string) =>
    (href === "/" ? path === "/"
      : path.startsWith(href)
        || (href === ROUTES.basis && MONITORING_PAGES.some(p => path.startsWith(p))))
      ? ("page" as const) : undefined;
  return (
    <header className="sx-top">
      <div className="sx-wrap">
        <Link href={ROUTES.home} className="sx-brand" aria-label="Sentinel home">
          <span className="sx-mark" aria-hidden>
            <svg width="14" height="14" viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.2" fill="none" stroke="#fff" strokeWidth="2" /><circle cx="7" cy="7" r="1.8" fill="#fff" /></svg>
          </span>
          <span className="sx-brand-name">Sentinel</span>
        </Link>
        <nav className="sx-nav" aria-label="Main">
          <Link href={ROUTES.basis} aria-current={cur(ROUTES.basis)}>Monitoring</Link>
          <Link href={ROUTES.health} aria-current={cur(ROUTES.health)}>Health check</Link>
          <Link href={ROUTES.replay} aria-current={cur(ROUTES.replay)}>Record and replay{" "}<span className="sx-lab">Lab</span></Link>
        </nav>
      </div>
    </header>
  );
}
