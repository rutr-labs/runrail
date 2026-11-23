# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull
request. Use **Report a vulnerability** on the repository's
[Security tab](https://github.com/rutr-labs/runrail/security), which opens a
private advisory only the maintainers can see, or email
therutr@outlook.com.

Say what you found, how to reproduce it, and what it lets someone do. You can
expect an answer within a week. Fixes are released as soon as they are ready,
and the reporter is credited in the advisory unless they would rather not be.

## Supported versions

Fixes go into the latest release. Upgrading to it is the way to get them:

```bash
pip install --upgrade runrail
```

## How RunRail is meant to be run

RunRail has no user accounts. Anyone who can reach its API can create a
workflow that runs commands on the machine it runs on, so what keeps it safe
is who can reach it:

- It listens on `127.0.0.1` by default, and `docker compose` publishes its
  port on `127.0.0.1` only.
- It refuses requests a page on another website makes from your browser, and
  answers only to loopback names, IP addresses, and the names you configure
  with `RUNRAIL_PUBLIC_URL` or `RUNRAIL_ALLOWED_HOSTS`.
- To reach it from another machine, put it behind a reverse proxy that
  handles sign-in. Do not expose it to a network you do not trust.
