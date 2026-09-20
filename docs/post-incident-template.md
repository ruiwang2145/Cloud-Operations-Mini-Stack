# Post-incident review template

Copy this into a new file under `docs/incidents/` (or a wiki, or the incident
tracker) and fill it in **within 48 hours**, while the timeline is still
recoverable from logs rather than from memory.

The rule that matters: **blameless**. A review that identifies a person as the
cause produces two outcomes — that person stops reporting near-misses, and the
next incident is found later. A review that identifies a *system* condition
produces a change that prevents the class of failure.

---

## Summary

| | |
|---|---|
| **Incident** | |
| **Date / duration** | |
| **Severity** | critical / warning |
| **Detection** | alert / user report / noticed during other work |
| **Time to detect** | |
| **Time to mitigate** | |
| **Time to resolve** | |
| **User impact** | |
| **Error budget consumed** | |

**One-sentence description.** Written for someone who was not there and will read
only this line.

---

## Impact

Quantify it. "Some requests failed" is not an impact assessment.

- **Requests affected:** `<count>` of `<total>` (from `app_http_requests_total`)
- **Availability during the incident:** `<sli>` against a `<target>` objective
- **Endpoints affected:**
- **Users affected:** all / a subset / unknown (and say which)
- **Data lost or corrupted:** yes/no — and if yes, how much, and how do you know
- **Duration of user-visible impact:** `<start>` → `<end>`

```promql
# Availability over the incident window
1 - (
  sum(increase(app_http_requests_total{status_class="5xx"}[<window>]))
  / sum(increase(app_http_requests_total[<window>]))
)

# Budget remaining afterwards
app_slo_error_budget_remaining_ratio
```

---

## Timeline

Every entry needs a timestamp and, where possible, a link to the evidence.
UTC. Reconstructed from logs and the alert history, not from memory — memory
produces a plausible timeline rather than an accurate one.

| Time (UTC) | Event | Evidence |
|---|---|---|
| | Change deployed / condition began | |
| | First failing request | |
| | Alert fired | Prometheus `/alerts` |
| | Alert acknowledged | |
| | Investigation started | |
| | Mitigation applied | |
| | Service recovered | |
| | Incident closed | |

**Detection gap:** the time between the first failing request and the alert.
**Response gap:** the time between the alert and the first action.

Both are more useful than the total duration, because they point at different
fixes.

---

## Root cause

Not "the deploy broke it". The chain:

1. **Trigger** — the change or event that started it.
2. **Mechanism** — how that produced a failure. The actual causal path.
3. **Why it was not caught earlier** — which check should have caught it, and why
   it did not. This is where the value is.
4. **Why it was not contained** — what should have limited the blast radius.

If the honest answer to (3) is "there was no check for this", that is a finding,
not a failure of the review.

---

## What went well

Genuinely. This section is not decoration — it identifies which parts of the
system to trust and to invest in.

- Did the alert fire before a user noticed?
- Was the runbook accurate? Did it get you to the answer, or did you work it out
  yourself and the runbook was scenery?
- Did the correlation id join the user's report to the log line?
- Did readiness correctly take a bad instance out of rotation?

---

## What went badly

- Which information was missing, and how long did it take to get?
- Which step took longer than it should have?
- Was anything done that made it worse?
- Was there a moment of hesitation about who should act? (There usually is. Write
  it down.)

---

## Action items

Each one needs an owner and a **date**. An action item without a date is a wish.

| Action | Type | Owner | Due |
|---|---|---|---|
| | detect / mitigate / prevent / document | | |

**Type matters.** "Add an alert" is detection. "Add a timeout" is mitigation.
"Fix the query" is prevention. A review that produces only prevention items leaves
the next, different incident just as invisible.

---

## Lessons for the runbooks

- [ ] Which runbook was used, and was it correct?
- [ ] What would have made it faster?
- [ ] Does a new runbook need to exist?
- [ ] Does [KNOWN_ISSUES.md](KNOWN_ISSUES.md) need a new entry?
- [ ] Does the SLO need to change — or was the objective right and the code wrong?
      (Both are legitimate conclusions. The second is more common.)

---

## Budget policy check

From [SLO.md](SLO.md):

| Budget consumed by this incident | Policy |
|---|---|
| < 50% | Normal. Ship features. |
| 50–100% | New work needs a reliability justification. |
| 100% | Feature work stops until the budget is restored. |

- [ ] Which band does this incident fall into?
- [ ] Does the policy apply, and has that been communicated?

---

## Appendix

- Links to the structured log extract for the incident window
- Prometheus graph permalinks
- The relevant Sentry issues
- Commits involved
- Any command output that is not reproducible later
