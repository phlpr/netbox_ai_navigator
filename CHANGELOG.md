# Changelog

## Unreleased

- Replace the close control with minimize/maximize controls, hide the launcher while open, and make the assistant
  window draggable within the visible NetBox interface.
- Add localized copy controls to copy-ready fenced response blocks, automatically recognize unfenced multi-line CSV,
  and prompt exact CSV/configuration output into those blocks.
- Deduplicate global-search identities, route plural follow-up updates through the atomic bulk proposal tool, and
  recognize concrete identifiers in change requests even without an explicit object-field noun. Reprompt once when
  an explicit change request stops after lookup without creating a validated proposal.
- Report safe Responses API incomplete reasons for output-token limits and content filtering.
- Add deterministic offset pagination to object queries so complete filtered lists can span multiple tool calls.

## 0.3.0 - 2026-08-27

- Add OpenAI Responses API support and validated deployment-specific headers to the general OpenAI-compatible
  provider while retaining Chat Completions compatibility.
- Remove the legacy provider integration and its server-side session lifecycle.
- Generalize rejected-response auditing into categorized AI response logs, classify validated write proposals, and
  add category and reason filters to the NetBox list view.

## 0.2.0 - 2026-08-27

- Validate small multi-object updates atomically and require a separate browser confirmation for every object.
- Add a server-side `has_contact` filter for contact-capable objects and report whether query results are truncated.
- Add a dedicated, permission-protected model for rejected AI responses with bounded retention, localized list and
  detail views, and audit fields for the user, request, rejected response, delivered response, reason, and model.

## 0.1.0 - 2026-08-25

- Require HTTPS for remote model providers unless insecure HTTP is explicitly enabled, reject redirects, and bound
  provider response bodies.
- Rate-limit chat requests per authenticated user through the configured Django cache.
- Filter credential-bearing nested serializer fields and reject them in proposed writes.
- Add an explicit `tools.include_custom_fields` opt-in while retaining recursive credential-name filtering.
- Prevent documentation indexing through symlinks that escape an indexed directory.
- Add a private vulnerability-reporting policy, deployment security guidance, pinned security CI, and dependency
  update automation.
- Add the standalone NetBox plugin and global chat interface.
- Add an OpenAI-compatible model provider with tool calling.
- Localize the chat controls with NetBox's active language, add resize/maximize controls, and use a robot launcher icon.
- Add the bounded agent runtime and current-user read-only tool provider.
- Enforce model, field, result, history, output, and response limits.
- Add RBAC, provider, agent, tool, and view tests.
- Add assignable `use_read` and future-ready `use_write` Navigator capabilities; hide and deny the assistant when
  neither action is granted.
- Support NetBox 4.5.10 through 4.6.x and Python 3.12 through 3.14.
