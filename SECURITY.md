# Security Policy

## Supported version

Security fixes are currently applied to the latest revision of the `main` branch. Version 0.1 is an alpha release and
should be evaluated in a test environment before production use.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's **Security → Report a vulnerability** function for
this repository. Do not open a public issue containing credentials, private NetBox data, exploit details, or provider
configuration.

Include the affected revision, required permissions, reproduction steps, and impact where possible. Revoke any exposed
credential through its issuing system before sharing a redacted report.

## Deployment responsibilities

- Keep provider credentials in environment-backed NetBox configuration and out of Git.
- Use HTTPS for remote model providers. Plain HTTP requires an explicit opt-in and should be limited to trusted local
  networks.
- Treat all NetBox data returned by tools as data disclosed to the configured model provider.
- Grant the Navigator capabilities and normal NetBox object permissions according to least privilege.
- Review confirmed changes and NetBox's object changelog regularly.
