# ADR 002: Use an OpenAI-compatible backend boundary

Status: Accepted
Date: 2026-07-14

## Context

Ollama, vLLM-Metal, and MLX-LM expose similar but non-identical HTTP interfaces. Letting each backend render its own chat template would make request equality unverifiable.

## Decision

The future controller will use a small backend interface modeled on OpenAI chat completions, while storing the exact rendered prompt or canonical payload and raw usage telemetry. Backend-specific extensions live in adapters and cannot change strategy semantics.

## Consequences

The controller avoids a large agent framework and can replay request shapes. Some runtime-specific features require explicit adapter capability flags. Cross-server comparisons must disclose checkpoint, template, and format differences.
