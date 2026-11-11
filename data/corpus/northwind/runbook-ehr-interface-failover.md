# Runbook - EHR Interface Engine Failover

`Owner: Clinical Integration Engineering  |  On-call schedule: "Integration-Primary"  |  last updated 2026.06.03`

## Summary

All HL7 and FHIR traffic between the EHR and our other systems (lab, radiology, e-prescribing, billing clearinghouse, Northwind Anywhere) flows through the **Bridgeway interface engine**. Bridgeway runs as an active/passive pair:

| Node | Location | Role |
|---|---|---|
| bwy-prod-a | AWS us-east-2 (primary) | Active |
| bwy-prod-b | AWS us-west-2 | Passive, warm standby |

If Bridgeway stops processing messages, lab results stop reaching charts, orders stop reaching the lab, and prescriptions can get stuck. That is a patient safety risk, so move quickly.

## Symptoms / alerts

- Datadog: `bridgeway.queue.depth > 5000` on any outbound channel for 10 minutes
- Datadog: `bridgeway.heartbeat` missing for 3 minutes
- Clinicians report "lab results not showing up" or "order says pending for hours"
- Lab partner calls the Service Desk saying they stopped receiving orders

Severity: if lab results or e-prescribing are delayed more than 1 hour for more than one clinic, declare SEV2. If the engine is completely down, or e-prescribing is failing everywhere, declare SEV1.

## Before you fail over

Failover is disruptive (about 8-12 minutes of no traffic) and messages queued on node A must be replayed. Try these first:

1. Check whether the problem is one channel or all of them. In the Bridgeway console, open **Channels > Status**. One red channel = probably a downstream partner issue, not the engine.
2. For a single stuck channel: stop and start the channel. If messages are erroring, look at the first error - a malformed message can block the queue. Move it to the "quarantine" folder and restart the channel.
3. Check disk on bwy-prod-a. Bridgeway stops accepting messages above 90% disk. Archive old logs:
   ```
   sudo /opt/bridgeway/bin/archive-logs.sh --older-than 3d
   ```
4. Restart the Bridgeway service on node A (`sudo systemctl restart bridgeway`). Takes about 3 minutes.

If the engine is still down after step 4, or AWS us-east-2 is having a regional issue, fail over.

## Failover procedure

**Get approval** from the Incident Commander before starting. Tell #clinical-apps that a failover is starting.

1. On bwy-prod-a (if reachable), stop the service so it does not process duplicate messages: `sudo systemctl stop bridgeway`
2. In Route 53, change the `bridgeway.internal.northwind` record to point to bwy-prod-b. TTL is 60 seconds.
3. On bwy-prod-b, start Bridgeway in active mode:
   ```
   sudo /opt/bridgeway/bin/set-role.sh active
   sudo systemctl start bridgeway
   ```
4. Confirm the lab, radiology and eRx channels on node B go green within 5 minutes.
5. **Replay messages.** Export the unprocessed queue from node A's last snapshot (EBS snapshots run every 15 minutes) and import it on node B with the replay tool. Duplicates are filtered by message control ID (MSH-10), but double-check the eRx channel manually - duplicate prescriptions are the main risk.
6. Ask the Clinical Liaison to have one clinic confirm that a test lab order round-trips.

## Failback

Do not fail back during clinic hours. Schedule it for after 10:00 p.m. CT with the change advisory board, using the same steps in reverse.

## Things that went wrong before

- **March 2025:** we failed over but forgot to update the clearinghouse VPN tunnel, so claims stopped for two days without anyone noticing. Step: check the billing channel too.
- The replay tool requires the node B service account to have read access to node A snapshots. That permission was removed once in a cleanup and broke failover during a drill. It is now in Terraform - do not remove it.

## Contacts

- Integration on-call: PagerDuty "Integration-Primary"
- Bridgeway vendor support: see Vendor Escalation Matrix (contract tier: Platinum, 30-minute response)
- Lab partner integration desk: number in the Vendor Escalation Matrix
