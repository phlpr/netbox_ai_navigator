# Changelog

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
