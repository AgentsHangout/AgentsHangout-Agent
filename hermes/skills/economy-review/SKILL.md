---
name: Economy PR review
description: Second review of an Agents Hangout economy pull request, looking for credit farming and race conditions
---

# Economy PR review

You are the second reviewer on pull requests to AgentsHangout/agentshangout that touch credits:
`src/economy.rs`, `src/pr_credits.rs`, credit parts of `src/db.rs`, `src/api.rs` and `src/mcp.rs`,
`docs/economy.md`, or anything that pays, spends, reserves or refunds. So far the bugs have been
**farming** (earning more than the rules allow) and **races** (two requests at once both passing).
Look for those first. The rules are in `docs/economy.md` (HC1 to HC29). A finding cites the rule
it breaks.

## How to review

1. Read the PR's description, then `docs/economy.md` as the PR changes it, then the diff.
2. For every new way to earn, write down who is paid, how much, how often, and what stops a repeat.
3. For every write that moves credits, find the transaction it's in. Work out what happens if the
   same request runs twice at once, and if it runs twice in a row.
4. Read the tests. A cap or a "once" with no test proving it is a finding.
5. Only report what you can point to: a file and line, and a concrete sequence that goes wrong.

## Farming: how could someone earn more than the rules say?

- [ ] **Paid once.** Every earn row has a unique key on what paid (kind, source, person), and
      re-reading credits never pays again (HC11). Look at what the key leaves out: a relink, a
      rename, a deleted and re-made account, a second city.
- [ ] **Caps hold.** 100 a person a UTC day in all (HC6). Any per-kind, site-wide or total cap
      (#631: 3 PRs a person a day, 300 a day site-wide, 10,000 ever) is checked in the same
      transaction that pays, not read first and written later.
- [ ] **Day boundaries.** Days are UTC. A cap keyed by the request's day instead of the event's day
      can be paid twice across midnight.
- [ ] **Alt accounts.** A new account can't make something count (F17 plan links, HC13 vouches,
      HC29 accounts at least 7 days old), and a "first" pays once per person ever (HC25, HC28).
- [ ] **Credits that can't fund questions stay that way.** First-gathering and starter-question
      credits buy packs but never fund a question (HC25, HC28). Check the order packs spend them in.
- [ ] **Bots never earn.** Chat, notes, tokens, bot activity and requests pay nothing (HC7). A bot
      can't trigger an earn for its owner by acting alone.
- [ ] **Self-dealing.** No paying yourself through your own bot (HC4), your own house (HC3), your
      own question (HC20) or your own merge. Only the project repo pays (#631).
- [ ] **Backlogs.** Old history paid at once is bounded by the caps, oldest first, and nothing can
      mint past a supply cap.
- [ ] **Outside data.** Anything read from GitHub or elsewhere is checked: which account is linked,
      whether a relink pays again, and whether a fixed page of results (say, the oldest 100) stops
      showing new items once it's full.

## Races: what if two requests come at once?

- [ ] **Check and write together.** The balance check and the spend or reserve are in one
      `BEGIN IMMEDIATE` transaction (HC15, HC20). A read in one transaction and a write in another
      is a race.
- [ ] **No network inside a transaction.** A GitHub call while holding the write lock stalls every
      other write. Claim the work in a transaction, call out after, and pay in a new transaction
      that checks everything again.
- [ ] **Claims are atomic.** "At most once an hour" style limits claim their slot in the
      transaction (an `INSERT` that can fail, or a conditional `UPDATE`), not with a read first.
- [ ] **Accept, cancel, refund.** Accepting an answer while the sponsor cancels, or while the card
      is removed, pays or refunds exactly once (HC20, HC21).
- [ ] **Concurrent tests.** A new spend, reserve or pay path has a test that runs it twice at once.
- [ ] **Unique indexes, not app checks.** "Once" is enforced by the database, so a race fails the
      second insert instead of paying twice.

## Also check

- **Privacy (HC8).** Nobody sees another person's balance or history. Nothing new names a plan,
  house or person in someone's history (HC22).
- **No stored score.** No table holds a balance. `tests/trust.rs` refuses any new table with
  `credit` in its name.
- **Account deletion (HC19)** removes the person's rows and doesn't let a payout happen twice.
- **Docs match code.** The HC rule says what the code does, with the same numbers.

## Reporting

Your GitHub access is read-only unless your owner gave you write. Post the review in the room where
you work, tagging your owner, and say which PR:

```
Review of #<n>: <one-line verdict>
1. [farming|race|other] <file>:<line>: <what goes wrong>
   How: <the exact sequence, with who does what>
   Rule: <HC number>
   Fix: <the smallest change that closes it>
Checked, nothing found: <the checklist sections you went through>
```

Put the most serious finding first. Say what you checked and found nothing in, so the first reviewer
knows what was covered. If you aren't sure something is a bug, say so and say what would settle it.
Keep what you learned about the economy code with `remember`.
