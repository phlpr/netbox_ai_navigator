# NetBox AI Navigator

Explore, navigate, and safely operate NetBox with an AI model of your choice. NetBox AI Navigator is a standalone
NetBox plugin whose local tools execute under the permissions of the currently authenticated user. Mutations use a
separate two-phase approval workflow and are never executed directly by the model.

> [!WARNING]
> NetBox data returned by tools is sent to the configured model provider. Use an internal endpoint such as Ollama or
> vLLM when data must not leave your environment.

## Status

Version 0.3 targets NetBox 4.5.10 through 4.6.x and Python 3.12 or newer. It provides:

- a localized, resizable global chat window with context from the currently visible NetBox page;
- an OpenAI-compatible provider for Chat Completions and Responses;
- a bounded agent loop with at most ten tool calls per request;
- dynamic read tools for model discovery, schema inspection, filtering, and object lookup;
- local search across installed NetBox and plugin documentation;
- permission-verified automatic browser navigation to object, list, and global-search pages, including contextual
  follow-ups to one previously found object, native list-filter preservation, and an exact-object fallback for
  Navigator-only filters;
- validated create, update, and delete proposals with an explicit browser confirmation step;
- NetBox FilterSet semantics and NetBox REST serializers;
- current-user RBAC via `queryset.restrict(user, "view")` before filtering or lookup;
- dedicated `use_read` and future-ready `use_write` AI Navigator capabilities assignable to NetBox users or groups;
- dynamic discovery of supported NetBox core and plugin models, fields, and filters;
- non-configurable credential guards plus optional administrator exclusions;
- session-scoped conversation history without persisting normal chat messages in the NetBox database;
- a permission-protected, categorized audit log for rejected responses and validated write proposals.

The OpenAI-compatible provider requires native tool calling and supports both the Chat Completions and Responses tool
protocols.

## Compatibility

| Plugin Release | NetBox | Python |
|---|---|---|
| `0.3.x` | `4.5.10` to `4.6.x` (tested with `4.5.10` and `4.6.8`; CI uses `4.6.9`) | `3.12`, `3.13`, `3.14` |
| `0.2.x` | `4.5.10` to `4.6.x` (tested with `4.5.10` and `4.6.8`; CI uses `4.6.9`) | `3.12`, `3.13`, `3.14` |
| `0.1.x` | `4.5.10` to `4.6.x` (tested with `4.5.10` and `4.6.8`; CI uses `4.6.9`) | `3.12`, `3.13`, `3.14` |

## Architecture

```text
NetBox chat UI
      │
      ▼
Assistant endpoint
      │
      ▼
AgentRuntime
 ├── ModelProvider
 │    └── OpenAICompatibleProvider
 │
 └── ToolProvider
      └── LocalCurrentUserProvider
           ├── Dynamic NetBox model/schema discovery
           ├── Local documentation index
           ├── Verified navigation actions
           └── Confirmed REST API change proposals
```

The `ModelProvider` and `ToolProvider` interfaces isolate future MCP, Itential, or additional model integrations from
the UI and agent runtime.

## Installation

Install the package in the same Python environment as NetBox. For development:

```bash
source /opt/netbox/venv/bin/activate
pip install -e /path/to/netbox_ai_navigator
```

Add the plugin to NetBox's `configuration.py`:

```python
import os

PLUGINS = [
    "netbox_ai_navigator",
]

PLUGINS_CONFIG = {
    "netbox_ai_navigator": {
        "enabled": True,
        "model": {
            "provider": "openai_compatible",
            "protocol": "chat_completions",
            "base_url": "http://ollama:11434/v1",
            # Required only for a trusted non-loopback HTTP endpoint such as this container hostname.
            "allow_insecure_http": True,
            "api_key": os.getenv("NETBOX_AI_NAVIGATOR_API_KEY"),
            # Optional deployment-specific headers. Authorization and transport headers are reserved.
            "extra_headers": {},
            "model": "qwen3",
            "timeout": 60,
            "temperature": 0.1,
            "max_tokens": 1200,
            "max_response_chars": 20000,
            "max_http_response_bytes": 2000000,
        },
        "tools": {
            "provider": "local_current_user",
            "max_results": 50,
            "max_output_chars": 50000,
            "timeout": 30,
            # None discovers all models with a NetBox REST serializer,
            # registered FilterSet, and restrict()-capable manager.
            "allowed_object_types": None,
            # Optional additional deployment-specific restrictions.
            "excluded_object_types": [],
            "excluded_fields": [],
            # Opt in only when custom-field values may be disclosed to the model provider.
            "include_custom_fields": False,
            "documentation": {
                "enabled": True,
                "max_results": 5,
                "max_section_chars": 12000,
                "additional_roots": [],
            },
            "write": {
                "enabled": True,
                "approval_ttl": 600,
                # Also limits multi-object update proposals per request.
                "max_pending": 5,
            },
        },
        "agent": {
            "max_tool_calls": 10,
            "max_history_messages": 20,
            "max_message_chars": 12000,
            "requests_per_minute": 20,
        },
        "rejected_response_logs": {
            "enabled": True,
            # The oldest entries are removed after this limit is exceeded.
            "max_entries": 1000,
        },
    }
}
```

For a provider exposing the OpenAI Responses API, use the same provider with `protocol="responses"`. The configured
base URL must be the API root immediately before `/responses`; do not append the endpoint itself. Additional
deployment-routing headers can be supplied server-side without exposing them to the browser:

```python
"model": {
    "provider": "openai_compatible",
    "protocol": "responses",
    "base_url": "https://model.example/v1",
    "api_key": os.getenv("NETBOX_AI_NAVIGATOR_API_KEY"),
    "extra_headers": {"deployment-id": "example-deployment"},
    "model": "example-model",
    "temperature": None,
    "max_tokens": 2000,
}
```

Responses requests use `store=False`. During one bounded agent run, the required provider output items are replayed
with new tool results or refinement instructions so reasoning context is preserved without creating a persistent
provider-side conversation. Response-only lifecycle fields are removed for compatibility when those items become
request input.

Apply the database migrations, collect static assets, and restart NetBox:

```bash
python /opt/netbox/netbox/manage.py migrate
python /opt/netbox/netbox/manage.py collectstatic --no-input
sudo systemctl restart netbox netbox-rq
```

The migrations register the permission-only `AI Navigator` object type and create the AI response log table.
The permission anchor remains unmanaged and stores no rows.

## Permissions and security model

Every object query follows this order:

```text
validate object type
  → require a registered REST serializer, FilterSet, and restrict()-capable manager
  → enforce non-configurable credential guards and administrator exclusions
  → queryset.restrict(current_user, "view")
  → apply registered NetBox FilterSet
  → validate ordering and enforce a hard limit
  → remove write-only and blocked fields before serialization
```

With `allowed_object_types=None`, compatible models from NetBox core and installed plugins are discovered at runtime.
Set it to a list of `app_label.model_name` values to use an administrator allowlist instead. `excluded_object_types`
and `excluded_fields` can narrow either mode further. Built-in credential exclusions cannot be overridden through
configuration.

Custom fields are excluded by default because their schema and content are deployment-specific. Set
`tools.include_custom_fields=True` to expose them to read queries, custom-field filters, and validated write proposals.
The non-configurable credential-name guards still apply recursively inside `custom_fields`; entries whose names contain
terms such as `password`, `token`, or `secret` remain unavailable even when the opt-in is enabled.

Documentation search indexes `DOCS_ROOT`, documentation or README files shipped by installed plugins, and any paths
explicitly listed in `documentation.additional_roots`. Only local files are read; documentation search performs no
internet requests. Index only additional paths whose content may be disclosed to the configured model provider; never
point this setting at deployment configuration, credential stores, or private keys.

For object lookup, RBAC restriction is applied before the primary-key filter. An unauthorized object therefore looks
identical to a nonexistent object. The browser never receives the configured provider credentials, and neither the
NetBox session nor CSRF token is sent to the model provider.

Normal request prompts, tool results, and accepted model answers are not persisted by the plugin. Technical metadata
such as username, duration, model name, tool count, and status is written to the configured application log. Chat
history is stored under a random, NetBox-session-specific browser key so it survives page navigation and reloads.
Resetting the conversation or starting a new login clears the visible history.

When `rejected_response_logs.enabled=True`, a final model response replaced or rejected by a Navigator safety control
is stored separately with the last user request, user, reason, provider/model identifiers, original model response,
and the safe response returned to the browser. Validated write proposals are stored in the same audit log under the
`write` category; safety rejections use `rejected`. The list view can filter both category and reason. Raw tool results
and provider credentials are not added to these records. Text is bounded by `model.max_response_chars`, and only the
newest `rejected_response_logs.max_entries` records are retained. These records can contain NetBox data and should be
treated as sensitive audit data. Set `rejected_response_logs.enabled=False` if they must not be persisted.

Remote provider URLs require HTTPS. Loopback HTTP endpoints are accepted for local runtimes; other HTTP endpoints need
the explicit `model.allow_insecure_http=True` opt-in. Provider redirects are not followed, response bodies are bounded,
and chat requests are limited per authenticated user through NetBox's configured Django cache. Nested serializer data
is filtered recursively for credential-bearing field names before it reaches the model. Documentation indexing does not
follow symlinks outside an indexed directory.

Navigator access is assigned through **Admin → Object Permissions** using the `AI Navigator` object type and one of
the registered custom actions:

- `use_read` shows the Navigator and permits its current read-only chat tools.
- `use_write` implies read access and exposes validated create, update, and delete proposals. It does not replace the
  model-specific NetBox `add`, `change`, or `delete` permission.

Assign either action directly to users or to groups. A user with neither action does not receive the UI and gets HTTP
403 from the chat and reset endpoints. `enabled=False` remains a global kill switch and overrides both capabilities.
Normal NetBox object permissions continue to determine which individual objects the read tools may return.

AI response logs use their own `AI response log` object type. Superusers can open the log from the AI Navigator menu
automatically. For a non-superuser administrator, grant only the `view` action for this object type
through **Admin → Object Permissions**. The log model exposes no add, change, or delete UI actions and is not available
to the model's dynamic NetBox tools because it has no REST serializer or FilterSet.

### Confirmed changes

The model can stage one change or an atomic group of exact named-object updates per assistant request, up to
`tools.write.max_pending`. A proposal is validated with the model's registered NetBox REST serializer, but no object is
saved. The exact before/after preview is stored server-side in the current session and displayed with Confirm and
Cancel controls. Approval tokens are single-use and expire after ten minutes by default.

After confirmation, the plugin locks the target object and rechecks its ETag before dispatching the stored action
through the registered NetBox REST ViewSet. NetBox then rechecks the current user's normal object permissions,
serializer validation, and plugin-specific rules. Concurrent changes therefore invalidate stale proposals instead of
being overwritten; NetBox 4.6 additionally enforces the same ETag through its REST API. Successful changes use a fixed
AI Navigator changelog message. Credential-bearing object types and fields remain blocked from both reads and writes.

## Development and tests

Run formatting and lint checks with Ruff:

```bash
ruff format --check --exclude netbox_ai_navigator/migrations netbox_ai_navigator
ruff check --exclude netbox_ai_navigator/migrations netbox_ai_navigator testing_configuration.py pyproject.toml
```

Run the plugin test suite from a supported NetBox source checkout (4.5.10 through 4.6.x).
`testing_configuration.py` adds this plugin to NetBox's standard test configuration:

```bash
export PYTHONPATH=/path/to/netbox_ai_navigator:/path/to/netbox/netbox
export NETBOX_CONFIGURATION=testing_configuration
cd /path/to/netbox/netbox
python manage.py test netbox_ai_navigator.tests
```

The RBAC integration tests create two users with different `ObjectPermission` coverage and require the normal NetBox
PostgreSQL test database.

## License

[MIT](LICENSE)
