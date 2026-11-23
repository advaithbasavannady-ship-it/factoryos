# Phase 1.2 owner decision proposals

**Status:** proposals only. No department redaction, retention deletion, paid provider, external integration, production access or deployment is authorized by this document. Technical detail is in `PHASE_1_2_REMAINING_GATES.md`.

## 1. Department field visibility

**Decision:** approve a field policy for order, customer, Job Card, production and quality responses.

| Option | Behavior | Main risk |
|---|---|---|
| A. Existing full payload | Department filters limit records, but returned records retain all fields | Unrelated pricing, customer, supplier or department details may be exposed |
| B. Per-role/per-department allowlists | Each response contains only fields approved for that role and department | More policy and maintenance work; mistakes can hide fields needed to work |
| C. Data classification plus allowlists | Mark fields as company-wide, department-only, commercial, personal or technical, then generate scoped response models | Requires initial classification and owner maintenance |

**Engineering recommendation:** use explicit allowlists, informed by data classification. This best limits accidental disclosure while allowing required operational fields. Approve/revise the detailed matrix and answer the open questions in the companion document before implementation. No redaction has been made.

## 2. Failed-login and security-event audit

**Decision:** choose durability, retention and outage behavior for failed-login, rate-limit, origin-denial and generic middleware events.

| Option | Behavior | Main risk |
|---|---|---|
| A. Best-effort application audit | Write to Mongo and log failures, as today | Events can be lost during database outages; generic middleware runs after the response |
| B. Fail authentication closed on audit failure | Reject login/security operations when the audit write fails | Audit outage becomes an authentication outage; attackers may deliberately trigger it |
| C. Separate monitored security-event sink | Emit structured, token-free events to an independently durable sink; alert on delivery failure | Requires selecting/operating a sink and setting retention/access policy |

**Engineering recommendation:** use C for security telemetry; retain transaction-coupled fail-closed auditing for successful business/security state mutations. Do not make ordinary valid login availability depend solely on telemetry availability unless the security owner explicitly chooses that tradeoff. Decide retention, alert thresholds, access roles and outage handling before changing behavior.

## 3. Transactional email outbox

**Decision:** choose the email provider and operational key/retry policy before implementation.

| Choice | Alternatives | Engineering recommendation / risk |
|---|---|---|
| Provider | Existing SMTP; existing managed provider; new provider | Prefer an already approved provider with delivery status and idempotency support. Do not introduce a paid service without approval. SMTP commonly gives at-least-once delivery only. |
| Payload encryption | Existing managed KMS/secret manager; dedicated application-managed key | Prefer an already operated managed key if available; keep it independent of JWT signing. Application-managed keys add rotation and access-control burden. Never store raw reset/invitation tokens in plaintext. |
| Retry | Immediate-only; bounded exponential backoff; unbounded retry | Recommend bounded exponential backoff with jitter, attempt cap, visible dead letter and alert. Owner must approve cap and alert responsibility. |
| Duplicate delivery | Provider idempotency; stable `Message-ID` only | Prefer provider idempotency when available; otherwise document at-least-once delivery and use single-use tokens. Exactly-once SMTP delivery cannot be promised. |
| Retention | Delete sent rows quickly; archive; retain | Do not TTL-delete pending/dead-letter events. Approve retention/archive for sent rows and encrypted payloads before cleanup. |

**Implementation shape:** put token-state mutation, audit event and encrypted outbox row in one transaction. A worker leases, sends, marks sent, retries transient failure, and reports stale leases/dead letters. No provider, key service or outbox has been selected or implemented.

## 4. Idempotency retention

**Decision:** decide whether exact original response replay must last as long as the duplicate-prevention key.

| Option | Behavior | Main risk |
|---|---|---|
| A. Keep full record forever | Keep request binding and full response | Predictable replay; largest storage growth |
| B. Keep permanent tombstone, compact response later | Retain tenant/key/actor/operation/payload/resource binding; later reconstruct response from the resource | Exact original response may no longer be reproducible; needs API contract change and tests |
| C. Delete all after a TTL | Key may be reused after expiry | A delayed retry can repeat a stock/financial effect |

**Engineering recommendation:** B is the storage-conscious option that preserves replay protection, but it changes response replay semantics. Until that change is approved and tested, retain current full results indefinitely with no TTL. Decide full-response replay duration, tombstone lifetime, archival, legal retention and backup interaction before cleanup.

**Measured evidence:** cleaned test DB had 0 idempotency records and 0 BSON bytes; this does not estimate production size. Illustrative arithmetic and a sizing plan are in `PHASE_1_2_REMAINING_GATES.md`.

## 5. Production MongoDB transaction/failover gate

**Decision/evidence owner:** deployment owner supplies the Mongo version/provider, standalone/replica-set/sharded topology, members/regions, transaction support and limits, write/read concerns, journaling/retry settings, failover/unknown-commit test results, and backup/restore evidence. No production connection is needed from engineering.

**Engineering recommendation:** keep transactional refresh, idempotency and audit-dependent mutations disabled/fail-closed where those semantics are unsupported. Do not silently fall back to separate writes. If the target cannot transact, approve and test an atomic alternative before use.

## 6. External API client inventory

**Decision/evidence owner:** product/integration owner provides every deployed caller of inventory adjustment, purchase create/update/receipt, payment, transfer, ready and dispatch: web/mobile versions, Excel macros, scripts, scheduled jobs, third parties, and API gateway identities/logs.

**Options:** enforce immediately; use a staged compatibility period; or defer release enforcement until each owner reports key support. **Recommendation:** staged rollout after inventory, with stable client-generated keys, observability for missing keys, owner communication and a dated enforcement gate. Do not silently exempt an unknown caller or deploy a breaking header requirement without checking gateway traffic.

## Owner decisions requested

1. Approve/revise the department field-visibility matrix and answer its listed field questions.
2. Select failed-login/security-event retention, monitored sink and outage behavior.
3. Identify the existing email provider/key service if any; approve retry, dead-letter and retention responsibilities. No new paid service is assumed.
4. Choose idempotency full-result and tombstone retention semantics; TTL remains disabled.
5. Assign deployment owner to provide production topology/failover evidence.
6. Assign integration owner to provide the deployed API-client inventory before release enforcement.
