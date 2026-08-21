# Daily — the standing session

Runs on a schedule, unattended. Boring on purpose. Never sends anything.

1. `tg_coverage`. If the last sync is older than 2 hours, say so at the top of the
   report and continue — do not silently report on stale data.
2. What changed since the last run:
   - new counterparty chats → new `brain/accounts/` file (draft, marked as such)
   - new promises made by either side → append to the account file, dated, cited
   - new asks → `brain/tasks/OPEN.md`
   - anything that closed → mark it closed with the message that closed it, do not
     delete the line
3. `tg_waiting_on_us(days=2)` — threads where they spoke last and we did not.
   Cross-check against `brain/tasks/OPEN.md` so a thread that is genuinely waiting
   on THEM is not reported as ours.
4. Anything promised with a date that has passed and no evidence of delivery.
5. Write today's report to `brain/reports/YYYY-MM-DD.md` and print the top of it:
   - what is waiting on us, longest first
   - promises past due
   - new accounts worth a human look
   - what you could not read

## What makes this useful rather than noise

- **Only actionable things get raised.** A thread nobody is waiting on is not a
  finding. If a section is empty, say "nothing" — do not pad it.
- **Never re-raise what a human has already answered.** Check the previous report.
- **Never rewrite yesterday.** Today is a new file. The history of what you
  believed on each day is itself evidence.
