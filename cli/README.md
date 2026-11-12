# Ekavo command line

Run any Ekavo tool from a terminal or a script. It uses only the Python standard library.

```
pipx install ./cli          # or: pip install ./cli
ekavo login --url https://your-api-address
ekavo tools --search draft
ekavo describe library.create_draft
ekavo call plan.limits
ekavo call library.create_draft --arg platform=LinkedIn --arg content="Hello" --dry-run
ekavo logout
```

## Signing in

`ekavo login` shows a short code and opens your browser. In the browser, where you are already signed in to Ekavo, check the name of the computer,
choose the workspace and what it may do, and say yes. Nothing is typed in the terminal, and no password or key is ever pasted.

The sign-in lasts 90 days, acts as you with your role (so it can never do more than you can), and shows in **Settings, Connections** where you can
revoke it at once. It can approve a person's decision only if you tick that box when you say yes. It can never manage keys, people or settings.

The token is kept in `~/.config/ekavo/credentials.json` (on Windows `%APPDATA%\ekavo`), private to you. For a build machine, set `EKAVO_API_URL` and
`EKAVO_TOKEN` instead, with a token from a sign-in or a workspace key.

## Running tools

`ekavo call <tool>` takes inputs as `--arg key=value` (a value that looks like JSON is read as JSON) or `--json '{...}'`.

- `--dry-run` shows the price and does nothing.
- A call over 20 credits asks first: repeat it with `--confirmed-credits N`.
- `--idempotency-key K` makes a repeat safe: it is made once.
- `--output-json` prints the whole answer for scripts.
- A call that makes something is never repeated on its own after a failure. A read is.

Exit codes: 0 done, 1 refused or failed, 2 usage, 3 needs your yes or a confirmed price, 4 not signed in.
