# App Overview

This document provides an overview of the App including critical information and important considerations when applying it to your Nautobot environment.

!!! note
    Throughout this documentation, the terms "app" and "plugin" will be used interchangeably.

## Description

Remote Jobs introduces a second job execution system for Nautobot in which job code runs in isolated containers on remote workers, never inside Nautobot itself:

- Workers interact with Nautobot exclusively through the REST API using short-lived tokens scoped to the launching user — remote jobs run with the launcher's ObjectPermissions, closing the ORM-bypass hole of core Jobs.
- Workers deploy into named **execution zones**; devices map to zones through configurable membership rules (locations, roles, prefixes, dynamic groups). Jobs targeting a device automatically execute on a worker in that device's zone, with per-device fan-out, failover, and wait policies.
- Workers connect **outbound** to a control-plane gateway over TLS WebSocket speaking JSON-RPC 2.0. Nautobot never initiates connections into remote sites.
- Results, logs, and console output land in the core `JobResult`, `JobLogEntry`, and `JobConsoleEntry` models, so the existing Job Results UI, saved views, filters, live console, and cancel button work unchanged.

The app is one of four components in the [mono-repo](https://github.com/jtdub/nautobot-app-remote-jobs): the Nautobot app (this package), the WebSocket gateway, the worker agent, and the job SDK. The full architecture is described in the repository's `SPEC.md`.

## Audience (User Personas) - Who should use this App?

- **Network automation engineers** who need jobs to reach devices in isolated sites, DMZs, or partner networks that Nautobot cannot (and should not) reach directly.
- **Platform/security teams** that want job execution constrained by real object permissions and short-lived credentials instead of implicit database superuser access.
- **Job authors** who prefer packaging automation as versioned, digest-pinned container images with typed inputs over deploying Python source into the Nautobot worker.

## Authors and Maintainers

- James Williams (@jtdub)

## Nautobot Features Used

- Core `JobResult` / `JobLogEntry` / `JobConsoleEntry` models for results, logs, and live console.
- The generic approvals framework (`ApprovableModelMixin`) on runs and schedules.
- `Secret` / `SecretsGroup` definitions as the secrets directory (values resolve worker-side).
- Object permissions via per-run scoped `users.Token` credentials.
- Celery beat for the schedule dispatcher and lease reaper.
- `nautobot.core.events` lifecycle event publication.

### Models

The app adds these models: Job Definition, Execution Zone, Zone Membership Rule (with ordered zone failover), Worker, Worker Enrollment Token, Remote Job Run, Run Artifact, and Remote Job Schedule.
