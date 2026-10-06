# GitLab Issue & MR Management Agent

AI agent for managing GitLab issues and merge requests, built with Agentic Star.

> **Category**: Cat 2 (domain-specific multi-step workflow)
> **Industry**: Common
> **Template ID**: CMN-C2-239

## Overview

Turns a natural-language request into a structured GitLab REST API v4 write operation - create an issue, open a merge request, or post a comment - through a deterministic five-step pipeline: validate, classify the intent, extract the fields, call the API, confirm. Callers can pass the request purely as text, or supply validated structured fields (project, title, labels, branches, comment target) alongside it for exact control. Every operation is a write, so anything the pipeline cannot resolve explicitly - the target project, a branch, a comment thread - is refused rather than guessed.

The shipped build performs no live GitLab operation: the API client runs on a deterministic, network-free stub transport that returns the documented GitLab response shapes, which makes the whole pipeline runnable and testable offline. Wiring a live transport (and a real access token) is a deployment-integration step with no business-logic change.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, start-up fails during
graph compile / start-up preflight rather than continuing in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

