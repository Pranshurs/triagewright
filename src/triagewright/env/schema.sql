-- Simulated business systems of a B2B SaaS vendor. All data is synthetic.

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL,            -- active | suspended | churned
    region TEXT NOT NULL,
    owner TEXT                       -- account manager
);

CREATE TABLE contacts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL               -- admin | billing | member
);

CREATE TABLE plans (
    code TEXT PRIMARY KEY,
    features TEXT NOT NULL           -- JSON list of feature keys
);

CREATE TABLE subscriptions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    plan TEXT NOT NULL REFERENCES plans(code),
    status TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE entitlements (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    feature TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    source_plan TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (account_id, feature)
);

CREATE TABLE invoices (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | paid | void
    period TEXT NOT NULL,
    issued_at TEXT NOT NULL
);

CREATE TABLE payment_events (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    invoice_id TEXT REFERENCES invoices(id),
    kind TEXT NOT NULL,              -- authorization | capture | refund | auth_expired
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    processor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL,            -- active | provisioning | failed | degraded
    tenancy TEXT NOT NULL,           -- dedicated | shared
    service_id TEXT NOT NULL REFERENCES services(id)
);

CREATE TABLE provisioning_jobs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    status TEXT NOT NULL,            -- queued | running | failed | succeeded
    error TEXT,
    attempts INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE services (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tenancy TEXT NOT NULL,           -- dedicated | shared
    health TEXT NOT NULL,            -- healthy | degraded | down
    restarts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE incidents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,            -- investigating | identified | resolved
    severity TEXT NOT NULL,
    service_ids TEXT NOT NULL,       -- JSON list
    started_at TEXT NOT NULL
);

CREATE TABLE tickets (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | pending | solved
    incident_id TEXT REFERENCES incidents(id),
    created_at TEXT NOT NULL
);

CREATE TABLE ticket_notes (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    public INTEGER NOT NULL,         -- 1 = sent to customer, 0 = internal note
    created_at TEXT NOT NULL
);

CREATE TABLE webhook_endpoints (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    url TEXT NOT NULL,
    status TEXT NOT NULL,            -- enabled | disabled
    consecutive_failures INTEGER NOT NULL
);

CREATE TABLE webhook_deliveries (
    id TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES webhook_endpoints(id),
    event TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE escalations (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    team TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Server-side idempotency store, like a payment processor's: a repeated key returns
-- the stored result instead of applying the effect again.
CREATE TABLE idempotency (
    key TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    result TEXT NOT NULL
);

-- Upstream-side journal of every write that took effect (first application only).
-- Kept by the systems, not by the agent: the scorer judges history from here.
CREATE TABLE effect_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,              -- JSON
    idempotency_key TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- Logical clock and id counters, so a reopened environment continues where it stopped.
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
