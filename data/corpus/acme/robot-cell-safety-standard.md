# Robot Cell Safety Standard (EHS-020)

Applies to: all Acme sites, demo floors, and Acme employees working at customer sites
Owner: Environment, Health & Safety (Renata Dlugosz, EHS Manager)
Last updated: 3/19/2026

---

## 1. Why this exists

An industrial robot doesn't know you are there. An ARC-20 can move a 20 kg payload at over 2 meters per second, and it does not slow down because somebody leaned in to grab a dropped part. Most robot injuries in our industry happen during maintenance, programming, and troubleshooting - not during normal production - because that is when people are inside the cell.

This standard sets the minimum rules for working in or near robot cells at Acme. Customers' own site rules apply on top of these when you are at their plant; if the two conflict, follow whichever is stricter.

## 2. Floor zones

Our production and demo floors are marked with painted lines:

- **Yellow line** - beyond this point safety glasses and safety-toe shoes are required.
- **Red-and-white hatching** - the guarded perimeter of a robot cell. Do not cross without following section 4.
- **Green walkway** - safe pedestrian path. Tern AMRs also use the walkways and always have right of way when loaded. Step aside; don't try to beat one to a doorway.

## 3. Training requirements

| Activity | Required training |
|---|---|
| Walking the floor unescorted | Safety 101 |
| Operating a cell (start/stop, load parts) | Cell Operator certification |
| Teaching / programming with the pendant | Robot Programmer Level 1 |
| Entering a guarded cell for maintenance | Lockout/Tagout (LOTO) Authorized |
| Collaborative (Kite) application setup | Collaborative Risk Assessment course |

Certifications are tracked in the learning portal and expire after 2 years, except LOTO Authorized, which must be renewed every year.

## 4. Entering a guarded cell

Before anyone enters a guarded cell for any reason:

1. Stop the robot using the normal stop on the pendant or HMI.
2. Apply your **personal** lock and tag to the cell's energy isolation point. Every person who enters applies their own lock. No shared locks, no "I'll just be a second."
3. Try to restart the cell to verify it is de-energized (try-out step).
4. Check for stored energy: gravity on raised axes, pneumatic grippers, springs. Support any raised axis before working under it.
5. Only the person who applied a lock may remove it. If someone left for the day with their lock on, contact the shift supervisor and EHS - do not cut it.

**Teach mode exception:** programmers may be inside the cell with power on only in reduced-speed teach mode (250 mm/s or less), holding an enabling device, and with a second trained person watching from outside the guarding.

## 5. Collaborative cells (Kite)

A cobot is not automatically safe. Collaborative operation is allowed only after a documented risk assessment that covers the tool, the part, the speeds, and the contact zones. Force and speed limits are set in the safety configuration and locked with a checksum. Changing a safety configuration requires sign-off from a Functional Safety engineer.

## 6. Emergency stops

- Every cell has at least two E-stop buttons reachable from outside the guarding.
- Anyone can hit an E-stop. Anyone. You will never be disciplined for a good-faith stop.
- After an E-stop, the cell is restarted only by a certified operator after the cause is understood.

## 7. Reporting

Report every injury, near miss, robot unexpected motion, or guarding defect in the EHS portal before the end of your shift. Unexpected robot motion at a customer site must also be reported to the Field Integration manager immediately, and it triggers an incident under the Incident Response Process.

## 8. Visitors

Visitors on the floor wear an orange badge, safety glasses and are escorted by a Safety 101-trained employee at all times. Visitors may never enter a guarded cell, even with the robot stopped.

## 9. Audits

EHS performs a monthly walk-through of every cell at HQ and Mon Valley, checking guarding, interlocks, E-stops and LOTO points. Findings are tracked to closure in the EHS portal. Interlock and E-stop function tests are logged quarterly per cell.

## 10. Questions

Ask any shift supervisor (yellow vest), the EHS team at ehs@acmerobotics.example, or #ehs on Slack.
