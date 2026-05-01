# ADR-0012: Public Demo Account with Shared State and Reset Button

**Date:** 2026-05-01
**Status:** Accepted
**Deciders:** AuditPilot maintainers
**Refs:** ADR-0004; PRD §4.1, §6; system-design.md §3.6 (new); PLAN.md Sprint 11

---

## Context and Problem Statement

The public demo URL (`auditpilot.dev`) has two distinct visitor types:

1. **Real users** who want to use AuditPilot for SOC 2 readiness work. They sign up with email or GitHub OAuth, connect their own GitHub organization, and run scans against their own evidence.
2. **Casual reviewers** — hiring managers, fellow open-source contributors, conference attendees — who want to understand what AuditPilot does in 90 seconds without committing to a sign-up or connecting their own GitHub.

The first group is straightforward: they sign up, they get their own account, they connect their tools, the system works as designed.

The second group is the design problem. They land on auditpilot.dev. If the only path forward is "Sign up + connect GitHub," the bounce rate is ~95% — they leave without seeing anything functional. A static demo video on the landing page covers some of this, but the magic moment of AuditPilot (clicking "Approve and download" on a drafted policy, watching the orchestrator stream tool calls in real time) cannot be conveyed in a video the way it can in a live UI.

The question is: what does a casual reviewer experience when they want to "try the demo" without signing up?

---

## Decision

**Public demo account with shared state, a "Reset demo" button visible to anyone, and a 24-hour automatic reset cron. Implemented as a special-cased user with a stable `user_id`, pre-loaded with a curated synthetic seed dataset.**

Concretely:

1. Landing page has two CTAs: "Sign up" (normal flow) and "Try the demo" (special).
2. "Try the demo" signs the visitor in as the demo user (`demo@auditpilot.dev`) using a pre-issued JWT — no sign-up, no OAuth.
3. The demo user's account contains a curated seed: 64 controls populated with mixed PASSING / FAILING / NOT_ASSESSED states, sample evidence, sample Pending Actions, one drafted Incident Response Plan, one filled SIG-Lite questionnaire, one mock readiness challenge gap report.
4. Visitors share the same demo state. Two simultaneous visitors see the same Pending Actions queue.
5. A persistent banner at the top reads: "This is the public demo. State is shared with all visitors. Click [Reset demo] to restore the seed, or [Sign up] for your own account."
6. The "Reset demo" button is in the banner. Anyone can click it. It clears the demo user's mutable data (actions completed, policies approved, questionnaire edits) and reloads the seed.
7. A daily cron at 03:00 UTC auto-resets the demo data to seed regardless of button activity.

---

## Rationale

### Why a single shared demo account, not per-visitor isolated copies

Per-visitor isolation was considered and rejected. The proposal was: on "Try the demo," create a temporary user, attach a fresh copy of the seed, drop the user after 1 hour idle.

Why rejected:

1. **Implementation cost.** Creating temporary users, copying seed rows, scheduling cleanup, and handling reconnects on tab refresh is approximately 1.5 days of work. The shared-account model is approximately 0.5 days.
2. **Concurrent visitor count is low.** AuditPilot's portfolio launch will see 1–3 visits per week peak, ~10 during a Show HN spike. The probability of two simultaneous visitors editing the same demo state in the same minute is low.
3. **The reset button + 24h cron is sufficient.** If a visitor walks into a messy demo state, they click "Reset demo" and see clean seed in two seconds. No friction.
4. **The honest UX disclosure.** The banner says "state is shared." A reviewer reading that line either accepts it (most cases) or signs up for their own account (the case we want).

### Why include the demo at all (not just a video)

A landing page video is the cheap option. The reasons to also include a live demo account:

1. **Magic-moment fidelity.** The streaming SSE rendering, the live Tool cards, the click-to-approve flow on the policy editor — none of these convey through a video the way they do live.
2. **Self-paced exploration.** A reviewer can poke at the dashboard, drill into a control, click into a Pending Action, and form their own understanding rather than watching the maintainer's demo path.
3. **Forkability signal.** A reviewer who clones the repo and runs `docker compose up` lands on the same seed data via the demo account, validating that the project actually runs end-to-end.

The video is still on the landing page (it satisfies the visitor who has 30 seconds, not 5 minutes). The demo account satisfies the longer visit.

### Why a Reset button (not auto-reset on every "Try the demo" click)

Auto-reset on each entry would make the visitor experience consistent. Reasons against:

1. **Race conditions.** Visitor A is mid-demo when Visitor B clicks "Try the demo" and triggers a reset. A's session breaks.
2. **The reset is heavy.** Resetting 64 controls + ~50 evidence rows + ~10 actions + ~3 policies + ~1 questionnaire is a 200ms operation that fights with whatever scan or HITL session a current visitor has open.
3. **The cron handles the worst case.** If state gets too messy for any visitor to make sense of, the next 03:00 UTC cron cleans it up. The reset button is the manual override.

### Why no real OAuth for the demo account

The demo account does not connect to a real GitHub organization. The seed evidence is synthetic. Reasons:

1. **Privacy.** A real GitHub org under "demo@auditpilot.dev" would need a maintainer-controlled GitHub account, which means real branch protection rules, real MFA settings, real members. None of that is appropriate to expose to anonymous visitors.
2. **Determinism.** Synthetic seed data is deterministic — visitors see the same state regardless of what GitHub did at 3am. A real org has flaky API behavior that breaks the demo.
3. **Cost discipline.** Real GitHub MCP calls cost LLM tokens (orchestrator runs the control mapping). Synthetic seed avoids that cost entirely; visitors see pre-computed results.

The trade-off: visitors do not see the live "connect GitHub, watch it pull real data" experience. The video covers that.

### Why the demo is a v1.0 launch requirement (not a v1.5 polish item)

The PRD's success metric is GitHub stars + Show HN top-10 (PRD §4.1). Both are driven by hiring-manager-quality first impressions on auditpilot.dev. Without a functional demo, the public URL is essentially a marketing landing page — which is significantly weaker for the project's positioning as a "production-grade reference architecture."

The cost is approximately one Sprint 11 day. It buys the entire demo experience for every visitor for the lifetime of the public URL.

---

## Implementation sketch

### Demo user identity

A constant in `apps/api/config.py`:

```python
DEMO_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
DEMO_USER_EMAIL = "demo@auditpilot.dev"
```

The user row exists in the `users` table with a special `is_demo: true` flag.

### Demo sign-in endpoint

```python
@router.post("/api/auth/demo")
async def demo_signin(
    response: Response,
    redis: Redis = Depends(get_redis),
):
    # Rate limit: 30/minute per IP to prevent abuse
    ip = request.client.host
    if not await rate_limiter.check(f"demo-signin:{ip}", limit=30, window=60):
        raise HTTPException(429, "demo sign-in rate limit")

    # Issue a short-lived JWT for the demo user
    token = create_jwt(
        user_id=DEMO_USER_ID,
        email=DEMO_USER_EMAIL,
        is_demo=True,
        ttl_minutes=60,
    )
    response.set_cookie("sb-access-token", token, httponly=True, secure=True, samesite="lax")
    return {"redirect_to": "/dashboard?demo=true"}
```

### Reset endpoint

```python
@router.post("/api/demo/reset")
async def reset_demo(user: User = Depends(current_user)):
    if not user.is_demo:
        raise HTTPException(403, "reset is only for the demo account")

    # Atomic: delete mutable demo state, re-insert from seed fixture
    async with db.transaction():
        await db.execute("DELETE FROM actions WHERE user_id = $1", DEMO_USER_ID)
        await db.execute("DELETE FROM evidence WHERE user_id = $1", DEMO_USER_ID)
        await db.execute("DELETE FROM control_map WHERE user_id = $1", DEMO_USER_ID)
        # ... full delete list
        await load_seed_fixture(db, DEMO_USER_ID)
    return {"reset_at": datetime.utcnow()}
```

### Cron reset

Vercel Cron at `0 3 * * *` calls `POST /api/demo/reset` with the cron token. The endpoint verifies the cron token bypasses the demo-user check.

### Banner component

```tsx
{user.is_demo && (
  <div className="bg-yellow-100 border-b border-yellow-300 p-2 text-sm">
    This is the public demo. State is shared with all visitors.
    <button onClick={resetDemo} className="ml-2 underline">Reset demo</button>
    <a href="/sign-up" className="ml-2 underline">Sign up for your own account</a>
  </div>
)}
```

### Seed fixture

`apps/api/seeds/demo_seed.sql` contains the curated dataset. ~200 lines. Hand-authored to show the most demo-relevant features:

- 64 controls with realistic mixed status (e.g. 38 PASSING, 18 NOT_ASSESSED, 8 FAILING)
- 6 Pending Actions with drafted fix text
- 1 fully-drafted Incident Response Plan in approved state
- 1 partially-filled SIG-Lite questionnaire (78/128 cells auto-filled)
- 1 completed mock readiness challenge with 4 findings

Fixture is regenerated when the project's SOC 2 control catalog or the policy templates change. Manual process; not automated.

---

## Consequences

### Positive

- Casual reviewers see functional AuditPilot in 60 seconds without sign-up
- Hiring managers can poke at every feature without connecting their own GitHub
- Synthetic seed data is privacy-safe and deterministic
- Reset button + 24h cron handles state-mess recovery without per-visitor isolation complexity
- Total implementation cost ~0.5 day in Sprint 11

### Negative

- Shared state means two simultaneous visitors can see each other's actions; the banner discloses this
- Synthetic seed does not show live GitHub MCP calls — that experience is only available to signed-up users
- Reset endpoint must be carefully scoped to the demo user only; a privilege bug here would reset real users' data (mitigation: `is_demo` check + RLS policy + tested by `security-reviewer` sub-agent)
- Demo user JWT issuance is rate-limited per IP; an abusive visitor could exhaust the rate limit and block legitimate visitors briefly

---

## Alternatives Considered

| Option | Why rejected |
|---|---|
| **Per-visitor isolated copies** | 1.5 day implementation cost vs. 0.5 day for shared. Concurrent-visitor count at portfolio scale does not justify the complexity. |
| **Read-only demo (no edits allowed)** | Loses the magic moment — the value is in clicking "Approve and download," which requires writes. |
| **No demo, only video** | Lower fidelity for the magic-moment experiences (streaming SSE, live Tool cards, HITL approve). |
| **Time-limited demo session (auto-clean after 1 hour)** | Same complexity as per-visitor isolation; same reasoning to reject. |
| **Sign-up required, free tier removes friction** | Sign-up + GitHub OAuth + scan-time still excludes the visitor who has 90 seconds. The whole reason for a demo account is the 90-second visitor. |
| **Demo on a separate subdomain** (`demo.auditpilot.dev`) | Adds a Vercel deployment target. Same cookie domain rules complicate auth. The query-param approach (`/dashboard?demo=true`) is simpler. |

---
