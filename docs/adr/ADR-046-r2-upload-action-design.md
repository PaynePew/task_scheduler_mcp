# ADR-046: `r2_upload` Action — Cloudflare R2 as Artifact Storage

- **Status**: Accepted
- **Date**: 2026-05-19
- **Deciders**: PaynePew
- **Related**: ADR-032 (secrets convention), ADR-033 (inter-handler data flow), #104

## Context

The task scheduler needs durable artifact storage for workflow outputs: nightly database
dumps, scheduled reports, digest results. W3 introduced a nightly `pg_dump → R2` cron
implemented as a shell script. W4 promotes this to a first-class typed action handler,
enabling composable declarative workflows:

```
schedule_daily_report → r2_upload → email_link
nightly_pg_dump       → r2_upload
github_digest         → r2_upload → slack_post
```

The design question is which storage backend to support and how to structure the upload
contract.

## Decision

**Use Cloudflare R2 via the S3-compatible API.**

The handler (`app/actions/r2_upload`) uploads content to R2 using `boto3` pointed at the
R2 endpoint (`https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com`). Credentials are read
from environment variables (ADR-032). The handler returns `{bucket, key, etag, size_bytes}`
to `JobRun.result` for downstream handlers to consume.

## Rationale

### R2 over S3

| Dimension | Cloudflare R2 | AWS S3 |
|-----------|--------------|--------|
| Egress cost | $0 (zero egress fee) | $0.09/GB after first 100 GB |
| Free tier | 10 GB storage, 1M Class A ops, 10M Class B ops/month | 5 GB for 12 months only |
| API compatibility | S3-compatible | Native |
| Migration path | Drop-in: change endpoint URL + credentials | — |
| Vendor coupling | Low — S3 API is the standard | High |

At our scale (nightly dumps, digest reports, small artifacts), the egress savings are
the primary driver. Cloudflare's free tier covers current and projected near-term usage
at zero cost. The S3-compatible API means frictionless migration to AWS S3, MinIO, or
any other S3-compatible provider — just change the endpoint URL.

### Promotion from shell script to typed action

W3's nightly `pg_dump → R2` cron was a shell script: effective but opaque to the
scheduler (no structured result, no error classification, no chain integration). Promoting
it to a typed action gives:

- **Structured result**: `{bucket, key, etag, size_bytes}` in `JobRun.result`
- **Error classification**: 4xx → DLQ, 5xx → retry (ADR-032 pattern)
- **Chain integration**: downstream handlers (e.g., `email_send`, `slack_post`) can
  consume the upload result via `from_run_id`
- **Observability**: the scheduler's `tasks://recent-results` surface shows upload status

The R2 bucket is the same; only the caller changes.

## Multipart threshold rationale

`TransferConfig(multipart_threshold=100 * 1024 * 1024)` (100 MB) is set explicitly:

- Below 100 MB: single PUT. Most artifacts (reports, digests, config dumps) are well
  under 100 MB. Single PUT is simpler and faster for small objects.
- At or above 100 MB: boto3's transfer manager automatically uses multipart upload,
  which provides parallel part uploads and resume capability on transient failures.

The boto3 default threshold is 8 MB, which is too aggressive — it adds multipart
overhead for typical digest-sized payloads (10–500 KB). 100 MB matches R2's practical
recommendations and our artifact size profile.

**Future tuning**: the threshold is defined as `_MULTIPART_THRESHOLD` in the module.
If artifact sizes grow (e.g., large database dumps), the threshold can be lowered to
improve reliability. If smaller artifacts dominate, raising it reduces API call count.
The `TransferConfig` object can also expose `max_concurrency` and `multipart_chunksize`
as future tuning parameters without changing the handler interface.

## Idempotency model

Idempotency is **caller-driven**, not handler-enforced:

- The handler uploads to whatever `bucket_path` the caller specifies.
- If the same `bucket_path` is used with the same content, R2 silently overwrites the
  object — semantically a no-op.
- If the caller wants true idempotency (no duplicate storage costs), they should embed
  a content hash in the path: `reports/{date}/{sha256}.json`.
- The handler does not generate or enforce content-addressed paths — this is the
  caller's responsibility and avoids surprising path rewriting.

**Rationale**: Handler-enforced content addressing would require hashing the content
before upload (adding latency and complexity) and would silently change the path the
caller specified. Caller-driven idempotency keeps the contract simple and predictable.

## Error classification

| Condition | Classification | Rationale |
|-----------|---------------|-----------|
| 4xx auth (401, 403) | DLQ (not retryable) | Credentials invalid; operator action needed |
| 4xx bad request (400) | DLQ (not retryable) | Invalid bucket/key; code or config error |
| 4xx not found (404) | DLQ (not retryable) | Bucket does not exist; operator action needed |
| 5xx server error | Retry | Transient R2 / infrastructure error |
| SSL error | Retry | Transient TLS failure; often self-heals |
| Network / OS error | Retry | Transient connectivity; often self-heals |
| Missing env vars | DLQ (not retryable) | Configuration error; operator action needed |

SQS redelivers retryable failures up to `max_receive_count` (3) times before routing
to the DLQ. Non-retryable failures are immediately sent to DLQ via `ok=False, retryable=False`.

## Chain data plane (ADR-033) integration

When `from_run_id` is set:
- **Ok variant**: `json.dumps(upstream.data)` is uploaded. The upstream result is
  preserved as valid JSON, consumable by downstream systems.
- **InvalidJson variant**: the raw bytes are uploaded as-is (best-effort pass-through).
- **UpstreamError / NoResult**: handler returns not-retryable failure — there is nothing
  meaningful to upload.

This matches the sink-to-storage pattern: the upload handler is a terminal sink that
persists upstream results for later retrieval.

## Secrets model

R2 credentials follow ADR-032:

- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` are set
  in the deployment environment.
- They are never stored in `action_params` or the database.
- `bucket_path` and `content` fields in `action_params` may use `${VAR}` substitution
  for whitelisted variables (e.g., `${R2_BUCKET}` if added to the whitelist), but R2
  credentials themselves are always read directly from `os.environ`.

## Alternatives considered

### AWS S3

Rejected: egress fees ($0.09/GB) would apply at scale. R2's zero-egress model is
strictly better for artifact storage where objects are frequently read (e.g., email
links, downstream processing). S3-compatible API means migration is a one-line change
if requirements change.

### MinIO (self-hosted)

Rejected: requires infrastructure management (Docker, storage provisioning, backup).
R2 is fully managed with a generous free tier. MinIO remains viable for air-gapped
deployments — the S3-compatible handler works unchanged.

### Handler-enforced content addressing

Rejected: would silently rewrite `bucket_path` values, surprising callers. Content
hashing adds latency for every upload. Caller-driven idempotency is simpler and more
transparent.

### Single `put_object` for all uploads

Rejected: `put_object` is limited to 5 GB and does not support parallel uploads.
Using boto3's transfer manager with `upload_fileobj` + `TransferConfig` provides
automatic multipart for large objects at no extra code complexity.

## Consequences

**Positive:**
- Zero egress fees; free tier covers current scale.
- S3-compatible: no vendor lock-in; one-line migration to AWS S3 or MinIO.
- Typed result enables chain integration (`from_run_id` → downstream handlers).
- Error classification aligns with ADR-032 conventions (consistent with other handlers).
- W3's nightly `pg_dump → R2` shell script can be replaced with a declarative chain.

**Negative:**
- R2 is Cloudflare-hosted; requires internet access (not suitable for air-gapped envs).
- Idempotency is caller-driven — operators must use content-hash paths if they want
  true idempotency guarantees.
- `boto3` is a large dependency (~10 MB); already included for S3 usage elsewhere in W3.
