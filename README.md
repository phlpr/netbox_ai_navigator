# NetBox AI Navigator

Explore and understand NetBox data with an AI model of your choice. NetBox AI Navigator is a standalone, read-only
NetBox plugin whose local tools execute under the permissions of the currently authenticated user.

> [!WARNING]
> NetBox data returned by tools is sent to the configured model provider. Use an internal endpoint such as Ollama or
> vLLM when data must not leave your environment.

## Status

Version 0.1 targets NetBox 4.6 and Python 3.12 or newer. It provides:

- a localized, resizable global chat window with context from the currently visible NetBox page;
- an OpenAI Chat Completions provider and a deployment-specific Custom API Connector with function/tool calling;
- a bounded agent loop with at most five tool calls per request;
- four read-only tools: `list_object_types`, `describe_object_type`, `query_objects`, and `get_object`;
- NetBox FilterSet semantics and NetBox REST serializers;
- current-user RBAC via `queryset.restrict(user, "view")` before filtering or lookup;
- an explicit model and output-field allowlist;
- session-scoped conversation history without plugin database models or migrations.

The OpenAI-compatible provider requires native tool calling. The Custom API Connector adapts a deployment-specific
backend to the same internal agent contract.

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
 │    ├── OpenAICompatibleProvider
 │    └── Custom API Connector
 │
 └── ToolProvider
      └── LocalCurrentUserProvider
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
        # Empty means all active, authenticated users.
        "allowed_groups": [],
        "model": {
            "provider": "openai_compatible",
            "base_url": "http://ollama:11434/v1",
            "api_key": os.getenv("NETBOX_AI_NAVIGATOR_API_KEY"),
            "model": "qwen3",
            "timeout": 60,
            "temperature": 0.1,
            "max_tokens": 1200,
            "max_response_chars": 20000,
        },
        "tools": {
            "provider": "local_current_user",
            "max_results": 50,
            "max_output_chars": 50000,
            "timeout": 30,
            "allowed_object_types": [
                "dcim.site",
                "dcim.location",
                "dcim.rack",
                "dcim.device",
                "dcim.interface",
                "ipam.vrf",
                "ipam.prefix",
                "ipam.ipaddress",
                "ipam.vlan",
                "circuits.provider",
                "circuits.circuit",
                "virtualization.cluster",
                "virtualization.virtualmachine",
            ],
        },
        "agent": {
            "max_tool_calls": 5,
            "max_history_messages": 20,
            "max_message_chars": 12000,
        },
    }
}
```

Collect static assets and restart NetBox:

```bash
python /opt/netbox/netbox/manage.py collectstatic --no-input
sudo systemctl restart netbox netbox-rq
```

The plugin has no database migrations.

### Custom API Connector

An optional, deployment-specific Custom API Connector is available for backends that do not expose an
OpenAI-compatible interface. Its vendor-specific endpoints and credentials are intentionally not documented in this
repository. Connector credentials remain server-side and are never returned to the browser.

## Permissions and security model

Every object query follows this order:

```text
validate object type
  → enforce code and administrator allowlists
  → queryset.restrict(current_user, "view")
  → apply registered NetBox FilterSet
  → validate ordering and enforce a hard limit
  → serialize only explicitly safe fields
```

For object lookup, RBAC restriction is applied before the primary-key filter. An unauthorized object therefore looks
identical to a nonexistent object. The browser never receives the configured provider credentials, and neither the
NetBox session nor CSRF token is sent to the model provider.

Request prompts, tool results, and model answers are not logged by the plugin. Technical metadata such as username,
duration, model name, tool count, and status is logged. Chat history is stored under a random, NetBox-session-specific
browser key so it survives page navigation and reloads. Resetting the conversation or starting a new login clears the
visible history.

`allowed_groups` contains NetBox group names. An empty list enables the assistant for every active, authenticated user.
Setting `enabled` to `False` removes the UI and denies the endpoint.

## Development and tests

Run formatting and lint checks with Ruff:

```bash
ruff format --check netbox_ai_navigator
ruff check netbox_ai_navigator testing_configuration.py pyproject.toml
```

Run the plugin test suite from a NetBox 4.6 source checkout. `testing_configuration.py` adds this plugin to NetBox's
standard test configuration:

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
