import "./globals.css";
import Shell from "./components/Shell";
import { RunProvider } from "./components/RunContext";

export const metadata = { title: "SAP Monitoring Agent" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* D-031: RunProvider wraps Shell so an in-flight run survives
            navigation between tabs (it used to die with the page component). */}
        <RunProvider>
          <Shell>{children}</Shell>
        </RunProvider>
      </body>
    </html>
  );
}
