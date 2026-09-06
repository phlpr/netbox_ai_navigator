# Compatibility

| NetBox AI Navigator | NetBox | Python |
|---|---|---|
| 0.4.1 | 4.5.10–4.7.x (CI tests 4.5.10, 4.6.10, and 4.7.0) | 3.12, 3.13, 3.14 |

The declared range is `min_version = "4.5.10"` through `max_version = "4.7.99"`.
CI tests every listed NetBox release with every listed Python version. Versions between these pinned releases
are covered by the declared range but are not individually verified by the matrix.

NetBox 4.7.0 was additionally checked locally with Python 3.14.7, Django 6.1, PostgreSQL 16, and Redis 7.
All 137 existing tests and 10 additional compatibility tests passed. No plugin schema migration was required.
