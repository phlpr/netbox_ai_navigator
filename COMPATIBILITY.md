# Compatibility

| NetBox AI Navigator | NetBox | Python |
|---|---|---|
| 0.4.1 | 4.5.10–4.7.x (CI tests 4.5.10, 4.6.10, and 4.7.1) | 3.12, 3.13, 3.14 |

The declared range is `min_version = "4.5.10"` through `max_version = "4.7.99"`.
CI tests every listed NetBox release with every listed Python version. Versions between these pinned releases
are covered by the declared range but are not individually verified by the matrix.

NetBox 4.7.0 was additionally checked locally with Python 3.14.7, Django 6.1, PostgreSQL 16, and Redis 7.
All 137 existing tests and 10 additional compatibility tests passed. No plugin schema migration was required.

NetBox 4.7.1 was checked locally with Python 3.12.13, 3.13.14, and 3.14.7, Django 6.1.1,
PostgreSQL 16.15, and Redis 7.4.10. All 152 plugin tests passed on each Python version, including five
additional regressions for case-insensitive name filters, negated multiselect custom fields, confirmed hierarchy
changes, VLAN translation serializer fields, and required write fields. No plugin runtime changes or schema
migration were needed. The 4.7 CI target is pinned to the 4.7.1 release commit; the declared bounds are unchanged.

## NetBox 4.7.1 upgrade note

NetBox 4.7.1 fixes the hierarchy cascade triggers that could be missing after restoring a NetBox 4.7.0 database
dump. The core upgrade reinstalls those triggers, but does not repair paths that have already become stale.
After upgrading, check a restored database from the NetBox checkout root with:

```bash
python netbox/manage.py rebuild_ltree_paths --check
```

If the check reports stale paths, follow NetBox's
[hierarchical path repair instructions](https://github.com/netbox-community/netbox/blob/v4.7.1/docs/administration/repairing-hierarchical-paths.md)
before relying on descendant filters. AI Navigator does not define its own `ltree` models, so it does not require
a plugin-specific trigger migration. See the [official release notes](https://github.com/netbox-community/netbox/releases/tag/v4.7.1).
