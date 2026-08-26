# Changelog

## Unreleased

- Validate small multi-object updates atomically and require a separate browser confirmation for every object.
- Add a server-side `has_contact` filter for contact-capable objects and report whether query results are truncated.

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
- Add a deployment-specific Custom API Connector using server-side credentials and temporary conversations.
- Reuse one connector conversation per NetBox login and clear it together with browser history on manual reset or
  logout.
- Localize the chat controls with NetBox's active language, add resize/maximize controls, and use a robot launcher icon.
- Add the bounded agent runtime and current-user read-only tool provider.
- Enforce model, field, result, history, output, and response limits.
- Add RBAC, provider, agent, tool, and view tests.
- Add assignable `use_read` and future-ready `use_write` Navigator capabilities; hide and deny the assistant when
  neither action is granted.
- Support NetBox 4.5.10 through 4.6.x and Python 3.12 through 3.14.
