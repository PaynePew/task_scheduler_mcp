# ADR-055: Public abuse / cost containment posture

- **Status**: Accepted
- **Date**: 2026-05-21
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs)
- **Related**: ADR-042 (rate limiting — revised here), ADR-049 (verified `user_id`), ADR-052 (LLM caps), ADR-057 (overload protection), ADR-027 (VPS budget)

## Context

Going public (ADR-049) on a single 1 vCPU / 2 GB Lightsail box exposes a gap that
the existing controls do not cover:

- **ADR-042** caps job *creation rate* — and its defaults (1000/day, 10/min)
  were designed for a single trust-only identity, not many real users.
- **ADR-052** caps *LLM token spend*.
- **Neither covers steady-state load.** Recurring jobs are permanent: "anyone" +
  recurring on one core accumulates without bound. Capping creation rate does
  not bound the resident load of already-created recurring jobs.

## Decision

A per-user + global containment posture, all env-configurable, operator exempt.

| Dimension | Default (public user) | Note |
|---|---|---|
| Creation rate (ADR-042, **now per verified `user_id`**) | **100/day, 5/min** | down from the single-identity 1000/day |
| **Active recurring jobs per user** 🆕 | **≤ 5** | the key new gate — bounds permanent load |
| Active jobs total per user 🆕 | **≤ 50** | prevents single-user flooding |
| **Global active recurring ceiling** 🆕 | **≤ 500** → new creation waitlisted/rejected | final protection for the single core |
| LLM (ADR-052) | 50k tokens/user/day + global monthly $10 hard ceiling | restated for alignment |
| Operator (`OPERATOR_USER_ID`) | **exempt from all of the above** | owner |

ADR-042 is revised so its windows key off the **verified** `user_id` (ADR-049),
making per-user limits meaningful rather than shared across a single identity.

## Consequences

- New `task.create` checks: active-recurring count, active-total count, global
  recurring ceiling — in addition to ADR-042's rate windows.
- New env knobs (e.g. `MAX_RECURRING_PER_USER`, `MAX_ACTIVE_JOBS_PER_USER`,
  `GLOBAL_RECURRING_CEILING`) following the ADR-042 convention.
- Operator exemption requires the operator-identity bootstrap (`OPERATOR_USER_ID`
  = the operator's WorkOS `sub`) — see open follow-up.
- The global ceiling is *policy-based* admission control; *health-based* shedding
  is ADR-057.

## Alternatives considered

- **Keep ADR-042 defaults unchanged** — rejected: 1000/day × many users melts the
  box; designed for one identity.
- **No recurring cap** — rejected: leaves steady-state load unbounded, the exact
  gap this ADR exists to close.
