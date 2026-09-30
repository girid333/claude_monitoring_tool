import "../sentinel.css";
import TopBar from "@/components/sentinel/TopBar";
import HealthDashboard from "@/components/sentinel/HealthDashboard";

export const metadata = { title: "Health check · Sentinel" };

export default function HealthPage() {
  return (
    <div className="sx">
      <TopBar />
      <HealthDashboard />
    </div>
  );
}
