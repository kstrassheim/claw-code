"""Writing planning documents: local spool first, the store when it can.

WRITE LOCALLY, FLUSH OPPORTUNISTICALLY
--------------------------------------
Every write lands in an append-only spool on the workspace volume BEFORE
anything touches the network, and the spool is flushed to the store when it can
be. Three reasons:

  - A runner flushes its call count repeatedly during a long run. Those
    writes must be cheap and must not fail the run.
  - The pod restarts. Anything only in memory is gone; anything in the spool
    survives on the PVC and is flushed by the next run.
  - The store will be unreachable sometimes. A planning store that can stop
    the issue solver is a worse trade than one that is briefly behind.

So: NOTHING here raises. Every public function returns a bool or a value and
logs to stderr. A planning store that kills a solver run has cost more than it
will ever be worth.

TWO BACKENDS, ONE FILE
----------------------
The same planning documents are stored in MongoDB in one deployment and in
Azure Cosmos DB in another. Keeping two copies of this file was the obvious
alternative and the wrong one: the spool, the flush loop, the idempotence rule,
the "nothing raises" contract and every report that reads through `query()` are
identical in both, and two copies drift the day after they are made. So the
BACKEND is chosen from the environment and everything above it is written once.

What is genuinely portable, and is therefore what the reports use: writing a
document (a deterministic id, upserted), the spool, counting, probing, and the
FILTER-DICT form of `query()`. What is not portable is an aggregation
`pipeline=` — that is MongoDB's own language, and inventing a lowest common
denominator for it would make every report worse to read in order to hide one
difference. On Cosmos a pipeline returns [] and says so on stderr.

IDEMPOTENCE MAKES THE SPOOL SIMPLE
----------------------------------
Document ids are deterministic (planning_docs derives a work event's id from
runId+role), that id becomes the store's primary key — MongoDB's `_id`, Cosmos'
`id` — and every write is an UPSERT. A document flushed twice is therefore a
no-op, which means the spool never needs a two-phase commit: on a crash
mid-flush the worst case is re-sending something already stored.

NOTHING IS IMPORTED THAT MIGHT NOT BE THERE
-------------------------------------------
`import pymongo` happens inside the functions that talk to the server, never at
module scope. The spool is the part that must never fail, and it needs no
driver at all — so a missing or broken pymongo degrades this module to "writes
are being kept locally" instead of breaking every import of it. It also keeps
the unit tests free of a pip dependency: they exercise the spool, the flush
loop and the error classification against an injected fake collection.

The Cosmos backend goes further and needs no package at all: it speaks the REST
API over urllib rather than pulling in azure-cosmos and azure-identity, because
it has to work in an image that has no pip packages whatsoever.

For the same reason nothing here classifies a driver exception with
isinstance() — see `_is_permanent`.

CONFIGURATION
-------------
    PLANNING_BACKEND            mongo | cosmos   (optional; see below)

    PLANNING_MONGO_URI          mongodb://claw-code-mongodb:27017
    PLANNING_MONGO_DB           database   (default: planning)
    PLANNING_MONGO_COLLECTION   collection (default: planning)

    PLANNING_COSMOS_ENDPOINT    https://<account>.documents.azure.com:443/
    PLANNING_COSMOS_DB          database  (default: planning)
    PLANNING_COSMOS_CONTAINER   container (default: planning)

Credentials, if any, travel inside the connection string — which is why nothing
ever prints one unredacted. See `redact_uri`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SPOOL_PATH = os.environ.get(
    "PLANNING_SPOOL", os.path.expanduser("~/.openclaw/planning-spool.jsonl"))

MONGO_URI = os.environ.get("PLANNING_MONGO_URI", "").strip()
COSMOS_ENDPOINT = os.environ.get("PLANNING_COSMOS_ENDPOINT", "").strip().rstrip("/")

# Milliseconds, because that is the unit every pymongo timeout takes. Short on
# purpose: this runs inside a runner's exit path, and a store that is down must
# cost seconds, not minutes. The spool is what keeps the data safe meanwhile.
TIMEOUT_MS = int(os.environ.get("PLANNING_MONGO_TIMEOUT_MS", "5000"))

# Seconds, the unit urllib takes. Same reasoning, and deliberately longer: an
# Entra token exchange and a Cosmos round trip are two hops over the internet
# rather than one inside the cluster.
HTTP_TIMEOUT = int(os.environ.get("PLANNING_HTTP_TIMEOUT", "20"))

COSMOS_API_VERSION = "2018-12-31"

# One query can be answered across several REST pages. Bounded because this
# runs in a status command and in reports: 20 pages of 1000 is far more than
# any planning question asks for, and a runaway continuation loop against a
# large container would hang the caller instead of answering it.
COSMOS_PAGE_SIZE = int(os.environ.get("PLANNING_COSMOS_PAGE_SIZE", "1000"))
COSMOS_MAX_PAGES = int(os.environ.get("PLANNING_COSMOS_MAX_PAGES", "20"))

# A long outage must not fill the volume. At ~1 KB a document this is a few
# megabytes — far more than any plausible backlog, and still bounded. When it
# is hit the OLDEST entries go: the newest state of a story is the one worth
# keeping, and older work events are the most replaceable thing in here.
SPOOL_MAX_LINES = int(os.environ.get("PLANNING_SPOOL_MAX", "5000"))


def _log(msg: str) -> None:
    sys.stderr.write(f"[planning] {msg}\n")


# --- which backend ------------------------------------------------------------


def _select_backend() -> str:
    """"mongo", "cosmos", or "" when nothing is configured.

    In this order, and the order is the whole point:

      1. PLANNING_BACKEND, if set. An explicit answer always wins — including
         over a connection string that happens to be lying around in the
         environment, which is exactly the case someone sets it to settle.
      2. Otherwise whichever connection string exists.
      3. Otherwise nothing: `enabled()` is False and writes still spool.

    BOTH configured with no PLANNING_BACKEND is a MISCONFIGURATION, not a
    preference to be guessed at quietly. The failure it produces otherwise is
    the worst kind: writes land in one store and reports read the other, both
    halves work, and the data merely appears to vanish. So it is logged loudly
    and resolved DETERMINISTICALLY IN FAVOUR OF MONGO — deterministic because
    two processes disagreeing about the answer is the actual disaster, and
    mongo because it is the backend a deployment can be running without any
    cloud identity at all, so choosing it cannot silently start writing into a
    billed account nobody meant to use.
    """
    explicit = os.environ.get("PLANNING_BACKEND", "").strip().lower()
    if explicit in ("mongo", "mongodb"):
        return "mongo"
    if explicit in ("cosmos", "cosmosdb", "cosmos-db"):
        return "cosmos"
    if explicit:
        _log(f"PLANNING_BACKEND={explicit!r} is not a backend I know "
             f"(mongo | cosmos) — falling back to what is configured")
    if MONGO_URI and COSMOS_ENDPOINT:
        _log("BOTH PLANNING_MONGO_URI and PLANNING_COSMOS_ENDPOINT are set "
             "and PLANNING_BACKEND is not. Using mongo. Set PLANNING_BACKEND "
             "explicitly: writing to one store and reading from the other "
             "loses data in a way that looks like nothing happened.")
        return "mongo"
    if MONGO_URI:
        return "mongo"
    if COSMOS_ENDPOINT:
        return "cosmos"
    return ""


BACKEND = _select_backend()

# The active backend's names, so everything below — status, error messages,
# REST paths — reads the same whichever store is behind it. On Cosmos
# COLLECTION is the container; the two words mean the same thing here.
if BACKEND == "cosmos":
    DATABASE = os.environ.get("PLANNING_COSMOS_DB", "planning")
    COLLECTION = os.environ.get("PLANNING_COSMOS_CONTAINER", "planning")
else:
    DATABASE = os.environ.get("PLANNING_MONGO_DB", "planning")
    COLLECTION = os.environ.get("PLANNING_MONGO_COLLECTION", "planning")


def _connection_string() -> str:
    """The active backend's connection string, raw. Never printed unredacted."""
    if BACKEND == "cosmos":
        return COSMOS_ENDPOINT
    if BACKEND == "mongo":
        return MONGO_URI
    return ""


# Anything that looks like a secret carried as a key=value parameter. Cosmos
# connection strings put the account key in one of these, and a mongo URI can
# carry an authMechanismProperties password the same way; neither is covered by
# the user:password@host form below.
_SECRET_PARAM = re.compile(
    r"(?i)(accountkey|account_key|sharedaccesskey|signature|sig|password|pwd"
    r"|secret|apikey|api_key|accesskey|token)=[^;&\s]*")


def redact_uri(uri: str) -> str:
    """A connection string with any credentials removed, safe to print.

    `planning status` is run from chat, and its output is pasted into issues.
    A URI of the form mongodb://user:password@host/ would put the password in
    both, and so would a Cosmos endpoint someone pasted with an AccountKey
    still attached to it. Nothing in this module prints a raw connection
    string; everything goes through here.

    Deliberately a dumb string operation rather than a URL parse: the input may
    be malformed (that is often exactly why someone is running `status`), and a
    parser that raises on a broken URI would take the diagnostic down with it.
    """
    # The key is kept and the value replaced — \1 is the parameter's name, so
    # the reader still sees WHICH credential was there.
    text = _SECRET_PARAM.sub(r"\1=***", str(uri or ""))
    if "@" not in text:
        return text
    scheme, sep, rest = text.partition("://")
    if not sep:
        # No scheme to anchor on; drop everything before the last @ anyway
        # rather than risk printing a credential.
        return "***@" + text.rsplit("@", 1)[1]
    creds, at, host = rest.partition("@")
    if not at:
        return text
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if user else f"{scheme}://***@{host}"


def enabled() -> bool:
    """False until a connection string is configured.

    Writes still spool while disabled — that is deliberate. The data is being
    produced now; it can be flushed once the store is provisioned, instead of
    being lost in the meantime.

    Note this asks about the ACTIVE backend. PLANNING_BACKEND=cosmos with no
    endpoint is not enabled, however emphatically it was chosen: naming a
    backend is not configuring one.
    """
    return bool(_connection_string())


# --- the spool ---------------------------------------------------------------


def spool(doc: dict) -> bool:
    """Append one document. Returns False only if the volume refused it."""
    try:
        os.makedirs(os.path.dirname(SPOOL_PATH), exist_ok=True)
        line = json.dumps(doc, separators=(",", ":"), default=str)
        # Single append of one line: on POSIX a write this size to a file
        # opened O_APPEND does not interleave with another process's, which
        # is what lets the solver and the reviewer share one spool without
        # locking.
        with open(SPOOL_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except OSError as e:
        _log(f"could not spool a document: {e}")
        return False


def _read_spool() -> list[dict]:
    if not os.path.exists(SPOOL_PATH):
        return []
    out = []
    try:
        with open(SPOOL_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    # One corrupt line must not strand every document behind
                    # it. Drop it and say so.
                    _log("dropping an unparseable spool line")
    except OSError as e:
        _log(f"could not read the spool: {e}")
    return out


def _write_spool(docs: list[dict]) -> None:
    tmp = SPOOL_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for d in docs[-SPOOL_MAX_LINES:]:
                f.write(json.dumps(d, separators=(",", ":"), default=str) + "\n")
        os.replace(tmp, SPOOL_PATH)
    except OSError as e:
        _log(f"could not rewrite the spool: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def spool_depth() -> int:
    return len(_read_spool())


# Why the store could not be reached, recorded when it happens rather than
# guessed afterwards. `status` prints this, and the causes — no connection
# string, no driver, no server, no token — lead to completely different fixes.
_last_error = ""


# --- the MongoDB backend ------------------------------------------------------


# One client per process, reused. A runner flushes many times, and building a
# MongoClient per flush would rebuild the connection pool and repeat server
# discovery for every 400-byte document.
_client = None


def _pymongo():
    """The driver module, or None. Import failures are reported, never raised.

    Lazy on purpose — see the module docstring. This is also the ONLY place
    that imports it, so the fallback message is written once.
    """
    global _last_error
    try:
        import pymongo
        return pymongo
    except Exception as e:  # noqa: BLE001 — never raise out of the store
        _last_error = (f"the pymongo driver is not importable here "
                       f"({str(e)[:80]}) — documents are being spooled only")
        return None


def _collection():
    """The collection handle, or None when the store cannot be used.

    Returning None rather than raising is the whole contract of this module:
    every caller below treats None as "keep it in the spool and move on".
    """
    global _client, _last_error
    if not enabled():
        _last_error = "no connection string configured"
        return None
    if _client is None:
        mongo = _pymongo()
        if mongo is None:
            return None
        try:
            _client = mongo.MongoClient(
                MONGO_URI,
                # All three matter. Without serverSelectionTimeoutMS the
                # driver blocks for 30s on an unreachable server, which turns
                # a best-effort flush in a runner's exit path into half a
                # minute of dead time on every single run.
                serverSelectionTimeoutMS=TIMEOUT_MS,
                connectTimeoutMS=TIMEOUT_MS,
                socketTimeoutMS=TIMEOUT_MS,
                appname="claw-code-planning",
            )
        except Exception as e:  # noqa: BLE001
            _last_error = f"could not build a client: {str(e)[:160]}"
            return None
    try:
        return _client[DATABASE][COLLECTION]
    except Exception as e:  # noqa: BLE001
        _last_error = f"could not open {DATABASE}/{COLLECTION}: {str(e)[:120]}"
        return None


# Failures that are about THIS DOCUMENT and will therefore fail identically
# forever. Everything else — a server that is down, a credential that is wrong,
# a replica set electing — is transient, so the document stays spooled.
#
# Matched by exception NAME rather than by isinstance so that classifying an
# error never requires importing the driver. That keeps the rule readable in
# one place, keeps this module importable without pymongo, and lets the tests
# exercise both branches against a fake that raises look-alike exceptions.
_PERMANENT_ERRORS = {
    "InvalidDocument",      # not encodable as BSON at all
    "DocumentTooLarge",     # over the 16 MB limit; re-sending cannot help
    "InvalidBSON",
    "BSONError",
    "WriteError",           # rejected by the server for what it contains
    "InvalidOperation",
}

# Server error codes with the same property. 121 is document validation, 2 and
# 9 are a malformed command or value. Notably ABSENT: 13 (Unauthorized) and 18
# (AuthenticationFailed) — those are a deployment problem, they get fixed, and
# then the spool flushes. Dropping documents over them would throw away exactly
# the backlog the spool exists to hold.
_PERMANENT_CODES = (2, 9, 121)


def _is_permanent(exc: BaseException) -> bool:
    """Whether re-sending this document could ever work."""
    if type(exc).__name__ in _PERMANENT_ERRORS:
        return True
    return getattr(exc, "code", None) in _PERMANENT_CODES


def _mongo_doc(doc: dict) -> dict:
    """The document as MongoDB stores it: the deterministic `id` as `_id`.

    `_id` is the only field MongoDB guarantees is unique, and it is what makes
    the upsert idempotent. Writing the id into an ordinary field instead would
    let the same run land twice under two generated ObjectIds, which is not a
    duplicate anyone would notice — it is a story that appears to have cost
    twice what it did.

    `id` is kept alongside so a document read back looks the same as the one
    planning_docs produced.
    """
    out = dict(doc)
    out["_id"] = doc.get("id") or doc.get("_id")
    return out


def _mongo_upsert(doc: dict) -> tuple[bool, bool, str]:
    col = _collection()
    if col is None:
        return (False, False, _last_error or "no collection")
    body = _mongo_doc(doc)
    if not body.get("_id"):
        # A document with no deterministic id cannot be upserted idempotently,
        # so letting it through would mean a duplicate on every re-flush.
        return (False, True, "the document has no id")
    try:
        col.replace_one({"_id": body["_id"]}, body, upsert=True)
        return (True, False, "")
    except Exception as e:  # noqa: BLE001
        return (False, _is_permanent(e), f"{type(e).__name__}: {str(e)[:140]}")


def _mongo_query(filter, projection, sort, limit, pipeline) -> list[dict]:
    col = _collection()
    if col is None:
        _log(f"query skipped: {_last_error}")
        return []
    try:
        if pipeline is not None:
            return list(col.aggregate(list(pipeline)))
        cursor = col.find(dict(filter or {}), projection)
        if sort:
            cursor = cursor.sort(sort)
        if limit:
            cursor = cursor.limit(int(limit))
        return list(cursor)
    except Exception as e:  # noqa: BLE001
        _log(f"query failed: {type(e).__name__}: {str(e)[:160]}")
        return []


def _mongo_count() -> int | None:
    col = _collection()
    if col is None:
        return None
    try:
        return int(col.estimated_document_count())
    except Exception as e:  # noqa: BLE001
        _log(f"could not count documents: {str(e)[:120]}")
        return None


def _mongo_probe() -> tuple[bool, str]:
    col = _collection()
    if col is None:
        return (False, _last_error or "the store could not be opened")
    try:
        col.database.command("ping")
    except Exception as e:  # noqa: BLE001
        return (False, _explain(e, "cannot reach the server"))
    try:
        col.find_one({}, {"_id": 1})
    except Exception as e:  # noqa: BLE001
        return (False, _explain(e, f"cannot read {DATABASE}/{COLLECTION}"))
    return (True, "")


def _explain(exc: BaseException, what: str) -> str:
    """One line a reader can act on, from a driver exception.

    The driver's own messages are long and lead with topology detail; the two
    distinctions that actually change what you do next — unreachable versus
    unauthorised — are buried in them.
    """
    name = type(exc).__name__
    if name in ("ServerSelectionTimeoutError", "NetworkTimeout",
                "ConnectionFailure", "AutoReconnect"):
        return (f"{what}: no server answered within {TIMEOUT_MS} ms "
                f"({redact_uri(MONGO_URI)})")
    if getattr(exc, "code", None) in (13, 18) or name in (
            "OperationFailure", "InvalidOperation"):
        return (f"{what}: {name} — this user is not authorised on "
                f"{DATABASE}/{COLLECTION} ({str(exc)[:100]})")
    return f"{what}: {name}: {str(exc)[:140]}"


# --- the Cosmos backend: authentication ---------------------------------------


def token_resource(endpoint: str) -> str:
    """The Entra audience for a Cosmos endpoint — scheme and host, no port.

    NOT the endpoint string itself. Cosmos advertises itself as
    `https://<account>.documents.azure.com:443/`, and an Entra scope carrying
    an explicit port is rejected with a bare `HTTP 400 Bad Request` whose body
    never reaches the caller. Passing the endpoint through unchanged therefore
    reads exactly like a broken federated credential — wrong subject, wrong
    issuer, unbound service account — while the identity is in fact fine and
    only the audience is malformed. Hence a named function and this comment.
    """
    raw = endpoint if "://" in endpoint else "https://" + endpoint
    try:
        parts = urllib.parse.urlsplit(raw)
        if not parts.hostname:
            return str(endpoint or "").rstrip("/")
        return f"{parts.scheme}://{parts.hostname}"
    except Exception:  # noqa: BLE001 — a malformed endpoint must not raise
        return str(endpoint or "").rstrip("/")


_token_cache: dict[str, tuple[str, float]] = {}

# Which path actually produced the token in use, and why the preferred one did
# not. Recorded rather than inferred: reporting "auth: workload-identity"
# whenever AZURE_FEDERATED_TOKEN_FILE is set is a statement about the
# ENVIRONMENT, not about what happened.
#
# That reads as a fact and is not one. A caller whose token file is present in
# the environment but unreadable — a sandboxed shell, a dropped projection —
# falls back to the `az` user session, gets a 403 from Cosmos because that user
# holds no data-plane role, and sees "auth: workload-identity" sitting directly
# above the 403. The obvious conclusion is that the workload identity lacks the
# role. The workload identity was never used.
_last_auth = ""
_last_auth_note = ""

# Where the pod's workload-identity settings can be mirrored at boot.
#
# The admission webhook injects them as ENVIRONMENT variables, which works for
# anything started from the pod's own shell — the runners, a kubectl exec — and
# not for the chat agent, which hands tool calls a sanitised environment, so
# AZURE_CLIENT_ID arrives empty there. The store then falls back to the `az`
# user session, which holds no data-plane role, and every report asked from
# chat fails with 403 while the identical command from a pod shell succeeds.
# Both observations are correct and they look like a contradiction.
#
# So anything that cannot see the environment reads the values from a file on
# the workspace volume instead. Nothing secret is stored — these are an id, a
# tenant and a path. The projected TOKEN itself stays where Kubernetes put it.
IDENTITY_FILE = os.environ.get(
    "PLANNING_IDENTITY_FILE",
    os.path.expanduser("~/.openclaw/azure-identity.env"))


def _identity_setting(name: str) -> str:
    """One workload-identity setting: environment first, then the mirror file.

    Environment first so a caller that HAS the real thing is never overridden
    by a stale file.
    """
    value = os.environ.get(name, "")
    if value:
        return value
    try:
        with open(IDENTITY_FILE, encoding="utf-8") as f:
            for line in f:
                key, _, val = line.partition("=")
                if key.strip() == name:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _token(resource: str) -> str:
    """An Entra access token for the Cosmos account, or "".

    Cached until shortly before expiry: a runner flushes many times, and each
    exchange is a network round trip that would otherwise dominate the cost of
    writing a 400-byte document.
    """
    cached = _token_cache.get(resource)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    global _last_auth
    tok, expires = _token_workload_identity(resource)
    if tok:
        _last_auth = "workload-identity"
    else:
        tok, expires = _token_az_cli(resource)
        _last_auth = "az-cli" if tok else ""
    if tok:
        _token_cache[resource] = (tok, expires)
    return tok


def _token_workload_identity(resource: str) -> tuple[str, float]:
    """Exchange the projected service-account token for an Entra token."""
    global _last_auth_note
    client_id = _identity_setting("AZURE_CLIENT_ID")
    tenant = _identity_setting("AZURE_TENANT_ID")
    token_file = _identity_setting("AZURE_FEDERATED_TOKEN_FILE")
    authority = (_identity_setting("AZURE_AUTHORITY_HOST")
                 or "https://login.microsoftonline.com/").rstrip("/")
    # Say WHICH precondition failed. "workload identity unavailable" sends the
    # reader to Azure RBAC; "the token file is not readable from here" sends
    # them to the process that is running, which is where the difference
    # between a pod shell and a sandboxed one shows up.
    if not client_id:
        _last_auth_note = "AZURE_CLIENT_ID is not set"
    elif not tenant:
        _last_auth_note = "AZURE_TENANT_ID is not set"
    elif not token_file:
        _last_auth_note = "AZURE_FEDERATED_TOKEN_FILE is not set"
    elif not os.path.exists(token_file):
        _last_auth_note = f"{token_file} does not exist for this process"
    elif not os.access(token_file, os.R_OK):
        _last_auth_note = f"{token_file} is not readable by this process"
    else:
        _last_auth_note = ""
    if _last_auth_note:
        return ("", 0.0)
    try:
        with open(token_file, encoding="utf-8") as f:
            assertion = f.read().strip()
        body = urllib.parse.urlencode({
            "client_id": client_id,
            "client_assertion_type":
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "scope": f"{resource}/.default",
            "grant_type": "client_credentials",
        }).encode()
        code, raw, _ = _http(
            "POST", f"{authority}/{tenant}/oauth2/v2.0/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if code != 200:
            _last_auth_note = (f"the token exchange returned HTTP {code}: "
                               f"{raw.decode('utf-8', 'replace')[:120]}")
            return ("", 0.0)
        d = json.loads(raw or b"{}")
        return (d.get("access_token", ""),
                time.time() + int(d.get("expires_in", 3600)))
    except Exception as e:  # noqa: BLE001 — never raise out of the store
        _last_auth_note = f"the token exchange failed: {str(e)[:100]}"
        _log(f"workload-identity token exchange failed: {str(e)[:120]}")
        return ("", 0.0)


def _token_az_cli(resource: str) -> tuple[str, float]:
    """Fallback: whatever `az` is logged in as.

    A USER session whose cache lives on the volume and needs a browser after it
    expires. It is here so the store works before the ServiceAccount is bound
    to a workload identity, and it should fall out of use once that lands.
    """
    try:
        p = subprocess.run(
            ["az", "account", "get-access-token", "--resource", resource,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            _log(f"az token failed: {p.stderr.strip()[:120]}")
            return ("", 0.0)
        tok = p.stdout.strip()
        # az does not report expiry in this shape; assume a conservative 30
        # minutes rather than trusting a token we cannot inspect.
        return (tok, time.time() + 1800) if tok else ("", 0.0)
    except Exception as e:  # noqa: BLE001
        _log(f"az token unavailable: {str(e)[:120]}")
        return ("", 0.0)


# --- the Cosmos backend: REST -------------------------------------------------


def _http(method: str, url: str, *, data=None, headers=None,
          timeout: int | None = None) -> tuple[int, bytes, dict]:
    """(status, body, response headers). The ONLY network call in this module.

    One function so that a failure has one shape — status 0 means the request
    never got an answer — and so the tests can substitute the network in one
    place rather than reaching into urllib.
    """
    try:
        req = urllib.request.Request(url, data=data,
                                     headers=dict(headers or {}),
                                     method=method)
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as r:
            return (getattr(r, "status", 200) or 200, r.read(), dict(r.headers))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            body = b""
        return (e.code, body, dict(getattr(e, "headers", {}) or {}))
    except Exception as e:  # noqa: BLE001
        return (0, str(e)[:200].encode("utf-8", "replace"), {})


def _cosmos_request(method: str, path: str, *, body=None,
                    headers=None) -> tuple[int, object, dict]:
    """(status, payload, response headers) for one Cosmos REST call.

    The header dict has to survive: Cosmos puts things there that are
    available nowhere else — the container's document count, RU charges,
    continuation tokens.
    """
    global _last_error
    token = _token(token_resource(COSMOS_ENDPOINT))
    if not token:
        _last_error = ("no Entra token could be obtained"
                       + (f" ({_last_auth_note})" if _last_auth_note else ""))
        return (0, _last_error, {})
    h = {
        # Cosmos wants the Entra token wrapped in its own scheme, URL-encoded.
        "Authorization": urllib.parse.quote(f"type=aad&ver=1.0&sig={token}",
                                            safe=""),
        "x-ms-version": COSMOS_API_VERSION,
        "x-ms-date": time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                   time.gmtime()).lower(),
        "Content-Type": "application/json",
    }
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    code, raw, resp_headers = _http(method, COSMOS_ENDPOINT + path,
                                    data=data, headers=h)
    text = (raw or b"").decode("utf-8", "replace")
    if code == 0:
        _last_error = f"could not reach the account: {text[:160]}"
        return (0, text[:200], resp_headers)
    if 200 <= code < 300:
        try:
            return (code, json.loads(text) if text.strip() else {},
                    resp_headers)
        except ValueError:
            return (code, text[:200], resp_headers)
    _last_error = f"HTTP {code}: {text[:160]}"
    return (code, text[:200], resp_headers)


# The Cosmos half of the permanent-versus-transient rule, and it is drawn in
# the same place as the Mongo one: a failure is permanent only when it is about
# THIS DOCUMENT.
#
#   401 / 403  a missing data-plane role assignment. TRANSIENT: the role gets
#              granted, and the spool is exactly the backlog that should then
#              flush. Dropping documents over a 403 throws away the data the
#              spool exists to hold.
#   404        the database or container does not exist YET. Same argument.
#   408 / 429  a timeout or a throttle, by definition retryable.
#   5xx / 0    the service or the network. Retryable.
#
# Everything else in the 4xx range — a malformed document, one over the size
# limit, a partition-key mismatch — will fail identically forever, and a
# permanently poisoned spool blocks everything queued behind it.
_TRANSIENT_STATUS = (401, 403, 404, 408, 429)


def _is_permanent_status(code: int) -> bool:
    """Whether re-sending a document that got this HTTP status could ever work."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return False
    if code in _TRANSIENT_STATUS:
        return False
    return 400 <= code < 500


# Cosmos writes these itself and rejects nothing for carrying them, but a
# document read back and written again should look like the one planning_docs
# produced rather than accumulating bookkeeping. `_id` goes the same way: it is
# MongoDB's primary key, and on this backend `id` is.
_COSMOS_SYSTEM_FIELDS = ("_rid", "_self", "_etag", "_attachments", "_ts",
                         "_id")


def _cosmos_doc(doc: dict) -> dict:
    """The document as Cosmos stores it: the deterministic id in `id`."""
    out = {k: v for k, v in doc.items() if k not in _COSMOS_SYSTEM_FIELDS}
    out["id"] = doc.get("id") or doc.get("_id")
    return out


def _clean_row(doc) -> dict:
    """A document read back, without Cosmos' own bookkeeping fields."""
    if not hasattr(doc, "items"):
        return doc
    return {k: v for k, v in doc.items()
            if k not in ("_rid", "_self", "_etag", "_attachments", "_ts")}


def _cosmos_upsert(doc: dict) -> tuple[bool, bool, str]:
    if not enabled():
        return (False, False, _last_error or "no endpoint configured")
    body = _cosmos_doc(doc)
    if not body.get("id"):
        # Same rule as MongoDB's `_id`: without a deterministic id the upsert
        # is not idempotent and every re-flush adds a row.
        return (False, True, "the document has no id")
    body["id"] = str(body["id"])
    code, payload, _ = _cosmos_request(
        "POST", f"/dbs/{DATABASE}/colls/{COLLECTION}/docs",
        body=body,
        headers={
            "x-ms-documentdb-is-upsert": "true",
            "x-ms-documentdb-partitionkey": json.dumps([doc.get("pk")]),
        })
    if 200 <= code < 300:
        return (True, False, "")
    return (False, _is_permanent_status(code),
            f"HTTP {code}: {str(payload)[:140]}")


# A field name goes into the SQL text itself, so it is checked rather than
# quoted: a parameter can only stand where a VALUE stands. Anything that is not
# a plain (possibly dotted) identifier makes the whole filter untranslatable,
# which is reported — never silently dropped, because a dropped condition
# returns MORE rows than were asked for and nothing about the answer says so.
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

_COMPARISONS = {"$eq": "=", "$ne": "<>", "$gt": ">", "$gte": ">=",
                "$lt": "<", "$lte": "<="}


def _literal_prefix(pattern: str) -> str | None:
    """The literal string a `^...` regex is an exact prefix match for, or None.

    Worth the special case: the one regex the reports use is an anchored
    prefix over the grouping key, and STARTSWITH can use the index while
    RegexMatch cannot.
    """
    if not pattern.startswith("^"):
        return None
    out = []
    i = 1
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            # An escaped character is a literal one — which is precisely what
            # re.escape() produces for a prefix built out of a repo name.
            out.append(pattern[i + 1])
            i += 2
            continue
        if ch in ".^$*+?()[]{}|":
            return None
        out.append(ch)
        i += 1
    return "".join(out)


def _cosmos_where(filter: dict) -> tuple[str | None, list[dict]]:
    """A filter dict as a Cosmos SQL WHERE clause plus its parameters.

    The filter dict is the PORTABLE interface — the shape both backends
    implement and every report is written against. Values always travel as
    parameters; only checked field names ever reach the SQL text.

    Returns (None, []) when some part of the filter has no faithful
    translation. The caller reports that and returns nothing, because an
    approximate answer to a question about how much work a sprint cost is
    worse than no answer: it is wrong and it looks right.
    """
    clauses: list[str] = []
    params: list[dict] = []

    def _param(value) -> str:
        name = f"@p{len(params)}"
        params.append({"name": name, "value": value})
        return name

    for key, want in (filter or {}).items():
        field = "id" if key == "_id" else key
        if not _FIELD.match(str(field)):
            _log(f"cannot translate the field name {key!r} to SQL")
            return (None, [])
        col = f"c.{field}"
        if not hasattr(want, "items"):
            if want is None:
                # Mongo matches a missing field here too.
                clauses.append(f"(NOT IS_DEFINED({col}) OR {col} = null)")
            else:
                clauses.append(f"{col} = {_param(want)}")
            continue
        for op, operand in want.items():
            if op in _COMPARISONS:
                if op == "$ne":
                    # Mongo's $ne matches documents that do not have the field
                    # at all; a bare <> in SQL evaluates to undefined for those
                    # and quietly drops them. That difference decides whether a
                    # sprint with no `state` counts as finished.
                    clauses.append(f"(NOT IS_DEFINED({col}) OR "
                                   f"{col} <> {_param(operand)})")
                else:
                    clauses.append(
                        f"{col} {_COMPARISONS[op]} {_param(operand)}")
            elif op == "$in":
                clauses.append(
                    f"ARRAY_CONTAINS({_param(list(operand))}, {col})")
            elif op == "$nin":
                clauses.append(
                    f"NOT ARRAY_CONTAINS({_param(list(operand))}, {col})")
            elif op == "$exists":
                clauses.append(f"IS_DEFINED({col})" if operand
                               else f"NOT IS_DEFINED({col})")
            elif op == "$regex":
                prefix = _literal_prefix(str(operand))
                if prefix is not None:
                    clauses.append(f"STARTSWITH({col}, {_param(prefix)})")
                else:
                    clauses.append(f"RegexMatch({col}, {_param(str(operand))})")
            else:
                _log(f"cannot translate the operator {op!r} to SQL")
                return (None, [])

    return (" AND ".join(clauses), params)


def _cosmos_select(projection) -> str:
    """The SELECT list for a Mongo-style projection.

    Inclusion projections translate; an exclusion projection ({"x": 0}) does
    not have a SQL equivalent worth faking, so it becomes SELECT * and the
    caller gets more fields than it asked for — which is harmless, unlike
    getting fewer.
    """
    if not projection:
        return "*"
    try:
        wanted = [("id" if k == "_id" else k) for k, v in projection.items()
                  if v]
        if len(wanted) != len(projection):
            return "*"
        if "id" not in wanted:
            # Mongo returns `_id` unless it is excluded, and a row that cannot
            # be written back is a trap.
            wanted.insert(0, "id")
        if not all(_FIELD.match(str(f)) for f in wanted):
            return "*"
        return ", ".join(f"c.{f}" for f in wanted)
    except Exception:  # noqa: BLE001
        return "*"


def _sort_key(value):
    """An ordering key that never raises on mixed or missing values."""
    if value is None:
        return (0, 0.0, "")
    if value is True or value is False:
        return (1, float(value), "")
    if type(value) in (int, float):
        return (1, float(value), "")
    return (2, 0.0, str(value))


def _apply_sort(rows: list[dict], sort) -> list[dict]:
    """Sort in the process rather than in the query, on purpose.

    A cross-partition ORDER BY needs the query plan the SDKs fetch, and the
    REST gateway refuses to serve one without it. Sorting here keeps `sort=`
    meaning the same thing on both backends; the result sets these reports
    read are small enough that the round trip already cost more than the
    comparison does.
    """
    if not sort:
        return rows
    try:
        out = list(rows)
        for field, direction in reversed(list(sort)):
            out.sort(key=lambda d: _sort_key(d.get(field)),
                     reverse=int(direction) < 0)
        return out
    except Exception as e:  # noqa: BLE001
        _log(f"could not sort the rows: {str(e)[:100]}")
        return rows


def _cosmos_query(filter, projection, sort, limit, pipeline) -> list[dict]:
    if pipeline is not None:
        # Said out loud rather than approximated. An aggregation pipeline is
        # MongoDB's language; translating $group into SQL well enough to be
        # trusted is a project, and translating it badly would return numbers
        # that look like the answer. Every report here uses the portable
        # filter-dict form and aggregates in Python for exactly this reason.
        _log("an aggregation pipeline is a MongoDB-only query and this "
             "deployment stores planning documents in Cosmos DB — returning "
             "nothing. Use a filter and aggregate the rows in Python.")
        return []
    where, params = _cosmos_where(dict(filter or {}))
    if where is None:
        _log("query skipped: that filter has no faithful SQL translation")
        return []
    sql = f"SELECT {_cosmos_select(projection)} FROM c"
    if where:
        sql += f" WHERE {where}"

    headers = {
        "Content-Type": "application/query+json",
        "x-ms-documentdb-isquery": "true",
        "x-ms-max-item-count": str(COSMOS_PAGE_SIZE),
    }
    # Scoping to one partition is what every per-story question should do, and
    # the filter already says so when it can: an equality match on `pk` IS the
    # partition. Anything else fans out, which is correct for sprint-wide
    # questions and merely wasteful for the rest.
    pk = (filter or {}).get("pk")
    if type(pk) is str:
        headers["x-ms-documentdb-partitionkey"] = json.dumps([pk])
    else:
        headers["x-ms-documentdb-query-enablecrosspartition"] = "true"

    rows: list[dict] = []
    continuation = ""
    for _ in range(max(1, COSMOS_MAX_PAGES)):
        h = dict(headers)
        if continuation:
            h["x-ms-continuation"] = continuation
        code, payload, resp = _cosmos_request(
            "POST", f"/dbs/{DATABASE}/colls/{COLLECTION}/docs",
            body={"query": sql, "parameters": params}, headers=h)
        if code != 200 or not hasattr(payload, "get"):
            _log(f"query failed ({code}): {str(payload)[:160]}")
            return []
        rows.extend(_clean_row(d) for d in payload.get("Documents", []))
        continuation = (resp or {}).get("x-ms-continuation", "")
        if not continuation:
            break
    else:
        _log(f"stopped after {COSMOS_MAX_PAGES} pages of results; the answer "
             f"is incomplete. Narrow the filter.")

    rows = _apply_sort(rows, sort)
    return rows[:int(limit)] if limit else rows


def _cosmos_count() -> int | None:
    """NOT `SELECT VALUE COUNT(1)`.

    The REST gateway refuses cross-partition AGGREGATES outright — serving one
    needs the SDK's query-plan round trip, which this store deliberately does
    not implement — so an aggregate count fails silently into "unknown" on a
    store that is in fact working.

    Cosmos will simply tell you, if asked: a container read with quota info
    populated returns `documentsCount` in a response header, at the cost of a
    single point read rather than a full scan.
    """
    code, _, hdrs = _cosmos_request(
        "GET", f"/dbs/{DATABASE}/colls/{COLLECTION}",
        headers={"x-ms-documentdb-populatequotainfo": "true"})
    if code != 200:
        return None
    usage = (hdrs or {}).get("x-ms-resource-usage", "")
    for part in str(usage).split(";"):
        key, _, value = part.partition("=")
        if key.strip() == "documentsCount":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _cosmos_probe() -> tuple[bool, str]:
    """A point read of the container: one RU, and it settles all three things
    that can be wrong — the token, the authorisation, and whether the database
    and container actually exist."""
    code, payload, _ = _cosmos_request(
        "GET", f"/dbs/{DATABASE}/colls/{COLLECTION}")
    if code == 200:
        return (True, "")
    if code == 0:
        return (False, str(payload)[:160])
    if code in (401, 403):
        return (False, f"HTTP {code} — this identity is not authorised on the "
                       f"account (it needs a Cosmos SQL data-plane role)")
    if code == 404:
        return (False, f"HTTP 404 — there is no {DATABASE}/{COLLECTION} on "
                       f"this account")
    return (False, f"HTTP {code}: {str(payload)[:120]}")


# --- the public API -----------------------------------------------------------


def driver_version() -> str:
    """What this process would reach the store WITH, or "" when it cannot.

    Reported by `status` because "the store is unreachable" and "there is
    nothing in this image to reach it with" are the same symptom and a
    different problem, and the second one is invisible from any server-side
    log.

    On Cosmos the honest answer is that no package is needed at all — the REST
    API over urllib is always available — so this is non-empty there and the
    "IS NOT INSTALLED" reading never applies.
    """
    if BACKEND == "cosmos":
        return "urllib (the Cosmos REST API needs no driver)"
    mongo = _pymongo()
    return getattr(mongo, "__version__", "unknown") if mongo else ""


def document_count() -> int | None:
    """How many documents the store holds, or None if it cannot be had.

    On MongoDB this is `estimated_document_count()` and NOT
    `count_documents({})`: the exact count is a full collection scan, and this
    is printed by a status command that is run casually and often. The
    estimate comes from collection metadata and is exact except while a write
    is in flight. Cosmos reports the same thing in a header — see
    `_cosmos_count`.
    """
    if BACKEND == "cosmos":
        return _cosmos_count()
    return _mongo_count()


def _upsert(doc: dict) -> tuple[bool, bool, str]:
    """(sent, permanent, detail) for one document, whichever store is behind it.

    `permanent` is only meaningful when `sent` is False: True means drop it,
    False means keep it spooled and try again later. Both backends draw that
    line in the same place — see `_is_permanent` and `_is_permanent_status`.
    """
    if BACKEND == "cosmos":
        return _cosmos_upsert(doc)
    return _mongo_upsert(doc)


def query(filter: dict | None = None, *, projection: dict | None = None,
          sort=None, limit: int = 0,
          pipeline: list | None = None) -> list[dict]:
    """Read documents. Returns [] on any failure — callers report, never crash.

    Two shapes, and only two:

        query({"type": "story", "sprintId": 4})           an ordinary find
        query(pipeline=[{"$match": ...}, {"$group": ...}]) an aggregation

    The FILTER DICT is the portable one, and it is what every report here uses.
    Both backends implement it: on MongoDB it is passed to the driver as it
    stands, and on Cosmos it is translated to a parameterised
    `SELECT * FROM c WHERE ...` (see `_cosmos_where`). Equality, $ne, $in,
    $nin, $exists, the comparisons and an anchored $regex all cross over;
    anything else is refused loudly rather than approximated.

    `pipeline=` is MongoDB ONLY. On Cosmos it returns [] and says so on
    stderr — see `_cosmos_query` for why that is better than a translation.

    `sort` takes what pymongo takes — [("number", -1)] — and `limit` caps the
    rows. On MongoDB both happen server-side; on Cosmos both happen here, for
    the reason in `_apply_sort`. Either way the reports mostly fetch and
    aggregate in Python, where the result sets are small enough that a round
    trip costs more than the arithmetic.

    Scoping a query to one `pk` is what every per-story question should do; it
    is an equality match on the field the documents are grouped by, and it is
    the closest thing here to reading one aggregate at once.
    """
    if not enabled():
        return []
    if BACKEND == "cosmos":
        return _cosmos_query(filter, projection, sort, limit, pipeline)
    return _mongo_query(filter, projection, sort, limit, pipeline)


# --- the public write path ---------------------------------------------------


def write(doc: dict, *, validate=True) -> bool:
    """Spool a document, then try to flush. Always returns quickly.

    Returns whether it reached the SPOOL — not whether it reached the store.
    The caller cares that the data is durable; when it arrives is this module's
    problem, not the runner's.
    """
    if validate:
        try:
            import planning_docs
            problems = planning_docs.validate(doc)
        except Exception:  # noqa: BLE001
            problems = []
        if problems:
            # Refuse rather than store: a malformed document in the store is
            # queryable, looks like data, and skews every report on it.
            _log("refusing an invalid document: " + "; ".join(problems[:3]))
            return False
    if not spool(doc):
        return False
    flush()
    return True


def flush(limit: int = 200) -> int:
    """Push spooled documents to the store. Returns how many landed.

    Documents that fail stay in the spool for the next attempt. A failure that
    is about the document itself is dropped, though: it will fail identically
    forever, and a permanently poisoned spool blocks everything queued behind
    it. See `_is_permanent` and `_is_permanent_status` for where that line is
    drawn — in the same place on both backends.
    """
    if not enabled():
        return 0
    docs = _read_spool()
    if not docs:
        return 0

    sent, keep = 0, []
    for i, doc in enumerate(docs):
        if i >= limit:
            keep.extend(docs[i:])
            break
        ok, permanent, detail = _upsert(doc)
        if ok:
            sent += 1
        elif permanent:
            _log(f"dropping a document the store rejected: {detail}")
        else:
            # Transient: keep it and everything after it, so order is
            # preserved and we stop hammering a store that is down.
            keep.extend(docs[i:])
            _log(f"flush paused at {sent} sent: {detail}")
            break
    _write_spool(keep)
    if sent:
        _log(f"flushed {sent} document(s), {len(keep)} still spooled")
    return sent


def probe() -> tuple[bool, str]:
    """Actually reach the store. Returns (ok, reason-when-not).

    `enabled()` answers "is a connection string configured", which is NOT the
    question anyone asking about the store means. Reporting configuration as
    reachability produces the worst possible output: a confident "reachable:
    yes" printed directly above a query that returned nothing — so the store
    looks healthy and merely empty, and the two facts on screen contradict
    each other with nothing connecting them.

    On MongoDB that costs two round trips, because they answer different
    questions. `ping` settles the network and the credentials; the read settles
    AUTHORISATION on this database and collection, which a successful ping says
    nothing about — a user that can connect and cannot read is a real and
    confusing state. On Cosmos one point read settles both.
    """
    if not enabled():
        return (False, "no connection string configured")
    if BACKEND == "cosmos":
        return _cosmos_probe()
    return _mongo_probe()


def status() -> dict:
    """For a chat skill: is this thing working, and how far behind is it?"""
    ok, reason = probe()
    return {
        # Which store this process is actually talking to. First, because
        # every other line means something different depending on it.
        "backend": BACKEND or None,
        "enabled": enabled(),
        "reachable": ok,
        "reason": reason,
        # Redacted, always — this ends up in chat and in issue comments.
        "uri": redact_uri(_connection_string()) or None,
        "endpoint": (redact_uri(COSMOS_ENDPOINT) or None
                     if BACKEND == "cosmos" else None),
        "database": DATABASE,
        # The container, on Cosmos. Same idea, and the same key so that one
        # status renderer serves both.
        "collection": COLLECTION,
        "spoolPath": SPOOL_PATH,
        "spoolDepth": spool_depth(),
        # "" means there is no way to reach the store from this process, which
        # is a different problem from an unreachable server and is invisible
        # from the server side.
        "driver": driver_version(),
        # What was ACTUALLY used, recorded during the probe above — not what
        # the environment suggests. See `_last_auth` for why the difference
        # matters more than it looks. Empty on MongoDB, which authenticates
        # inside the connection string.
        "auth": (_last_auth or "none — no token could be obtained"
                 if BACKEND == "cosmos" else ""),
        "authNote": _last_auth_note if BACKEND == "cosmos" else "",
    }
