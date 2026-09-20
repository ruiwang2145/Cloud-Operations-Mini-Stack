# Runbooks

Operational procedures for the Cloud Operations Mini Stack.

## What a runbook is for

A runbook is not documentation of how the system works — that is
[`architecture.md`](../architecture.md). A runbook is what somebody follows at
03:00 when they are tired, under pressure, and do not have the codebase in their
head. It is written for the worst version of the reader, not the best one.

That means every runbook here is structured the same way:

| Section | Purpose |
|---|---|
| **Symptoms** | How you know you are in this runbook and not another one |
| **Impact** | What is actually broken, so you can decide how hard to panic |
| **First five minutes** | The minimum set of checks that either resolves it or narrows it down |
| **Diagnose** | The questions to answer, in order, with the exact commands |
| **Mitigate** | How to stop the bleeding — before fixing anything |
| **Verify** | How to prove it is fixed, not just quiet |
| **Follow-up** | What to write down while it is fresh |

## The order that matters

**Mitigate before you diagnose.** Restarting a wedged process or rolling back a
bad deploy restores service; understanding *why* it happened is valuable and can
wait thirty minutes. A runbook that starts with "open a debugger" is a runbook
that turns a five-minute outage into a fifty-minute one.

**Write down timestamps as you go.** The post-incident review is assembled from
what somebody remembered to note at the time, and memory reconstructs a
plausible timeline rather than an accurate one.

## Index

| Runbook | Use when |
|---|---|
| [service-down.md](service-down.md) | The service is unreachable, or `ServiceDown` / `NoTraffic` is firing |
| [high-error-rate.md](high-error-rate.md) | Error rate is elevated, or a budget burn alert is firing |
| [high-latency.md](high-latency.md) | Requests are slow, or `LatencyObjectiveBreach` is firing |
| [database-unreachable.md](database-unreachable.md) | Readiness is failing, or the database is the suspect |
| [deploy-and-rollback.md](deploy-and-rollback.md) | You are shipping, or a deploy just made things worse |
| [metrics-and-logs.md](metrics-and-logs.md) | You need to see what the service is actually doing |
| [disk-and-log-growth.md](disk-and-log-growth.md) | A volume is filling up |

## Alert → runbook map

The alert rules are generated from the SLO definition by
`scripts/render_rules.py`, and every alert carries a `runbook:` annotation that
points here. `ops/tests/test_alert_rules.py` fails the build if an alert points at
a file that does not exist, so this map cannot rot.

| Alert | Severity | Runbook |
|---|---|---|
| `ServiceDown` | critical | [service-down.md](service-down.md) |
| `AvailabilityBudgetBurnFast` | critical | [high-error-rate.md](high-error-rate.md) |
| `AvailabilityBudgetBurnSlow` | warning | [high-error-rate.md](high-error-rate.md) |
| `HighErrorRate` | critical | [high-error-rate.md](high-error-rate.md) |
| `ErrorBudgetExhausted` | warning | [high-error-rate.md](high-error-rate.md) |
| `LatencyObjectiveBreach` | warning | [high-latency.md](high-latency.md) |
| `ReadinessCheckFailing` | warning | [database-unreachable.md](database-unreachable.md) |
| `SloReporterDegraded` | warning | [metrics-and-logs.md](metrics-and-logs.md) |
| `NoTraffic` | warning | [service-down.md](service-down.md) |

## Severity, and what it means for you

| Severity | Meaning | Expected response |
|---|---|---|
| **critical** | Users are affected right now, or the budget will be gone within the day | Drop what you are doing |
| **warning** | Not yet user-visible, but the trend is wrong | Look during working hours; it will keep |

## Escalation

This is a single-service project, so escalation is simple and written down anyway,
because "who do I tell?" is the question people freeze on:

1. Acknowledge the alert.
2. Work the runbook.
3. If mitigation is not working after 30 minutes, or data loss is possible, stop
   and get help rather than continuing to improvise on a live system.
4. Write the timeline as you go — it becomes the post-incident review
   ([template](../post-incident-template.md)).
