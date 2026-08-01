/* D-024: runtime-resolved backend base URL — fixes a real bug hit deploying
   D-022's container. NEXT_PUBLIC_API is inlined into the client bundle at
   BUILD time (Next.js's own constraint on NEXT_PUBLIC_* vars), and
   docker-compose.yml's default build arg is "http://localhost:8000". That
   works when you browse from the same machine the backend runs on, but
   breaks silently the moment you reach the frontend via a LAN IP or
   hostname from a different device: the browser's own "localhost:8000"
   resolves to ITS machine, not the server's — every fetch throws
   (connection refused), and because none of the pages wrapped fetch() in
   try/catch, the failure was invisible (empty system list, "Add system"
   silently doing nothing).

   Fix: only trust the build-time value if someone explicitly overrode it
   away from the Dockerfile's own default; otherwise derive the API origin
   from wherever the browser actually loaded the page (window.location),
   on port 8000. This makes one built image work whether it's reached via
   localhost, a LAN IP, or a real hostname — no rebuild per environment. */

export function apiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API;
  if (configured && configured !== "http://localhost:8000") return configured;
  if (typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
  return configured ?? "http://localhost:8000";
}
