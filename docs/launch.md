# M5 launch notes: recruiting the first 10-20 Kalshi traders

Goal (BUILD_PLAN 1): can 10-20 real Kalshi traders be persuaded to connect a
read-only key and share their page? Their objections are the next roadmap.

## Where

r/Kalshi, prediction-market Discords (Kalshi's own, Manifold, Metaculus
crossover), X accounts that post P&L screenshots. Traders who already post
screenshots are the target: they want to be believed.

## The post (draft; keep it short and honest)

> I built a small thing for Kalshi traders who post P&L screenshots.
>
> Connect a **read-only** API key and it pulls your fills straight from
> Kalshi, checks them against your balance every night, and gives you a
> public page with your verified equity curve, drawdown, monthly grid and a
> risk score. Nothing self-reported, no money moves, disconnect erases the
> key. Write-scoped keys are rejected automatically.
>
> It's an MVP. I want to know what would make you trust it, or what would
> stop you. Link + the "what we can and can't see" page: <URL>/security

## What to ask each person after they connect

1. Did the numbers match what you expected? (If not: reconciliation bug or
   a deposit/withdrawal, both worth knowing.)
2. Would you share the link? Where? What would you want on it first?
3. What made you hesitate before pasting the key?

## What to log

A decisions/objections file, PRD OQ-style: one line per objection, who said
it, and whether it changes the roadmap. Zero traction with a working
product falsifies the demand assumption cheaply (BUILD_PLAN 3, stop-loss).

## Before posting the link

- [ ] `scripts/preflight.py` passes against the production deployment.
- [ ] Your own real Kalshi account is connected and has reconciled at least
      three nights in a row.
- [ ] Kalshi API terms re-read for third-party read access on behalf of
      users (prerequisite checklist item, still open).
- [ ] Domain + HTTPS live; SECURE_COOKIES=true.
