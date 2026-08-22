<!--
  Describes the Entra / Azure / Microsoft-SSO capabilities scoped to
  the bot's identity: bot user account, programmatic MFA via TOTP,
  az CLI, and the workflow for logging into self-hosted
  Entra-protected web apps. None of these are auto-wired in claw-code
  by default — the operator must seal the $ENTRA_* envs into
  openclaw-secrets to activate them.
-->

---

# Entra / Azure — bot user, browser MFA, az CLI

## ⚠️ READ THIS FIRST — anti-delegation rules

When a **dedicated Microsoft Entra ID bot user account** is wired
into this pod via the `$ENTRA_*` envs (see below), Microsoft logins
(both `az login` and any web-app SSO) are **YOUR JOB to drive
autonomously** using the **browser plugin** that's part of your
default toolset.

**ABSOLUTELY DO NOT:**

- Print the device-code URL + code in chat and ask the user to
  "please open the URL and enter the code". The user is not the bot
  account. Asking the user to do the login is a workflow bug, not a
  feature.
- Ask the user for the username, password, TOTP code, tenant, or any
  Microsoft credentials. They are all in your env (`$ENTRA_*`); the
  TOTP helper script generates codes on demand.
- Say things like "Please authenticate at <URL>" or "Let me know
  once you've signed in". The user will be confused — they expect
  you to do the login.
- Wait for the user to confirm anything before doing the browser
  dance. The whole point of having `$ENTRA_USERNAME / PASSWORD /
  TOTP_SEED` and `entra-totp` is that you can complete the login
  end-to-end with zero user interaction.

**INSTEAD:** use the **browser plugin** (your default toolset
exposes browser navigate / type / click / screenshot tools) to drive
the Microsoft login form yourself, the same way a human would but
faster.

## What is set up for you (env, helpers, persistence)

| Resource                  | What                                                                  |
|---------------------------|-----------------------------------------------------------------------|
| `$ENTRA_TENANT_ID`        | tenant GUID or domain (`<tenant>.onmicrosoft.com`)                    |
| `$ENTRA_USERNAME`         | bot UPN (e.g. `bot@<tenant>.onmicrosoft.com`)                         |
| `$ENTRA_PASSWORD`         | bot password                                                          |
| `$ENTRA_TOTP_SEED`        | TOTP shared secret (base32). **Never echo, never print, never paste into chat.** |
| `entra-totp` (shell cmd)  | reads `$ENTRA_TOTP_SEED`, prints current 6-digit TOTP code on stdout  |
| `az` (shell cmd)          | Azure CLI. Token cache lives at `~/.azure/`, PVC-backed, persistent across pod restarts |
| `~/.azure/`               | already-mounted PVC subPath; surviving cache here means after one successful login the bot stays signed in for ~90 days without re-doing the dance |

If any of `$ENTRA_TENANT_ID / USERNAME / PASSWORD / TOTP_SEED` is
unset, surface that to the user and ask them to seal it — don't
fall back to interactive flows.

## Workflow — `az login` from cold (no cache)

1. Always start with `az account show 2>/dev/null` (or
   `--output none`). If it returns 0 with the tenant info, you are
   already signed in — **stop immediately, do not re-login**, go
   straight to whatever the user actually asked for. Re-login when
   already authenticated is wasted work and surfaces a Microsoft
   "we noticed a recent sign-in" warning to the user.

2. If `az account show` errors with *"Please run 'az login' to setup
   account"*, run in your shell:
   ```
   az login --use-device-code --tenant "$ENTRA_TENANT_ID"
   ```
   It will block, printing a line like
   *"To sign in, use a web browser to open the page
   https://microsoft.com/devicelogin and enter the code XXXX-XXXX
   to authenticate."*

3. **Extract URL and code from the output. Do NOT print them in chat
   for the user.** Send them to the browser plugin instead.

4. Drive the browser plugin through the full dance — exact tool names
   vary by your runtime, but the conceptual sequence is:

   a. **Navigate** to `https://microsoft.com/devicelogin` (or
      whatever URL `az` printed).
   b. **Find the code field** (snapshot or query the page; the field
      is usually an `<input>` with a placeholder like "Code" or
      labelled "Enter code").
   c. **Type** the device code into it. Click **Next**.
   d. Microsoft account picker page — if a list of recent accounts
      is shown, click **"Use another account"** (the bot account is
      typically not in the list). On the resulting input field type
      `$ENTRA_USERNAME` (the value, not the literal string). Click
      **Next**.
   e. Password page — type `$ENTRA_PASSWORD` (the value). Click
      **Sign in**.
   f. MFA page expects a 6-digit TOTP code. Run `entra-totp` in a
      shell, get the 6-digit response, type it into the verification
      code field, click **Verify**. If Microsoft says "invalid code",
      wait ~30 seconds, run `entra-totp` again (it produces a new
      code each 30s window) and retry.
   g. **"Stay signed in?"** prompt — click **No**. Always No.
      Persisting the session here would tie the login to the
      container as a "trusted device", which we don't want.
   h. Microsoft confirms "You may close this window".

5. The `az login` shell process that was blocking exits 0 and
   populates `~/.azure/msal_token_cache.json`. The refresh token
   inside has the MFA claim and is good for ~90 days. From here on
   `az account show` and any other `az` command works without a
   re-login until the cache expires.

## Workflow — self-hosted Entra-protected web apps

Two sub-flavours depending on how the app calls MSAL.js — figure out
which one you're on by `openclaw browser snapshot` after the Sign-In
click and look at where the URL change happens.

### Variant 1 — `loginRedirect()` (full-page navigation, simple case)

1. **Navigate** the browser plugin to the app's URL.
2. Click the app's Sign-In control. The whole page navigates to
   `login.microsoftonline.com/<tenant>/oauth2/...` — same tab, no
   popup spawned.
3. Account picker (if shown) → **"Use another account"** →
   `$ENTRA_USERNAME`.
4. Password → `$ENTRA_PASSWORD`.
5. MFA → `entra-totp` → enter code.
6. "Stay signed in?" → **No**.
7. Microsoft redirects back to the app's `/redirect`-uri, the app's
   `handleRedirectPromise()` callback fires and the session cookie /
   localStorage entry is written. Subsequent navigations within the
   pod's lifetime reuse it.

### Variant 2 — `loginPopup()` (separate window, automation-trickier)

If the app calls `msalInstance.loginPopup()` instead of
`loginRedirect()`, clicking Sign-In does **not** navigate the
current tab. Instead it `window.open()`s a popup tab/window
containing the Microsoft login form. The auth result comes back via
`postMessage` from the popup to the parent window — meaning you
**must** drive the login inside the popup tab, never by navigating
the parent tab directly to `login.microsoftonline.com` (that breaks
the postMessage channel and the parent app never learns you signed
in).

Signs you're on Variant 2: after clicking Sign-In, the parent tab
URL doesn't change, but `openclaw browser tabs` shows a new entry
with `login.microsoftonline.com/.../authorize?...` in the URL.

Workflow:

1. **Navigate** the browser plugin to the app's URL.
2. `openclaw browser snapshot` (parent tab) → find the Sign-In ref.
3. `openclaw browser click <ref>` → triggers `window.open()`.
4. **Immediately** run `openclaw browser tabs` (no `--target-id`,
   that flag scopes to one tab — we want the full list). You should
   now see two entries: the original app tab plus a fresh entry with
   URL matching `https://login.microsoftonline.com/...`. Capture the
   new target id (the long hex string at end of each tab line).
5. From here on every browser command takes `--target-id <popup-id>`
   so it acts in the popup, not the parent. E.g.
   `openclaw browser snapshot --target-id <popup-id>`,
   `openclaw browser type <ref> "$ENTRA_USERNAME" --target-id <popup-id>`.
6. Run the standard username → password → TOTP → "Stay signed in?
   No" sequence inside the popup, all calls scoped to its target-id.
7. After the final "Stay signed in? No" click, Microsoft's success
   page calls `window.close()` on the popup. The popup target
   disappears from `openclaw browser tabs`. **Do not panic / do not
   try to close it yourself** — that breaks the postMessage which
   notifies the parent.
8. Back in the parent tab — its login state is now authenticated
   (MSAL stored the tokens in sessionStorage / localStorage). Take
   a screenshot or snapshot of the parent to verify the app shows
   the signed-in UI; continue with whatever the user actually asked
   for.

#### Anti-patterns for Variant 2

- **DO NOT** click Sign-In and then `openclaw browser navigate
  https://login.microsoftonline.com/...` in the parent tab. That's a
  manual workaround that bypasses MSAL's postMessage callback — the
  parent app will never see the login complete.
- **DO NOT** keep using the parent tab's target-id after the popup
  opens. The login form is in the popup, not the parent. Snapshots /
  types against the parent target-id will find no form fields and
  the model will go in circles.
- **DO NOT** close the popup manually before the success redirect.
  The popup's own success page closes itself and that's what triggers
  the parent-app's session activation; an early close orphans the
  flow.

If you're not sure which variant the app uses, run
`openclaw browser tabs` *before* clicking Sign-In, click Sign-In,
run `openclaw browser tabs` again — if the count went up by one,
it's popup (Variant 2). If parent tab's URL changed but count
stayed equal, it's redirect (Variant 1).

## Why the browser-plugin-as-bot pattern is correct

The TOTP seed + username + password in your env are the *bot's*
credentials, not the human user's. The user has their own MFA on
their own Entra account, separate from the bot's. When you do
`az login --use-device-code`, the URL that comes out is for the
**identity that completes the login**, and the only identity with
the bot's password and TOTP seed is **you, via the browser plugin**.
Asking the human user to enter the code would either:

- Have them sign in as **themselves**, which means `az` would
  authenticate as the human, not the bot — wrong identity, wrong
  RBAC, wrong test surface.
- Or have them manually type the bot's username + password + TOTP
  from your env into Microsoft's form, which exposes the secrets in
  their browser history and is the security failure mode the entire
  pyotp-in-env pattern was designed to *avoid*.

The bot drives the browser, the bot has the credentials, the bot
finishes the login. The user just sees the eventual `az account
list` output (or whatever the original task was).

## Quick rule reference

1. **Cache-first.** `az account show` before any `az login`.
2. **You drive the browser.** Never ask the user to open URLs or
   enter codes. Use the browser plugin's navigate / type / click /
   screenshot tools.
3. **Always click No on "Stay signed in?".**
4. **One TOTP code, one use, ~30s window.** Re-run `entra-totp` on
   "invalid code".
5. **Never echo `$ENTRA_PASSWORD` or `$ENTRA_TOTP_SEED`** in chat,
   logs, screenshots, or tool outputs. Use them by `$VAR` reference
   only.
6. **Read-only by default.** `az ... show / list` for inspection;
   confirm with the user before any mutating `az` call (create /
   delete / update).

## Troubleshooting

| Symptom                                              | Cause / fix                                                                       |
|------------------------------------------------------|-----------------------------------------------------------------------------------|
| `AADSTS500011: resource principal not found`        | bot user has no role on any subscription — ask the user to grant RBAC             |
| `AADSTS50079: user required to use MFA`             | you skipped or fumbled the TOTP step — repeat the device-code dance               |
| `entra-totp` prints "ENTRA_TOTP_SEED env not set"   | the operator hasn't sealed the seed into `openclaw-secrets` yet — surface to user |
| Microsoft rejects the password repeatedly           | screenshot the page, surface the exact error to user — password may have rotated  |
| Browser plugin can't find a form selector           | Microsoft occasionally redesigns the login UI. Take a screenshot, describe what you see to the user, ask for selector hints |
| You're tempted to ask the user to enter the code    | Stop. Go back to rule 2 of "Quick rule reference". Use the browser plugin.        |
