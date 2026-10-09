---
name: jobsift-guided-operator-ux
description: Shape JobSift's private owner UI into a clear, approachable, reliable daily assistant.
metadata:
  origin: JobSift
---

# JobSift guided-operator experience

## One job
This is a one-person operating tool, not an engineering console. The main screen must answer, without specialist knowledge: **Is JobSift okay? What happened? Do I need to do something? What exactly should I do now?**

The owner should not need to understand GitHub Actions, Neon, workflow names, UUIDs, profile IDs, schedule cohorts, crawl tuning or delivery journals to operate the normal product.

## Display priority (strict)
1. **Safety / urgent issue:** uncertain Sheet write, stale or unverifiable client state, failed operation, paused client, disabled delivery. Explain what is known and blocked; give one safe next action.
2. **Owner decision:** prepared batch waiting for approval. Show the client, number of jobs, useful context, and safe review action. Do not turn review into an automatic send.
3. **Reported results:** jobs actually sent today, clients/delivery destinations actually reported, pending reviews. Label units precisely and show provenance/freshness.
4. **No action needed:** say there is no reported action to take **without claiming that jobs were found, delivered or workflows succeeded** just because a schedule is enabled.
5. **Tools:** rarely used manual controls, diagnostics, schedule tuning, Sheet repair and engineering details belong in clearly named secondary views/disclosures; never crowd the initial screen.

## Copy: babysit, don't quiz
- Use one ordinary sentence per state and one clearly labelled next action.
- Every error should say **what happened**, **what is safe/blocked**, **what to do next**.
- Call things by owner-facing names: "Jobs sent today", "Review waiting jobs", "Client", "Google Sheet". Avoid profile handle, workflow, hash, batch UUID and infrastructure terms in normal view.
- Don't write "Ready", "Active" or "Running" unless the underlying evidence supports exactly that claim. Enabled schedule does NOT prove a completed crawl.
- Do not invent zeros for unknown counts; a missing/partial/capped Neon snapshot is **unknown**, not "0 jobs".
- Do not auto-retry uncertain deliveries. Buttons must tell the user their consequences, require confirmation for destructive/irreversible actions, and preserve backend checks.
- No modal forms demanding unrelated inputs; use step-by-step client onboarding when needed.

## Visual system
- Strong single focal region for the next action; quiet typography and restrained colour, not a row of competing "Run" buttons.
- Small set of stable design tokens; accessible contrast and visible focus. Use native controls and readable line lengths.
- Mobile-first: one-column reading order, thumb-size targets (44px), no sideways scrolling on the primary path.
- Both light and dark theme; no gradient or animation as a substitute for a state or instruction.
- Every piece of information earns its place by helping a **routine** decision. No decorative counters, charts or badges.
- Information disclosed in Advanced must remain discoverable by clear labels; truly hazardous recovery must be labelled as such.

## Resource guardrails
The frontend must never trigger crawling, Sheet delivery, database writes or expensive state synchronization just by rendering or showing a status card. Manual sourcing and recovery can consume Neon transfer and GitHub Actions usage; warn before running them. Backend freshness <=24h, strict matching, dedupe, quotas, safe Sheet retries, verified capabilities and owner authentication remain authoritative.

## Checks on every UI PR
- Test: normal verified, pending review, paused, failed, incomplete/unknown, empty client list, uncertain delivery, and API failure.
- Test: a button's actual action matches its text and reaches the disclosed destination.
- Test: no normal-state technical jargon and no daily page crammed with rare controls.
- Test on 360px mobile and desktop, keyboard and dark themes.
- Verify no extra Neon calls or crawling were added; no production workflow dispatches as a UI test.
- Request an independent Codex review; don't merge before green checks.

## Design resources (for principles, not runtime dependencies)
- Anthropic frontend-design: https://github.com/anthropics/skills/tree/main/skills/frontend-design
- Vercel web-design-guidelines: https://github.com/vercel-labs/agent-skills/tree/main/skills/web-design-guidelines
- UI/UX Pro Max: https://github.com/nextlevelbuilder/ui-ux-pro-max-skill
- Nielsen Norman Group progressive disclosure: https://www.nngroup.com/articles/progressive-disclosure/

Use these as design reference, not as arbitrary build dependencies or authority over JobSift's product contract.
