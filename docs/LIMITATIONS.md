# Known limitations

- **Scripted agents prove the runtime and the scorer, not diagnosis quality.** The
  scenario arms are fixed scripts. A live model can be plugged in through the model
  protocol, but no live-model results are claimed.
- **Grounding is atomic.** A fact binds a value to a record and field inside a cited
  observation, which rejects a real value taken from the wrong entity or field. It does
  not verify the relationship a finding's prose asserts (for example that two captures
  are duplicates of each other), nor that the cited record is the one the prose is
  about.
- **Customer-facing text is not fact-checked.** A reply that misstates what happened is
  only penalised indirectly, when required effects or predicates fail.
- **Golds are hand-written.** They are independent of the runtime code but share the
  author's understanding of each scenario.
- **The operator is simulated** by per-scenario rules.
- **Unknown outcomes on simulated tools are settled by the operation's idempotency
  key**, which assumes the upstream honours keys. The HubSpot note, whose upstream does
  not, is settled by a read-only lookup instead and can stay unknown (`HUBSPOT.md`).
- **The HubSpot connector is exercised against a local fake** of the endpoints it
  uses. The fake follows HubSpot's documented shapes; it is not HubSpot.
- **Single process.** Sessions persist to disk and resume after a crash, but there is no
  concurrent access control between two runners on one case.
