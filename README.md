# TUL Menza MCP

Unofficial MCP server for **https://menza.tul.cz/**. Built for Hermes Agent, usable with other stdio MCP hosts. Python 3.10+; Linux is required for the process lock and isolated deployment.

## Features

- Menu, published minutky, current orders, account credit and consumed-meal history.
- Buy one selected meal within an explicit price cap; cancel within the refund window.
- Offer your own meal on burza after the refund cutoff, or withdraw an unsold offer.
- Historical spending and favourite dishes.
- Suggestions for lunch and dinner for up to 14 days: dinner at one fixed canteen, lunch canteen inferred from weekday history among the allowed lunch canteens, dishes ranked by exact past orders then overlapping dish-name words. The canteens default to the author's habits (dinner Harcov, lunch Harcov/Husova); to change them, add `MENZA_DINNER_CANTEEN` and `MENZA_LUNCH_CANTEENS` (comma-separated `harcov`, `husova`, `voronezska`, `komenskeho`) to the `env -i` line of the root-owned launcher `deploy/menza-mcp`. Counts and reasons are returned, not claims about dietary preferences. Suggestions never buy anything. Lunch includes soup by default; select `lunch_no_soup` explicitly for the other variant. Missing menus remain unavailable.
- Persistent burza watches with exact dish/section/canteen/date matching, a maximum price and expiry. A separate worker polls about every 20 seconds. Network delays and competing buyers mean neither instant purchase nor success is guaranteed. Inspect `list_watches` for outcomes.

This uses the same website as a person, not an official API. University/site changes can break it. Minutky are **direct-sale only**: listing the published menu does not guarantee stock or reserve them.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
.venv/bin/pytest
.venv/bin/menza-hermes serve
```

Fill in `MENZA_USERNAME` and `MENZA_PASSWORD` locally; never commit `.env`. For compatibility with the initial local setup, `username_email` and `password` are accepted in the operator file. `MENZA_ENV_FILE` chooses that file; `MENZA_STATE_DIR` chooses a private state directory. Neither is an MCP tool argument.

The Microsoft/Shibboleth flow only supports password sign-in. MFA, captcha, consent or unexpected authentication pages require human action; the worker stops rather than repeatedly submitting credentials. It does not bypass those checks. TLS stays verified. Passwords go only to the approved Microsoft HTTPS host; SAML responses go only to the expected university service endpoints.

## Tools

| Tool | Purpose |
|---|---|
| `menu`, `minutkas` | Read a canteen/day menu |
| `orders`, `balance` | Current own orders/burza offers and credit |
| `meal_history`, `meal_statistics` | Consumed meals and account transactions |
| `suggest_meals` | Read-only lunch/dinner plan with evidence |
| `buy_meal` | Confirmed single meal purchase with `max_price` |
| `cancel_meal` | Refund-window cancellation only |
| `sell_on_burza` | Offer an owned order; refund is **not guaranteed** |
| `withdraw_from_burza` | Restore your unsold offer |
| `watch_meal`, `list_watches`, `cancel_watch` | Authorize, inspect or stop a bounded watch |

Mutation tools require `confirmed=true` after the host obtains the user's approval. This is an intent safeguard, **not authentication**: a compromised or prompt-injected host can pass it itself. To bound the damage, the server enforces hard limits in its own state directory, covering both `buy_meal` and watcher purchases: at most **one lunch and one dinner per served date** (active orders made on the web, in the app or by an older server count too), at most **150 Kč per meal**, at most **3000 Kč in any rolling 14 days** (only purchases made through this server), plus at most 10 active watches. This still allows a confirmed 14-day plan. A purchase is counted before it is sent, so an uncertain outcome keeps using budget; it is released only when the site refuses the order outright or when `cancel_meal` confirms the refund in the balance. Change the limits only through the operator environment (`MENZA_MAX_PER_SLOT`, `MENZA_MAX_MEAL_CZK`, `MENZA_MAX_CZK_14D` in the root-owned launcher/service), never via tool arguments. `balance` reports current usage. The separate account protects the university password; these limits protect your credit.

Normal cancellation and burza sale use the same site endpoint. The tool checks the current deadline and refuses to silently turn a requested refund into a burza offer. A sale before the burza window is refused rather than cancelling the meal. A purchase POST is never blindly retried after a timeout. Watch state is committed before purchasing; if the worker crashes or cannot confirm the result, the watch becomes `needs_review` and does not purchase again automatically.

## Isolated deployment (recommended)

A university password also grants access outside the canteen. Do **not** put it in Hermes's `.env` when Hermes can execute shell commands.

Use three accounts:

- `menza`: locked system account, owns credentials and runtime state.
- Hermes's existing account: can invoke **only** the fixed MCP launcher as `menza`; cannot read credentials or modify the installed Python/code.
- A separate administrator: credentials/private SSH key unavailable to Hermes.

Remove blanket sudo/root-equivalent access from Hermes first, but only after independently verifying the administrator login. Merely adding a second user while retaining Hermes's unrestricted sudo provides no isolation. Check privileged groups, polkit permissions and existing elevated processes too. Do not forward your SSH agent to the agent host.

Suggested layout:

```text
/opt/menza-hermes/                    root-owned code + non-editable venv
/etc/menza-hermes/credentials.env     menza-owned, 0600; parent directory 0700
/var/lib/menza-hermes/state/          menza-owned, 0700; cookies/watch DB 0600
/usr/local/libexec/menza-mcp         root-owned, 0755 (deploy/menza-mcp)
```

Install the package with `/opt/menza-hermes/.venv/bin/pip install /opt/menza-hermes` (not editable). Install `deploy/menza-watcher.service` as a system service and enable it. It survives gateway restarts; authentication failures stop it for operator intervention. After upgrading, restart the watcher **and** the MCP host (e.g. the Hermes gateway): a stdio MCP server keeps running the code it started with until its host respawns it.

The sudoers entry must permit **no arguments** (the final `""` matters):

```sudoers
tomja ALL=(menza) NOPASSWD: /usr/local/libexec/menza-mcp ""
```

The launcher resets the environment, fixes its working directory and uses Python isolated mode. Do not allow arbitrary Python, shell, environment variables, extra arguments, writable code paths or command interpolation through sudo.

Hermes config (no password passed to Hermes):

```yaml
mcp_servers:
  menza:
    command: /usr/bin/sudo
    args: ["-n", "-u", "menza", "/usr/local/libexec/menza-mcp"]
    connect_timeout: 60
```

Verify from Hermes's account: credential and cookie reads fail; arbitrary sudo/root commands fail; extra launcher arguments fail; the MCP handshake and read tools still work. Verify `systemctl is-active menza-watcher` from the separate administrator.

Credentials and cookies are stored in plaintext protected by Unix permissions; root/physical access can read them. They are not encrypted at rest by this project. Never put tokens, session snapshots, real menus tied to user activity, or personal history into public fixtures. Tests use synthetic data and make no live purchases.

## References

[UtrataMenza](https://github.com/konerzajakub/UtrataMenza) and [JagerMenzer](https://github.com/konerzajakub/JagerMenzer) provided background on the site. This implementation uses its own parsers, authenticated transaction verification and bounded watcher.

MIT license.
