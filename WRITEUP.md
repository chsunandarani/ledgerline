# Write-up

## What I built

LedgerLine is a small system for moving money between accounts. There are three kinds of users: admins, account holders and auditors. You can open accounts, deposit, withdraw, transfer between accounts, reverse a payment, see a statement for any account you're allowed to see, and check that the books are right. There's a simple web page for all of it, and a JSON API underneath. It deploys to Render from `render.yaml`, and the README has a login for each role.

## How the money stays right

Money is stored as whole paise, never as decimals, so there's no rounding.

Every movement is a transaction made of two or more postings, and the postings always add up to zero. If ₹100 leaves one account, ₹100 arrives in another. Money coming in from outside, or leaving to the outside, goes through a system account called `EXTERNAL`. Because of that, there's one rule I can check at any time: if you add up every balance in the system, including EXTERNAL, you get exactly zero. And the total that customers hold can only change through deposits and withdrawals.

The `balance` stored on each account is really just a fast copy. The postings are the actual record, and the database won't commit a balance that doesn't match them.

## What happens on one request

Everything happens inside one database transaction, so either all of it happens or none of it does.

First it checks the user's role and whether they're still active, again, with a lock. This matters because their account might have been changed since the request came in. Then it looks up the idempotency key. If this user already sent this key, it returns the answer from last time and stops.

Next it checks the request is allowed. Who owns an account comes from the database, never from whatever the client sends. If the request isn't allowed, it's still saved, as a refused transaction, so it shows up on the statement.

Then it locks the accounts involved, always in order of account id, and checks the balance using those locked values. A balance can't go below zero, or below minus the overdraft limit if the account has one. If that's fine, it writes the postings, updates the balances and adds the entry to the hash chain. Then it commits.

## Why I used row locks

There are two common ways to stop two requests from spending the same money. One is to lock the rows. The other is to run transactions at SERIALIZABLE isolation and let the database cancel one of them when they clash.

Both are correct. I went with locks because of what happens in a race. With a lock, the second request just waits for the first to finish, then sees the real balance. So every answer is either "done" or "refused". With SERIALIZABLE, the database aborts one of them and I'd need retry logic around every money request, which is more code in the place where mistakes cost the most.

Locks can cause deadlocks if two requests grab them in different orders, for example a transfer from A to B at the same time as one from B to A. To avoid that, the locks are always taken in the same order: accounts by id first, then the hash-chain head.

## A second line of defence

The application checks come first, but the database also refuses bad data on its own. It won't let a customer balance go below its floor. At commit it checks that every transaction's postings add up to zero, that each posting carries on correctly from that account's previous balance, and that each account's balance matches its last posting. So even a bug in my code, or someone typing SQL directly, can't commit money that came out of nowhere.

## Making sure a payment happens only once

Every money request has to carry an idempotency key. The key is stored on the transaction row itself, and there's a unique rule on the pair (user, key). That means the key and the money are saved together. There's no moment where the money has moved but the key isn't saved yet.

If the same request arrives twice, maybe because of a timeout and retry or a double click, the second copy waits for the first, then gets back exactly the same answer and moves nothing. Keys are kept forever, so even a retry that turns up much later is safe. If someone reuses a key for a different request, it's rejected and flagged.

Every request that comes in, including the duplicates, is written to `request_log`. That's how I can show in the data which requests were duplicates.

## Proving what happened

The ledger tables and the audit table can't be updated, deleted or truncated. Refused attempts are saved too, so you can see who tried what and why it was refused.

On top of that, each transaction is hashed together with the one before it, like a chain. The Verify button in the auditor's view recalculates the whole chain and re-adds every account from its postings. If anyone changes a row, even a database superuser who switches the triggers off first, the chain breaks and Verify shows it. The one way around this is to rewrite every row after the changed one. That's why Verify shows the latest hash, so an auditor can note it down somewhere outside the system and compare later.

Changes to users and accounts, and every login, are logged by triggers inside the database rather than by my code. So there's no code path that can forget to log something, and changes made with raw SQL get logged too and flagged. In production the app should connect as a database role that can only add rows to history. It can't edit them, delete them or switch the triggers off.

## How I know it works

There are 184 automated tests. They run against a real server and a real Postgres database, because locks and triggers only really exist there. They fire up to 300 requests at the same moment from separate threads, replay requests, try to move other people's money, and go straight to the database to try to edit history.

There's also a mutation script. It takes the code, removes one lock or one check, and runs the tests again, once for each protection. All 18 protections it removes are caught. On top of that, the running server was sent junk input directly, and that found a few bugs the tests had missed. They're described in PROMPTS.md.

## What I left out on purpose

An admin can still do things alone: deposit, withdraw, reverse a payment, change someone's role. It's all logged and flagged for the auditor, but nothing requires a second person to approve it.

A database superuser can switch the triggers off and change a user without leaving a log entry. The real fix would be sending the audit log somewhere they don't control.

I didn't build multiple currencies, holds, scheduled or batch payments, fees or daily limits. Each of these is one more way for balances to go wrong, and I'd rather hand in a small system that's always right than a big one that loses a rupee under load.

There's no rate limiting, apart from locking a username for 15 minutes after 5 wrong passwords.

It isn't fast at scale. Every write passes through one lock on the hash-chain head, so writes happen one at a time. That's simple and easy to check, but it would need a different design for real volume.

## Using AI, and where it was wrong

I used Claude for most of the code, and PROMPTS.md has each prompt and what went wrong. The things I learned from it:

Tests can pass for the wrong reason. Three times, a test still passed after the thing it was meant to check had been removed. Usually another protection was covering for the missing one, or the race it was meant to create never actually happened. The mutation script is what showed this.

Sending junk input to the live server found bugs the tests didn't.

The AI's explanations can be wrong too. It described a password bug that turned out not to exist once it was actually tested.

And reviewing the threat list found a real hole: someone deactivated in the middle of a request could still move money.

## What's not finished

Two-person approval for admins, saving the hash-chain head somewhere outside the database, and rate limiting.

The Docker image hasn't been built yet on a normal machine. The start command was tested without Docker.

The demo deployment connects to the database as the owner unless the extra steps in the README are done.

The web page works but it's plain, and long statements are cut off at a limit instead of being split into pages.
