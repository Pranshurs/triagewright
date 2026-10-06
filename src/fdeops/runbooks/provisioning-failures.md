# Provisioning: failed workspace setup

## Read the job error
A job error of the form `requires:<feature>` means the account is not entitled to a
feature the workspace needs. Retrying without fixing the entitlement fails again.

## Stale entitlements
After a plan change, entitlements can lag behind the subscription. If the active
subscription's plan includes the feature but the entitlement is missing or disabled,
resync entitlements (safe, no approval) and then retry provisioning.

## Retry limits
Retry provisioning automatically at most while the job has fewer than 3 attempts.
At 3 or more attempts, escalate to engineering with the job history.
