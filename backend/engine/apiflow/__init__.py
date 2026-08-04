"""D-047: API-level flow composition — the replacement for screen record/replay.

The premise, after three attempts at recording the PRESENTATION layer
(WebGUI DOM widgets, SAP GUI COM scripting, eCATT screen scripts) all
proved fragile or unworkable:

    Record/replay breaks because screens have no stable contract.
    Underneath, the operations DO have stable contracts — RFC/BAPI
    signatures and OData service metadata are things SAP maintains
    across releases.

So a "flow" here is an ordered list of typed OPERATIONS (call this RFC
function with these parameters; POST this OData entity with this payload)
rather than a list of clicks. Replay is then just re-issuing those
operations server-side: no browser, no Windows agent, no COM, no SCC4
gate, no client install of any kind — and, unlike the COM path, fully
testable in CI against a fake SAP.

`discovery.py` is the first piece: it inventories what a given system
actually exposes, which is both the answer to "is this landscape
Fiori/Gateway enabled?" and the raw material a flow is composed from.
"""
