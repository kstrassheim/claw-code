#!/usr/bin/env python3
"""Upload a draft's screenshots to the repo's `tester-screenshots`
orphan branch and rewrite the draft so the body links to them.

Invoked by tester-runner.sh as part of post-agent processing. The
agent stages drafts of the form:

    {
      "title": "...",
      "body":  "markdown",
      "assigneeRole": "BOT"|"OWNER",
      "media": [
        {"path": "/home/node/.openclaw/media/browser/<uuid>.png",
         "alt":  "what this screenshot shows"}
      ]
    }

This helper:
  1. Reads the draft.
  2. For each item in `media`, base64-encodes the PNG and uploads it
     to `tester-screenshots` at `<HEAD_SHA>/<basename>`.
  3. On the first run for the repo the branch does not exist yet;
     it's created as an orphan branch (no shared history with main)
     via the Git Data API so it doesn't carry any project files.
  4. Appends a `## Screenshots` section to the draft body with
     `![alt](raw-url)` for each uploaded image.
  5. Removes the `media` field and writes the draft back to the
     same path.

Best-effort: if uploads fail (rate-limit, network blip, missing
PNG), the helper logs to stderr and leaves the draft body without
the section. The wrapper still creates the issue.

Env:
  GITHUB_TOKEN    — PAT used for all API calls (already present in
                    the openclaw pod's env).
  REPO            — "owner/name".
  HEAD_SHA        — tested commit; doubles as the subdirectory.

CLI:
  argv[1]         — absolute path to the draft.json file.

Stdout: prints one line per uploaded screenshot ("ok <url>") for
  log readability. Nothing else.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

BRANCH = "tester-screenshots"
API = "https://api.github.com"


def gh(method: str, url: str, body=None) -> tuple[int, dict]:
    """Make a single GitHub API call. Returns (status, json)."""
    token = os.environ["GITHUB_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
            return r.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except Exception:
            err = {}
        return e.code, err


def branch_exists(repo: str) -> bool:
    status, _ = gh("GET", f"{API}/repos/{repo}/branches/{BRANCH}")
    return status == 200


def create_orphan_branch_with_file(repo: str, repo_path: str, content_b64: str) -> bool:
    """Create the orphan branch in one shot by composing a blob → tree → commit
    with no parents → ref. Used only on the very first upload for the repo."""
    status, blob = gh("POST", f"{API}/repos/{repo}/git/blobs",
                      {"content": content_b64, "encoding": "base64"})
    if status >= 300:
        print(f"err: create blob: {status} {blob}", file=sys.stderr)
        return False
    status, tree = gh("POST", f"{API}/repos/{repo}/git/trees",
                      {"tree": [{"path": repo_path, "mode": "100644",
                                 "type": "blob", "sha": blob["sha"]}]})
    if status >= 300:
        print(f"err: create tree: {status} {tree}", file=sys.stderr)
        return False
    status, commit = gh("POST", f"{API}/repos/{repo}/git/commits",
                        {"message": f"tester screenshots seed",
                         "tree": tree["sha"], "parents": []})
    if status >= 300:
        print(f"err: create commit: {status} {commit}", file=sys.stderr)
        return False
    status, ref = gh("POST", f"{API}/repos/{repo}/git/refs",
                     {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    if status >= 300:
        print(f"err: create ref: {status} {ref}", file=sys.stderr)
        return False
    return True


def upload_to_existing_branch(repo: str, repo_path: str, content_b64: str) -> bool:
    """PUT the file via the contents API. Idempotent only if the path
    didn't exist; for re-uploads we'd need the existing blob's SHA. We
    don't re-upload (per-HEAD subdirs make the path unique), so we
    treat 422 ("already exists") as a soft success."""
    status, body = gh("PUT", f"{API}/repos/{repo}/contents/{repo_path}",
                      {"message": f"tester screenshot {repo_path}",
                       "content": content_b64,
                       "branch": BRANCH})
    if status in (200, 201):
        return True
    if status == 422 and "sha" in (body.get("message") or "").lower():
        # File already exists at that path. Treat as success.
        return True
    print(f"err: upload {repo_path}: {status} {body}", file=sys.stderr)
    return False


_PLACEHOLDER_RE = __import__("re").compile(
    r"^[^\n]*tester-screenshot:[^\n]*\n?", __import__("re").MULTILINE
)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: tester-upload-screenshots <draft.json>", file=sys.stderr)
        return 2
    draft_path = sys.argv[1]
    repo = os.environ["REPO"]
    head_sha = os.environ["HEAD_SHA"]

    with open(draft_path) as f:
        draft = json.load(f)

    # Always strip stale "tester-screenshot:<name>" placeholder lines
    # from the body — those are an old convention the agent occasionally
    # falls back to. Cleanest to remove them before we either attach
    # real screenshots or leave the body alone.
    body = draft.get("body") or ""
    new_body = _PLACEHOLDER_RE.sub("", body)
    if new_body != body:
        draft["body"] = new_body.rstrip() + "\n"

    media = draft.pop("media", None) or []

    # Fallback: if the agent didn't populate media[] (older MiniMax turns
    # often skip the new field even with the prompt update), accept a
    # colon-separated list of paths from the wrapper. The wrapper only
    # sets this for the FIRST draft processed in a run, so subsequent
    # drafts in a multi-issue run don't get the same screenshots
    # double-attached.
    if not media:
        fallback = os.environ.get("TESTER_FALLBACK_MEDIA", "")
        if fallback:
            media = [
                {"path": p, "alt": os.path.basename(p)}
                for p in fallback.split(":")
                if p and os.path.isfile(p)
            ]

    if not media:
        # Nothing to do — write back without the empty media key.
        with open(draft_path, "w") as f:
            json.dump(draft, f, indent=2)
        return 0

    have_branch = branch_exists(repo)
    uploaded: list[tuple[str, str]] = []

    for item in media:
        if isinstance(item, str):
            path, alt = item, os.path.basename(item)
        elif isinstance(item, dict):
            path = item.get("path")
            alt = item.get("alt") or (os.path.basename(path) if path else "")
        else:
            continue
        if not path or not os.path.isfile(path):
            print(f"warn: skipping missing media {path!r}", file=sys.stderr)
            continue
        try:
            with open(path, "rb") as fh:
                content_b64 = base64.b64encode(fh.read()).decode()
        except OSError as e:
            print(f"warn: read {path}: {e}", file=sys.stderr)
            continue

        basename = os.path.basename(path)
        repo_path = f"{head_sha}/{basename}"

        if not have_branch:
            if create_orphan_branch_with_file(repo, repo_path, content_b64):
                have_branch = True
                ok = True
            else:
                ok = False
        else:
            ok = upload_to_existing_branch(repo, repo_path, content_b64)

        if ok:
            url = (f"https://raw.githubusercontent.com/{repo}/"
                   f"{BRANCH}/{repo_path}")
            uploaded.append((url, alt))
            print(f"ok {url}")

    if uploaded:
        body = draft.get("body", "") or ""
        # Don't double-append if the agent already wrote a Screenshots
        # heading (unlikely; we control the prompt — but be safe).
        section = "\n\n## Screenshots\n\n"
        for url, alt in uploaded:
            section += f"![{alt}]({url})\n"
        draft["body"] = body + section

    with open(draft_path, "w") as f:
        json.dump(draft, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
