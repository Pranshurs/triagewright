# Operations: degraded or unavailable workspace

## Before acting
Check open incidents first. If an open incident covers the workspace's service, do not
restart: link the ticket to the incident and tell the customer.

## Restart policy
A restart is a safe action only when the service is `dedicated` (single tenant),
no open incident covers it, and it has been restarted fewer than 2 times.
Shared services serve many customers; restarting one is never an agent action.
Escalate to engineering instead.
