"""BASIS knowledge used to triage SAP's flags. Conservative by design: when
unsure, say 'review'. A false 'expected' hides a real problem; a false
'review' only costs a look."""

from __future__ import annotations

# Syslog (SM21) message IDs -> (triage, short meaning, what to do)
SYSLOG = {
    "EBI": ("act", "Background jobs failing to schedule",
            "The finding names the job and the user. A missing batch user means "
            "a standard job's step user does not exist in this client: create "
            "the user or reassign the job's step user."),
    "EBT": ("act", "Batch user does not exist",
            "Create the named user in the client or change the job step user."),
    "BY2": ("review", "Database error in the ABAP DB interface",
            "Check the following syslog line and the work process trace "
            "(ST11) for the failing statement."),
    "BY0": ("review", "Database error detail", "See BY2."),
    "TR2": ("review", "Secondary database connection failing",
            "A program uses a DB connection that is undefined or unreachable. "
            "Check the connection name in DBCO / DBACOCKPIT."),
    "FZ1": ("review", "Client without an SDM user",
            "Follow the SAP Note quoted in the message text."),
    "Q0R": ("review", "Gateway read failures",
            "Correlate with SMGW and the gateway trace; frequent failures "
            "point at an unstable RFC partner."),
    "Q0W": ("review", "RFC destination deleted",
            "Confirm the deletion was an authorised change."),
    "R48": ("expected", "Soft cancel after an SAP GUI disconnect",
            "Users closed SAP GUI during a request. No action."),
    "Q04": ("expected", "Terminal connection closed",
            "Normal user disconnects. No action."),
    "Q0I": ("expected", "Connection reset by peer",
            "Occasional resets are normal. Investigate only if frequent."),
    "E03": ("expected", "Security report executed",
            "Audit trace of a report run. No action."),
    "AB0": ("act", "Short dump written", "Analyse the dump in ST22."),
    "R49": ("act", "Communication error", "Check SM21 context and the dev_w trace."),
    "D01": ("review", "Transaction canceled", "Check the user and program in context."),
}

# SM12 lock tables that are held permanently or for long periods by design.
PERSISTENT_LOCK_PREFIXES = (
    "BGRFC_I_SERVER_REGISTRATION",
    "BGRFC_O_SERVER_REGISTRATION",
    "FDC_TIMERDAEMON_LOCK",
    "/SDF/SMON_CALL_KEY",
)

# SM04 session types that run at high priority by design.
HIGH_PRIO_BY_DESIGN = ("BGRFC", "BACKGROUND RFC")

CATEGORY = {
    "SM51": "Availability", "RZ03": "Availability", "RZ04": "Availability",
    "SMLG": "Availability", "RZ12": "Availability", "SMMS": "Availability",
    "SMGW": "Availability", "SICK": "Availability", "SCC4": "Availability",
    "ST02": "Performance", "ST06": "Performance", "ST03N": "Performance",
    "SM50": "Performance", "SM66": "Performance", "DB01": "Performance",
    "SM37": "Jobs and output", "SM13": "Jobs and output", "SP01": "Jobs and output",
    "SOST": "Jobs and output", "SRT_MONI": "Jobs and output",
    "SM21": "Logs and dumps", "ST22": "Logs and dumps", "ST11": "Logs and dumps",
    "DB12": "Database", "DB02": "Database",
    "CERTIFICATE INFO": "Security", "SLICENSE": "Security", "RSRFCCHK": "Security",
    "AL08": "Users and sessions", "SM04": "Users and sessions", "SM12": "Users and sessions",
}
