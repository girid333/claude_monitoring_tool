import type { TableOut } from "@/lib/sentinelApi";

export default function EvidenceTable({ table }: { table: TableOut }) {
  if (!table.columns.length) return null;
  return (
    <div>
      {table.title ? <h3 style={{ margin: "0 0 8px", fontSize: 14, color: "var(--text-2)", fontWeight: 600 }}>{table.title}</h3> : null}
      {table.rows.length ? (
        <div className="sx-table-wrap" tabIndex={0} aria-label={table.title || "Evidence table"}>
          <table className="sx-table">
            <thead><tr>{table.columns.map((c, i) => <th key={i} scope="col">{c}</th>)}</tr></thead>
            <tbody>
              {table.rows.map((r, i) => (
                <tr key={i}>{table.columns.map((_, j) => <td key={j} title={r[j] || ""}>{r[j] || ""}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="sx-caption">No entries.</p>}
      {table.total > table.rows.length ? (
        <p className="sx-caption">Showing the first {table.rows.length.toLocaleString()} of {table.total.toLocaleString()} rows. The full list is in the workbook.</p>
      ) : null}
    </div>
  );
}
