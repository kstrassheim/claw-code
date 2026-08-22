<!--
  Appended to TOOLS.md ONLY for the openclaw instance (not olga) via the
  conditional in templates/tools-md-configmap.yaml. Describes the
  read-only Gmail capability scoped to this single instance.
-->

---

# Gmail — read-only mailbox access

You have **read-only** access to one Gmail account (the one wired to
the GMAIL_* env vars on this pod). The capability is **already
configured**, do **not** ask the user for credentials, an account name,
or which mailbox to use — those are injected on every pod start.

## What is set up for you

- `mcp.servers.gmail` — pre-registered stdio MCP server at
  `/opt/gmail-mcp/gmail-mcp.mjs`. Exposes three tools, all read-only:
  - `gmail_list` — search/list messages with the same query syntax as
    the Gmail web UI search box (`is:unread`, `from:`, `subject:`,
    `after:YYYY/MM/DD`, `label:`, `newer_than:7d`, etc.). Returns id,
    threadId, from, subject, date, snippet.
  - `gmail_get` — fetch one message in full (headers + decoded text
    body), keyed by the id from `gmail_list`.
  - `gmail_thread` — fetch every message in a thread (conversation)
    in chronological order, keyed by threadId.

## Scope of access

- **Read-only by hard guarantee.** The OAuth refresh token used was
  issued with scope `https://www.googleapis.com/auth/gmail.readonly`
  only. Even if you somehow constructed a write call against the
  Gmail API, Google would server-side reject it with
  `insufficient_scope`. The MCP server here also doesn't expose any
  write tools — there is no "send", "draft", "delete", "label", or
  "modify" surface to attempt.
- One mailbox only. The MCP server is bound to a single account via
  the refresh token; there is no way to switch users.

## Workflow conventions

1. **Start with a narrow query.** Don't list the entire inbox if the
   user asked about one sender or one subject — pass the appropriate
   `q` to `gmail_list` and you'll get back ≤50 hits instead of
   pages of irrelevant mail.
2. **Use threads for context.** If a single message references a
   reply chain, `gmail_thread` is almost always more useful than
   `gmail_get` — you get the whole conversation, in order.
3. **Don't paste full bodies into chat.** Decoded message bodies can
   be large. Summarise; quote only the relevant lines.
4. **You cannot send replies.** If the user asks you to reply / send /
   archive / delete / label — say so plainly. Don't try to invoke a
   write tool that doesn't exist, and don't pretend the action
   succeeded.

## Quick reference

| Goal                              | Tool call                                                     |
|-----------------------------------|---------------------------------------------------------------|
| Recent unread                     | `gmail_list({ query: "is:unread", max: 20 })`                 |
| Mail from one sender              | `gmail_list({ query: "from:alice@example.com" })`             |
| Mail this week                    | `gmail_list({ query: "newer_than:7d" })`                      |
| Full body of one message          | `gmail_get({ id: "<id from gmail_list>" })`                   |
| Whole conversation                | `gmail_thread({ threadId: "<threadId from gmail_list>" })`    |
| Search by subject across history  | `gmail_list({ query: "subject:invoice" })`                    |
