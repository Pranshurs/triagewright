# Entitlements: customer cannot access a paid feature

## Check in order
1. Active subscription plan (billing is the source of truth for the plan).
2. Plan feature list.
3. Account entitlements and their `synced_at`.

## Remedy
If the plan includes the feature and the entitlement is missing, resync entitlements.
This is safe and needs no approval. If the plan does not include the feature, do not
grant it; explain the plan difference and offer account management.
