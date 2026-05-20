# ADR-054: Layer-2 OAuth token storage — AWS KMS envelope encryption

- **Status**: Accepted
- **Date**: 2026-05-21
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs)
- **Related**: ADR-050 (credential model — this specifies its at-rest mechanism), ADR-053 (WorkOS Layer-1), ADR-027 (Lightsail / AWS), ADR-030 (R2 backup), ADR-046 (boto3 already a dependency), ADR-024/049 (Terraform module set)

## Context

ADR-050 stores per-user **downstream** (Layer-2) OAuth tokens (GitHub / Slack /
Google refresh + access tokens) so recurring jobs can run unattended. These must
be encrypted at rest. The $5 single Lightsail VPS has **no on-box KMS**, so the
"where does the master key live" problem must be solved.

Offloading storage entirely (WorkOS Pipes / Nango — store nothing) was evaluated
and rejected: Pipes pricing is undocumented and WorkOS's connection-priced
products run $125/connection/mo (budget risk for a $5/mo posture), and a
self-built KMS encryption layer is a stronger backend/infra portfolio signal
than adopting a managed token-vault SaaS.

## Decision

**App-level envelope encryption with AWS KMS.**

- A KMS **Customer Managed Key (CMK)** in the same account/region as Lightsail
  (Tokyo, ap-northeast-1).
- **Per-write data key**: `GenerateDataKey` → encrypt the token locally with the
  plaintext data key (AES-GCM) → persist `{ciphertext, KMS-wrapped data key,
  nonce}` in Postgres → discard the plaintext data key. On read: KMS `Decrypt`
  the wrapped data key → decrypt the token locally.
- CMK key material **never leaves KMS**.
- VPS → KMS auth: an IAM user access key scoped to `kms:GenerateDataKey` +
  `kms:Decrypt` on that one CMK, stored in `.env` (0600). (Lightsail has no IAM
  instance role, so an access key is required.)

## Honest caveat (single-VPS reality)

An attacker who owns the box also gets the IAM key and can call KMS during their
access window — this is **not** compromise-proof while live. But KMS is strictly
better than a static master key in `.env`:

| | master key in `.env` | AWS KMS envelope |
|---|---|---|
| Key material on disk | yes | no |
| Compromise type | one-shot **permanent** exfiltration | only during the live access window |
| Revocation | rotate every secret | disable key / rotate IAM creds instantly |
| Audit | none | CloudTrail logs every `Decrypt` |
| Rotation | manual | automatic annual CMK rotation |

Blast radius is further bounded because the tokens themselves are scoped and
user-revocable (ADR-050).

## Cost

≈ **$1/mo** per CMK + ~$0.03 / 10k requests — negligible, known, bounded.

## Alternatives considered

- **Master key in `.env`** — rejected (co-located; one-shot permanent leak).
- **`pgcrypto`** — rejected (key must still be supplied from somewhere; same
  co-location, without the audit/revocation/rotation benefits of KMS).
- **Offload to WorkOS Pipes / Nango** — rejected on budget uncertainty +
  weaker infra-portfolio signal; revisit if Pipes confirms a free/cheap tier.

## Consequences

- Terraform module set gains a KMS CMK + least-privilege IAM policy; works for
  both the VPS target and the Fargate design artifact (ADR-049).
- `boto3` (already present, ADR-046) used for KMS calls.
- `pg_dump → R2` backups (ADR-030) carry token columns as ciphertext + wrapped
  data keys — safe; useless without KMS access.
- Per-write data keys make CMK rotation transparent.
- Remaining open nodes: per-user rate limit (ADR-042 revision), public
  abuse/cost containment, LLM token accounting (ADR-052).
