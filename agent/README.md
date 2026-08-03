# SAP GUI bridge agent (D-044 sandbox)

Lets the "SAP GUI (sandbox)" page in the app drive SAP GUI Scripting on
your Windows workstation. The container backend never connects to your
machine — this program connects OUT to the server, the same way your
browser already does, so no inbound firewall/hostname changes are needed.

## Setup (Windows workstation with SAP GUI installed)

```
pip install -r requirements.txt
```

Enable scripting in SAP GUI once: **Options → Accessibility & Scripting →
Scripting → Enable scripting**, and untick "Notify when a script attaches
to SAP GUI" for a smooth first run. Then log into the SAP system you want
to record against, same as always.

## Pairing and running

1. Open the app's **SAP GUI (sandbox)** page and click **Generate pairing
   code** — it shows the exact command to run, e.g.:

   ```
   python sapgui_bridge_agent.py --server ws://your-server:8000 --code ABCD1234
   ```

2. Run that on the workstation. The code expires after 5 minutes.
3. Once connected, the workstation shows up under "Bridge agent" on the
   page — record and replay from there.

## Trying it without SAP GUI installed

```
python sapgui_bridge_agent.py --server ws://your-server:8000 --code ABCD1234 --fake
```

`--fake` simulates a session so you can see pairing, the agent list, and
the record/replay flow work end to end before wiring up a real SAP GUI
session.

## Status

- Connect / pair / heartbeat / probe and REPLAY (`findById`, `.press()`,
  `.selected`, `.sendVKey`) use standard, documented SAP GUI Scripting API
  members — solid.
- RECORD uses a polling-diff of the active window's control tree (SAP GUI
  Scripting has no public "subscribe to user actions" event stream — SAP's
  own built-in Script Recorder is a separate, closed feature of the GUI
  client). This works but will need tuning against your actual screens —
  treat the first recordings as a starting point, not gospel, and tell us
  what's missing so it can be improved.
