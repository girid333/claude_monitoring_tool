"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { perfApi } from "@/lib/sentinelApi";
import type { Smon, SmonSchedule } from "@/lib/sentinelApi";

/* D-052c — schedule /SDF/SMON (or /SDF/MON) daily monitoring from Sentinel.
   A write to SAP: shown as an explicit form that lists exactly what will be
   created, and nothing is sent until the user presses "Schedule in <SID>". */

const hhmm = (s?: string | null) => (s ? s.slice(11, 16) : "–");
const ddmm = (s?: string | null) => (s ? `${s.slice(8, 10)}.${s.slice(5, 7)}.` : "");

export default function SmonScheduler({ sid, smon, onChanged, declined, onDecline }: {
  sid: string; smon?: Smon; onChanged: () => void; declined: boolean; onDecline: () => void;
}) {
  const [st, setSt] = useState<SmonSchedule | null>(null);
  const [form, setForm] = useState(false);
  const [interval, setInterval_] = useState(10);
  const [keep, setKeep] = useState(14);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<string[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const sending = useRef(false);                    // one write at a time, whatever the button state

  const refresh = useCallback(() => {
    perfApi.smonStatus(sid).then(setSt).catch(e => setErr(String((e as Error).message || e)));
  }, [sid]);
  useEffect(() => { setSt(null); setResult(null); setErr(null); setForm(false); refresh(); }, [refresh]);

  const tool = st?.tool ?? smon?.tool ?? "SMON";
  const tx = `/SDF/${tool}`;

  const schedule = async () => {
    if (sending.current) return;
    if (!(interval >= 1 && interval <= 600 && keep >= 1 && keep <= 366)) { setErr("Interval 1–600 s, retention 1–366 days."); return; }
    sending.current = true;
    setBusy(`Scheduling ${tx} daily monitoring in ${sid}…`); setErr(null);
    try {
      const r = await perfApi.scheduleSmon(sid, interval, keep);
      setResult([...r.done, ...r.messages]); setSt(r.status); setForm(false); onChanged();
    } catch (e) { setErr(String((e as Error).message || e)); } finally { setBusy(null); sending.current = false; }
  };
  const stop = async () => {
    if (sending.current) return;
    sending.current = true;
    setBusy("Removing the daily scheduler jobs…"); setErr(null);
    try {
      const r = await perfApi.unscheduleSmon(sid);
      setResult([`Deleted ${r.deleted} daily ${st?.job_name ?? "scheduler"} job(s).`, ...(r.note ? [r.note] : [])]);
      setSt(r.status); setConfirmStop(false); onChanged();
    } catch (e) { setErr(String((e as Error).message || e)); } finally { setBusy(null); sending.current = false; }
  };
  // Enter in a number field must not send the write: only the "Schedule in <SID>" button does
  const noEnter = (e: React.KeyboardEvent) => { if (e.key === "Enter") e.preventDefault(); };

  if (!st) return err ? <p className="px-note">{err}</p> : null;
  if (!st.tool) return <p className="px-empty">{st.why}</p>;

  const scheduled = st.daily_scheduled || st.today;
  return (
    <div className="sm-sched" data-testid="px-smon-sched">
      {scheduled ? (
        <div className="sm-state" data-testid="px-smon-state">
          <span className="sm-dot" aria-hidden />
          <div>
            <b>{tx} daily monitoring {st.daily_scheduled ? "is scheduled" : "runs today only"}</b>
            <small>
              {st.today ? `Today ${hhmm(st.today.start)}–${hhmm(st.today.end)}, every ${st.today.interval_s} s${st.today.running ? " · collecting" : ""}` : "No run today"}
              {st.daily_scheduled ? ` · next start ${ddmm(st.next_start)} ${hhmm(st.next_start)} (job ${st.job_name})` : ""}
              {st.watchdog ? " · watchdog active" : ""}
            </small>
          </div>
          {st.daily_scheduled && st.can_schedule ? (confirmStop ? (
            <span className="sx-actions">
              <button className="sx-btn" onClick={stop} disabled={!!busy} data-testid="px-smon-stop-confirm">Delete the daily jobs</button>
              <button className="sx-btn" onClick={() => setConfirmStop(false)}>Keep</button>
            </span>
          ) : <button className="sx-btn" onClick={() => setConfirmStop(true)} data-testid="px-smon-stop">Stop daily monitoring</button>) : null}
          {!st.daily_scheduled && st.can_schedule ? <button className="sx-btn" onClick={() => setForm(true)}>Schedule every day</button> : null}
        </div>
      ) : smon?.active && !form ? (
        <div className="sm-state"><span className="sm-dot" aria-hidden /><div><b>{tx} is collecting</b>
          <small>An analysis started outside daily monitoring{smon.last ? ` · last snapshot ${smon.last.slice(11, 16)}` : ""}</small></div>
          {st.can_schedule ? <button className="sx-btn" onClick={() => setForm(true)}>Schedule every day</button> : null}</div>
      ) : !form && !declined ? (
        <div className="px-offer" data-testid="px-smon-offer">
          <p><b>{tx} is not running in {sid}.</b> {smon?.why ? `${smon.why}. ` : ""}Snapshot monitoring adds a picture of work processes,
            queues, users, CPU and the network round trip to the database for every application server, every few seconds.
            Without it, this page uses SAP&apos;s other read-only sources.</p>
          <div className="sx-actions">
            {st.can_schedule ? <button className="sx-btn sx-btn-primary" onClick={() => setForm(true)} data-testid="px-smon-schedule">Schedule {tx}…</button>
              : <span className="px-note">{st.why}</span>}
            <button className="sx-btn" onClick={onDecline} data-testid="px-smon-decline">Use read-only sources</button>
          </div>
        </div>
      ) : !form ? <p className="px-empty">{tool} not scheduled — showing the live view from read-only sources.
        {st.can_schedule ? <> <button className="sx-link" onClick={() => setForm(true)}>Schedule {tx}…</button></> : null}</p> : null}

      {form ? (
        <form className="sm-form" data-testid="px-smon-form" onSubmit={e => e.preventDefault()}>
          <h4>Schedule {tx} daily monitoring in {sid}</h4>
          <div className="sm-fields">
            <label>Snapshot interval
              <span><input type="number" min={1} max={600} value={interval} onChange={e => setInterval_(Number(e.target.value))} onKeyDown={noEnter} data-testid="px-smon-interval" /> s</span></label>
            <label>Keep data for
              <span><input type="number" min={1} max={366} value={keep} onChange={e => setKeep(Number(e.target.value))} onKeyDown={noEnter} data-testid="px-smon-keep" /> days</span></label>
          </div>
          <p className="sm-what"><b>This writes to SAP</b> — the same jobs {tx}&apos;s own &quot;Schedule Daily Monitoring&quot; creates, run by the
            Sentinel RFC user:</p>
          <ul className="sm-what">
            {!st.today && !st.today_job ? <li>Job <code>/SDF/{tool}_SCHEDULER</code> now: today&apos;s run until 23:59, every {interval} s</li> : null}
            {!st.daily_scheduled ? <li>Job <code>/SDF/{tool}_SCHEDULER</code> every day at 00:00 from tomorrow (steps <code>/SDF/{tool}_START</code> + <code>/SDF/{tool}_REORG</code>)</li> : null}
            <li>{tool === "SMON" ? <>SMON&apos;s own watchdog job <code>/SDF/SMON_WATCHDOG</code>, which restarts stopped collectors</> : "Data older than the retention is removed by the reorg step"}</li>
            <li>One collector per application server runs in a dialog work process while monitoring is on</li>
          </ul>
          <div className="sx-actions">
            <button type="button" onClick={schedule} className="sx-btn sx-btn-primary" disabled={!!busy} data-testid="px-smon-confirm">Schedule in {sid}</button>
            <button type="button" className="sx-btn" onClick={() => setForm(false)} disabled={!!busy}>Cancel</button>
          </div>
        </form>
      ) : null}
      {busy ? <p className="sx-busy" aria-live="polite">{busy}</p> : null}
      {err ? <p className="sx-error" role="alert">{err}</p> : null}
      {result ? <ul className="sm-result" data-testid="px-smon-result">{result.map((r, i) => <li key={`${i}-${r}`}>{r}</li>)}</ul> : null}
    </div>
  );
}
