# Working SOP

## Purpose

This document defines the operating discipline for planning, documenting, and implementing DYC Comm.

The goal is to reduce avoidable errors by requiring explicit verification before new facts, account details, policy assumptions, or implementation decisions are treated as settled.

## Core Rule

Do not invent facts.

If a detail is not confirmed by:

* the user
* the repository
* the live environment
* provider documentation
* observed system behavior

then it must be treated as unverified.

## Verification Workflow

Before recording a decision in docs or code:

1. identify which details are facts versus assumptions
2. verify each fact against a source of truth
3. mark unresolved items explicitly
4. prefer conservative defaults when a decision cannot safely be inferred

## Source Of Truth Order

Use sources in this order:

1. direct user-provided facts
2. repository files already committed or intentionally added during the session
3. local environment evidence such as config, file contents, or command output
4. official provider documentation for Microsoft, Google, Azure, and other external systems
5. observed runtime behavior from tests or live integration work

If sources conflict, stop and call out the conflict instead of silently choosing one.

## Required Checks

### Account And Identity Data

For mailbox addresses, domains, folder names, and tenant-specific settings:

* copy exact values from the user or verified system output
* do not normalize or "correct" values unless explicitly confirmed
* flag likely typos as unverified instead of silently rewriting them

### Product And Policy Decisions

For docs, specs, and architecture notes:

* distinguish confirmed decisions from recommendations
* label open questions clearly
* do not promote tentative ideas into requirements without confirmation

### Implementation

Before writing integration logic:

* inspect the current codebase and schema
* verify provider semantics from official docs when behavior may differ by platform
* prefer the smallest safe implementation over speculative abstraction

### Before Declaring Completion

Before reporting work as done:

* review the exact diff
* verify changed file paths and key wording
* state any remaining uncertainty plainly

## Default Safety Behavior

When something is unclear:

* keep the item in `Inbox`
* keep the policy conservative
* choose review mode over automatic action
* ask for confirmation when the risk of guessing is non-trivial

## Oversight

External review is encouraged for high-stakes decisions, especially:

* account inventory
* mailbox move policy
* provider integration details
* production deployment choices

If an external reviewer such as Claude Code is used, treat it as an additional review layer, not a replacement for source verification.

## Practical Session Checklist

For each substantial task:

1. restate the task in operational terms
2. inspect the relevant files or environment first
3. identify facts that must be verified
4. make edits only after those checks
5. review the resulting diff
6. report what is confirmed, what changed, and what remains uncertain
