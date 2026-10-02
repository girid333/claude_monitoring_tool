import "../sentinel.css";
import TopBar from "@/components/sentinel/TopBar";
import PerfDashboard from "@/components/sentinel/PerfDashboard";

export const metadata = { title: "Performance · Sentinel" };

export default function PerformancePage() {
  return (
    <div className="sx">
      <TopBar />
      <PerfDashboard />
    </div>
  );
}
