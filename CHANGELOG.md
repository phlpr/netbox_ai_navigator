# Changelog

## Unreleased

- Require HTTPS for remote model providers unless insecure HTTP is explicitly enabled, reject redirects, and bound
  provider response bodies.
- Rate-limit chat requests per authenticated user through the configured Django cache.
- Filter credential-bearing nested serializer fields and reject them in proposed writes.
- Add an explicit `tools.include_custom_fields` opt-in while retaining recursive credential-name filtering.
- Prevent documentation indexing through symlinks that escape an indexed directory.
- Add a private vulnerability-reporting policy, deployment security guidance, pinned security CI, and dependency
  update automation.

## 0.1.0 - Unreleased

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
