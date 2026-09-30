"use client";

import { useEffect, useRef } from "react";
import { TRIAGE_LABEL, type Check } from "@/lib/sentinelApi";
import Bars from "./Bars";
import EvidenceTable from "./EvidenceTable";

const SAP_LABEL = { ok: "OK", not_ok: "Not OK", unknown: "Not stated" };

export default function CheckDrawer({ check, onClose }: { check: Check; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      prev?.focus?.();
    };
  }, [onClose]);

  return (
    <>
      <div className="sx-scrim" onClick={onClose} aria-hidden />
      <aside className="sx-drawer" role="dialog" aria-modal="true" aria-labelledby="sx-drawer-title">
        <div className="sx-drawer-head">
          <div>
            <h2 id="sx-drawer-title">{check.tcode}</h2>
            <p>{check.task}</p>
          </div>
          <button ref={closeRef} className="sx-close" onClick={onClose} aria-label="Close details">×</button>
        </div>
        <div className="sx-drawer-body">
          <dl className="sx-compare">
            <div>
              <dt>SAP&apos;s verdict</dt>
              <dd><b>{SAP_LABEL[check.sap_status]}</b>{check.sap_evidence ? `: ${check.sap_evidence}` : ""}</dd>
            </div>
            <div>
              <dt>Sentinel&apos;s assessment</dt>
              <dd><span className={`sx-tag sx-t-${check.triage}`}>{TRIAGE_LABEL[check.triage]}</span>
                <span style={{ display: "block", marginTop: 8 }}>{check.headline}</span></dd>
            </div>
          </dl>

          {check.analysis_note ? <p className="sx-note">{check.analysis_note}</p> : null}

          {check.metrics.length ? (
            <div className="sx-metrics">
              {check.metrics.map((m, i) => (
                <div className="sx-metric" key={i}><span>{m.label}</span><b className={`sx-tone-${m.tone}`}>{m.value}</b></div>
              ))}
            </div>
          ) : null}

          {check.findings.length ? (
            <section className="sx-block"><h3>What the evidence shows</h3>
              <ul>{check.findings.map((f, i) => <li key={i}>{f}</li>)}</ul></section>
          ) : null}

          {check.recommendation ? (
            <section className="sx-block"><h3>Recommended next step</h3><p className="sx-rec">{check.recommendation}</p></section>
          ) : null}

          {check.charts.map((c, i) => <Bars key={i} chart={c} />)}

          {check.tables.map((t, i) => <EvidenceTable key={i} table={t} />)}

          {check.text ? (
            <section className="sx-block"><h3>Report text</h3><pre className="sx-pre" tabIndex={0}>{check.text}</pre></section>
          ) : null}

          {check.description ? (
            <p className="sx-caption">SAP check: {check.description}{check.period ? ` (${check.period})` : ""}.
              {check.sheet ? ` Evidence tab: ${check.sheet}.` : ""}</p>
          ) : null}
        </div>
      </aside>
    </>
  );
}
