# ADR-005: Cloud front door — ALB over API Gateway HTTP API

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: .doc/session/grilling-state.md Q5b
- **Related**: ADR-004 (ECS Fargate), ADR-006 (MCP transport)

## Context

The MCP Streamable HTTP transport uses Server-Sent Events (SSE) for long-lived streaming responses. The cloud front door must support persistent connections without imposing an idle timeout that would kill MCP request/response flows mid-stream.

## Decision

Use **AWS Application Load Balancer (ALB)** as the public entrypoint. ALB terminates TLS, applies path-based routing, and forwards to the mcp-server target group. Idle timeout defaults to 60s but is configurable up to 4000s; SSE keep-alive frames keep the connection alive within the configured window.

## Alternatives considered

- **API Gateway HTTP API** — cheapest, but the **30-second idle timeout is not configurable** and will sever MCP SSE responses that take longer. Full reasoning in `.doc/learn/aws-deep-dive.md` § 1.
- **API Gateway REST API** — same 30s idle limitation; more expensive than HTTP API.
- **NLB (Network Load Balancer)** — layer-4 only, no path routing, no TLS termination convenience; we'd need a sidecar.
- **CloudFront in front of ALB** — adds latency and edge complexity for no resume-narrative win at W3 scope.

## Consequences

- One ALB serves the mcp-server target group; W3 has a single Terraform `aws_lb` resource.
- W3 can layer ALB OIDC authentication (Cognito) for free, replacing the W1 trust-only `X-User-Id` header (see ADR-015).
- Cost: ALB ≈ $16/month minimum. Acceptable for portfolio.
- If a future bonus requires REST-API-style usage plans / API keys, we'd need to add API Gateway selectively — but not for the W1 surface.
