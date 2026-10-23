# Contributing to transmission_manage

Thanks for considering a contribution! These are guidelines, not hard rules;
use your best judgment and feel free to propose changes to this document.

## What belongs here

This project is a port of [qbit_manage](https://github.com/StuffAnthings/qbit_manage)
to Transmission. Changes are welcome when they:

- port another qbit_manage module, or follow an upstream change in one that is already ported;
- fix a place where the port behaves differently from upstream without Transmission forcing it;
- improve tests, documentation or support for another Transmission version.

Behaviour that upstream does not have does not go into the port. If you need
it, use the `pre_remove` hook (see the README), or keep it in a fork. A new
generic extension point can be proposed in an issue first.

## Licensing

The port is MIT-licensed, like qbit_manage. Code may only be ported from
MIT-compatible sources; please do not copy code or data (such as pattern lists)
from GPL-licensed projects. Name the upstream file you ported from in the module
docstring.

## Getting started

```bash
python -m pip install -e ".[dev]"
pip install pre-commit && pre-commit install
python -m pytest
```

## Style

- Follow the existing code: ruff (`ruff check`, `ruff format`) with the settings in `ruff.toml`.
- Keep module, function and config key names as in upstream where they apply.
- Add or update tests for every behaviour change; prefer porting the matching upstream test.
- Write code, comments, commit messages and issues in English.

## No secrets, no personal setups

Never commit tracker names, announce URLs, passkeys, API tokens, real paths or
other details of your own setup — not even as examples. Use placeholders such as
`tracker-one.example.org` or `/path/to/torrents`. The pre-commit hook and CI
refuse anything shaped like a tracker passkey.

## Pull requests

Open pull requests against the `dev` branch. CI must be green before review.
