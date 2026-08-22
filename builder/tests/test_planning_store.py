"""The planning store, which is allowed to be late and never allowed to fail.

Every test here defends one of three properties, and they are the reason the
module is shaped the way it is:

  1. NOTHING RAISES. This code runs in a runner's exit path. A store that can
     throw is a store that can fail a run which has already merged its work,
     and no planning data is worth that.

  2. THE SPOOL IS THE DURABLE PART. A write is finished once it is on the
     volume; reaching the store is a separate, retryable, idempotent step. So
     an unreachable store must cost a delay and never a document.

  3. ONE FILE SERVES BOTH BACKENDS. The same module stores documents in
     MongoDB in one deployment and in Cosmos DB in another, and the spool, the
     flush loop, the idempotence rule and the filter-dict form of query() must
     behave IDENTICALLY on both — otherwise the second backend is a fork
     wearing the same filename.

The fakes below are deliberately tiny — the handful of calls the store
actually makes. They are also what keeps this file runnable with neither
pymongo nor any Azure package installed, which matters because the suite must
run anywhere the repository is checked out, not only inside the built image.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse

from harness import TMP_ROOT, load, temp_env

URI = "mongodb://claw-code-mongodb:27017"
ENDPOINT = "https://claw-planning.documents.azure.com:443"


# --- exceptions shaped like the driver's -------------------------------------
#
# planning_store classifies failures by exception NAME and `.code`, never by
# isinstance, precisely so it never has to import pymongo to decide. That is
# what lets these stand in for the real thing.

class ServerSelectionTimeoutError(Exception):
    pass


class DocumentTooLarge(Exception):
    pass


class OperationFailure(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, spec):
        for field, direction in reversed(list(spec)):
            self.rows.sort(key=lambda d: d.get(field) or 0,
                           reverse=direction < 0)
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def __iter__(self):
        return iter(self.rows)


class FakeDatabase:
    def __init__(self, collection):
        self.collection = collection

    def command(self, name):
        if self.collection.ping_error:
            raise self.collection.ping_error
        return {"ok": 1}


class FakeCollection:
    """Only what planning_store calls: replace_one, find, aggregate, count."""

    def __init__(self, write_error=None, read_error=None, ping_error=None):
        self.docs = {}
        self.write_error = write_error
        self.read_error = read_error
        self.ping_error = ping_error
        self.attempts = []
        self.pipelines = []

    @property
    def database(self):
        return FakeDatabase(self)

    def replace_one(self, filt, doc, upsert=False):
        self.attempts.append(filt["_id"])
        if self.write_error:
            raise self.write_error
        assert upsert, "every write must be an upsert or re-flushing duplicates"
        # Keyed on _id exactly as MongoDB would: this is what makes a second
        # flush of the same document a no-op rather than a second row.
        self.docs[filt["_id"]] = dict(doc)
        return object()

    def _matches(self, doc, filt):
        for key, want in (filt or {}).items():
            got = doc.get(key)
            if isinstance(want, dict):
                if "$ne" in want and got == want["$ne"]:
                    return False
                if "$regex" in want:
                    import re
                    if not re.search(want["$regex"], str(got or "")):
                        return False
            elif got != want:
                return False
        return True

    def find(self, filt=None, projection=None):
        if self.read_error:
            raise self.read_error
        return FakeCursor([d for d in self.docs.values()
                           if self._matches(d, filt)])

    def find_one(self, filt=None, projection=None):
        if self.read_error:
            raise self.read_error
        return next(iter(self.find(filt)), None)

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return iter([{"_id": "solver", "calls": 7}])

    def estimated_document_count(self):
        if self.read_error:
            raise self.read_error
        return len(self.docs)


class FakeAzure:
    """Stands in for the network: the Entra token endpoint and Cosmos' REST API.

    Installed as planning_store._http, which is the ONE function the module
    reaches anything through. That single seam is what lets the Cosmos backend
    be tested with no azure-identity, no azure-cosmos and no Azure account —
    the same bargain the fake collection strikes for pymongo.

    It deliberately does NOT implement Cosmos SQL. What is worth testing here
    is the TRANSLATION — that the filter dict became this SQL with these
    parameters — not a reimplementation of the query engine, which would only
    ever prove the fake agrees with itself.
    """

    def __init__(self):
        self.docs = {}          # id -> the document Cosmos would hold
        self.upserts = []       # (request headers, body) per document write
        self.queries = []       # (sql, parameters, request headers)
        self.token_scopes = []  # every scope the token exchange asked for
        self.rows = []          # what a query returns
        self.query_pages = None  # [(rows, continuation), ...] when paging
        self.doc_status = 201
        self.query_status = 200
        self.container_status = 200
        self.token_status = 200
        self.count = None

    def __call__(self, method, url, *, data=None, headers=None, timeout=None):
        headers = dict(headers or {})
        if "/oauth2/v2.0/token" in url:
            fields = urllib.parse.parse_qs((data or b"").decode())
            self.token_scopes.append(fields.get("scope", [""])[0])
            if self.token_status != 200:
                return (self.token_status, b'{"error":"invalid_request"}', {})
            return (200, json.dumps({"access_token": "entra-token",
                                     "expires_in": 3600}).encode(), {})
        body = json.loads(data.decode()) if data else None
        if method == "GET":
            # A point read of the container: probe() and document_count().
            if self.container_status != 200:
                return (self.container_status, b'{"message": "no"}', {})
            hdrs = {}
            if self.count is not None:
                hdrs["x-ms-resource-usage"] = (
                    f"documentSize=1;documentsCount={self.count};"
                    f"collectionSize=1")
            return (200, json.dumps({"id": "planning"}).encode(), hdrs)
        if headers.get("x-ms-documentdb-isquery"):
            self.queries.append((body["query"], body["parameters"], headers))
            if self.query_status != 200:
                return (self.query_status, b'{"message": "no"}', {})
            rows, continuation = self.rows, ""
            if self.query_pages:
                rows, continuation = self.query_pages.pop(0)
            return (200, json.dumps({"Documents": rows}).encode(),
                    {"x-ms-continuation": continuation} if continuation else {})
        self.upserts.append((headers, body))
        if not 200 <= self.doc_status < 300:
            return (self.doc_status, b'{"message": "rejected"}', {})
        # Keyed on `id` exactly as Cosmos would: this is what makes a second
        # flush of the same document a no-op rather than a second row.
        self.docs[body["id"]] = body
        return (self.doc_status, json.dumps(body).encode(), {})


class StoreTestCase(unittest.TestCase):
    """Each test gets its own spool file and its own freshly imported module.

    Fresh matters: the spool path, the connection string, the CHOSEN BACKEND
    and the spool bound are all read at import, so a test that changes the
    environment needs the module re-read rather than the copy an earlier test
    imported.
    """

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="planning-", dir=TMP_ROOT)
        self.spool_file = os.path.join(self.dir, "planning-spool.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def store(self, collection=None, **env):
        env.setdefault("PLANNING_SPOOL", self.spool_file)
        # Cleared unless a test says otherwise. Backend selection reads the
        # whole environment, so a stray PLANNING_COSMOS_ENDPOINT on the
        # developer's machine would otherwise decide what these tests measure.
        env.setdefault("PLANNING_BACKEND", None)
        env.setdefault("PLANNING_COSMOS_ENDPOINT", None)
        ctx = temp_env(**env)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        store = load("planning_store")
        if collection is not None:
            # The seam the whole suite hangs on: everything above _collection
            # is pure logic, everything below it is the driver.
            store._collection = lambda: collection
        return store

    def cosmos_store(self, **env):
        """A store configured for Cosmos, with the network faked out.

        The workload-identity settings are real files and real environment
        variables, so the token exchange is EXERCISED rather than stubbed —
        which is the only way the audience it asks for can be asserted.
        """
        token_file = os.path.join(self.dir, "sa-token")
        with open(token_file, "w", encoding="utf-8") as f:
            f.write("a-projected-service-account-token")
        env.setdefault("PLANNING_COSMOS_ENDPOINT", ENDPOINT)
        env.setdefault("PLANNING_MONGO_URI", None)
        env.setdefault("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
        env.setdefault("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000002")
        env.setdefault("AZURE_FEDERATED_TOKEN_FILE", token_file)
        store = self.store(**env)
        store._http = FakeAzure()
        # The `az` fallback shells out, and a developer machine that happens to
        # be logged in would answer it — turning "no token" tests into live
        # calls against a real tenant and passing for the wrong reason. It is
        # stubbed to silence by default and exercised explicitly below.
        self.az_calls = []

        def _no_az(resource):
            self.az_calls.append(resource)
            return ("", 0.0)

        store._token_az_cli = _no_az
        return store

    def spool_lines(self) -> list[dict]:
        """The spool file as documents, in the order it holds them."""
        with open(self.spool_file, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def work(self, run_id="r1", role="solver", calls=1):
        docs = load("planning_docs")
        return docs.work_doc(host="github", repo="acme/web", number=42,
                             run_id=run_id, role=role, llm_calls=calls,
                             now="2026-08-17T10:00:00+00:00")


class TheSpoolIsTheDurablePart(StoreTestCase):
    def test_enabled_is_false_when_nothing_is_configured(self):
        # And it must stay false rather than defaulting to localhost: a
        # default connection string would make every flush attempt a
        # connection to nothing, on every runner, forever.
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertFalse(store.enabled())

    def test_enabled_is_true_once_a_connection_string_exists(self):
        store = self.store(PLANNING_MONGO_URI=URI)
        self.assertTrue(store.enabled())

    def test_a_write_with_no_store_configured_still_spools(self):
        # The data is being produced NOW. Refusing to keep it until the store
        # is provisioned would lose exactly the history the estimator needs.
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertTrue(store.write(self.work()))
        self.assertEqual(store.spool_depth(), 1)
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 1)

    def test_an_unreachable_store_never_raises_and_keeps_the_document(self):
        fake = FakeCollection(
            write_error=ServerSelectionTimeoutError("no servers"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        self.assertTrue(store.write(self.work()))
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 1,
                         "a transient failure must not lose the document")

    def test_a_missing_driver_degrades_to_spooling(self):
        # Simulates an image built without the pip layer. `import pymongo`
        # raises ImportError when the entry is None, which is exactly what the
        # lazy import is there to absorb.
        self.addCleanup(sys.modules.pop, "pymongo", None)
        sys.modules["pymongo"] = None
        store = self.store(PLANNING_MONGO_URI=URI)
        self.assertTrue(store.write(self.work()))
        self.assertEqual(store.spool_depth(), 1)
        self.assertEqual(store.driver_version(), "")
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("pymongo", reason)

    def test_a_corrupt_spool_line_does_not_strand_the_ones_behind_it(self):
        store = self.store(PLANNING_MONGO_URI=None)
        store.spool(self.work(run_id="a"))
        with open(self.spool_file, "a", encoding="utf-8") as f:
            f.write("{ this was truncated by a kill -9\n")
        store.spool(self.work(run_id="b"))
        self.assertEqual(store.spool_depth(), 2)

    def test_an_invalid_document_is_refused_before_it_is_spooled(self):
        # A malformed document in the store is worse than one rejected here:
        # it is queryable, it looks like data, and it skews every report.
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertFalse(store.write({"type": "nonsense"}))
        self.assertEqual(store.spool_depth(), 0)

    def test_validation_can_be_waived_for_a_document_read_back(self):
        # Closing a sprint writes back the document the store returned, which
        # carries fields validate() knows nothing about.
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertTrue(store.write({"id": "x", "pk": "sprint#1",
                                     "type": "sprint", "_id": "x"},
                                    validate=False))
        self.assertEqual(store.spool_depth(), 1)


class TheSpoolIsBounded(StoreTestCase):
    def test_the_oldest_entries_go_first(self):
        # A long outage must not fill the volume. When the bound is hit the
        # OLDEST go: the newest state of a story is the one worth keeping, and
        # an old work event is the most replaceable thing in here.
        fake = FakeCollection(
            write_error=ServerSelectionTimeoutError("still down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI, PLANNING_SPOOL_MAX=5)
        for i in range(10):
            store.spool(self.work(run_id=f"run-{i}"))
        self.assertEqual(store.spool_depth(), 10)

        # The bound is applied when the spool is rewritten, which is what a
        # flush against a dead store does.
        store.flush()
        kept = self.spool_lines()
        self.assertEqual(len(kept), 5)
        self.assertEqual([d["runId"] for d in kept],
                         [f"run-{i}" for i in range(5, 10)])


class FlushingTwiceIsANoOp(StoreTestCase):
    def test_the_deterministic_id_becomes_the_primary_key(self):
        store = self.store(PLANNING_MONGO_URI=URI)
        doc = self.work()
        self.assertEqual(store._mongo_doc(doc)["_id"], doc["id"])

    def test_re_flushing_the_same_documents_stores_one_copy_each(self):
        # The property the whole spool design rests on. Without it the spool
        # would need a two-phase commit, and a crash mid-flush would either
        # lose a document or double a run's recorded cost.
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for run in ("r1", "r2"):
            store.spool(self.work(run_id=run))
        self.assertEqual(store.flush(), 2)
        self.assertEqual(store.spool_depth(), 0)

        for run in ("r1", "r2"):
            store.spool(self.work(run_id=run, calls=99))
        self.assertEqual(store.flush(), 2)
        self.assertEqual(len(fake.docs), 2, "a re-flush must replace, not add")
        self.assertEqual(fake.attempts.count("work#r1#solver"), 2)
        self.assertEqual(fake.docs["work#r1#solver"]["llmCalls"], 99,
                         "the later write is the one that survives")

    def test_a_partial_record_is_replaced_by_the_final_one(self):
        # planning-record --follow writes the running total every couple of
        # minutes. Each write must overwrite the last, or a long solve would
        # appear in the reports as fifty separate runs.
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for calls in (10, 50, 137):
            store.write(self.work(run_id="same-run", calls=calls))
        self.assertEqual(len(fake.docs), 1)
        self.assertEqual(fake.docs["work#same-run#solver"]["llmCalls"], 137)

    def test_a_document_with_no_id_is_dropped_rather_than_duplicated(self):
        store = self.store(FakeCollection(), PLANNING_MONGO_URI=URI)
        sent, permanent, detail = store._upsert({"pk": "story#github#a/b#1"})
        self.assertFalse(sent)
        self.assertTrue(permanent, "it would duplicate on every re-flush")
        self.assertIn("id", detail)


class WhatIsWorthRetrying(StoreTestCase):
    def test_a_document_the_server_rejects_is_dropped(self):
        # It will fail identically forever, and a poisoned spool blocks
        # everything queued behind it.
        fake = FakeCollection(write_error=DocumentTooLarge("17 MB"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        store.spool(self.work())
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 0)

    def test_document_validation_is_permanent(self):
        fake = FakeCollection(
            write_error=OperationFailure("failed validation", code=121))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        store.spool(self.work())
        store.flush()
        self.assertEqual(store.spool_depth(), 0)

    def test_an_authorisation_failure_keeps_the_backlog(self):
        # Credentials get fixed; the spool is exactly the backlog that should
        # then flush. Dropping documents over a 13 would throw away the data
        # the spool exists to hold.
        fake = FakeCollection(
            write_error=OperationFailure("not authorized", code=13))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        store.spool(self.work())
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 1)

    def test_the_line_between_permanent_and_transient(self):
        # Stated as a table because this single predicate decides whether a
        # document is kept or thrown away. Getting it wrong in one direction
        # loses data; in the other it wedges the whole spool behind one
        # unsendable document.
        store = self.store(PLANNING_MONGO_URI=URI)
        self.assertTrue(store._is_permanent(DocumentTooLarge("17 MB")))
        self.assertTrue(store._is_permanent(OperationFailure("bad", code=121)))
        self.assertFalse(store._is_permanent(
            ServerSelectionTimeoutError("down")))
        self.assertFalse(store._is_permanent(
            OperationFailure("not authorized", code=13)))
        self.assertFalse(store._is_permanent(
            OperationFailure("auth failed", code=18)))

    def test_a_paused_flush_preserves_order(self):
        fake = FakeCollection(write_error=ServerSelectionTimeoutError("down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for i in range(3):
            store.spool(self.work(run_id=f"r{i}"))
        store.flush()
        kept = self.spool_lines()
        self.assertEqual([d["runId"] for d in kept], ["r0", "r1", "r2"])

    def test_the_limit_leaves_the_rest_spooled(self):
        # A flush runs inside a runner's exit path; it must be bounded work.
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for i in range(5):
            store.spool(self.work(run_id=f"r{i}"))
        self.assertEqual(store.flush(limit=2), 2)
        self.assertEqual(store.spool_depth(), 3)


class ReadingBack(StoreTestCase):
    def test_a_query_without_a_store_is_empty_not_an_error(self):
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertEqual(store.query({"type": "sprint"}), [])

    def test_a_find_is_sorted_and_limited_by_the_server(self):
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for n in (1, 3, 2):
            fake.docs[f"sprint#{n}"] = {"_id": f"sprint#{n}", "type": "sprint",
                                        "number": n}
        rows = store.query({"type": "sprint"}, sort=[("number", -1)], limit=2)
        self.assertEqual([r["number"] for r in rows], [3, 2])

    def test_a_pipeline_goes_to_aggregate(self):
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        rows = store.query(pipeline=[{"$match": {"type": "work"}}])
        self.assertEqual(rows, [{"_id": "solver", "calls": 7}])
        self.assertEqual(fake.pipelines, [[{"$match": {"type": "work"}}]])

    def test_a_failing_query_is_an_empty_list(self):
        # Reports print what they get. A raise here would turn "the store is
        # down" into a traceback in a chat window.
        fake = FakeCollection(read_error=ServerSelectionTimeoutError("down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        self.assertEqual(store.query({"type": "story"}), [])

    def test_document_count_is_none_when_it_cannot_be_had(self):
        # None and 0 are different answers, and printing 0 for "could not ask"
        # describes an empty store that is in fact merely unreachable.
        fake = FakeCollection(read_error=ServerSelectionTimeoutError("down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        self.assertIsNone(store.document_count())

    def test_document_count_reports_what_is_there(self):
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        store.write(self.work())
        self.assertEqual(store.document_count(), 1)


class TellingTheTruthAboutTheStore(StoreTestCase):
    def test_a_configured_store_is_not_a_working_one(self):
        # The worst possible output is "reachable: yes" printed above a query
        # that returned nothing: the store looks healthy and merely empty.
        fake = FakeCollection(ping_error=ServerSelectionTimeoutError("down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        self.assertTrue(store.enabled())
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("no server answered", reason)

    def test_connecting_and_reading_are_separate_questions(self):
        # A user that can connect and cannot read this collection is a real
        # state, and a successful ping says nothing about it.
        fake = FakeCollection(read_error=OperationFailure("not authorized",
                                                         code=13))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("not authorised", reason)

    def test_a_healthy_store_probes_clean(self):
        store = self.store(FakeCollection(), PLANNING_MONGO_URI=URI)
        self.assertEqual(store.probe(), (True, ""))

    def test_probe_without_a_connection_string_says_so(self):
        store = self.store(PLANNING_MONGO_URI=None)
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("no connection string", reason)

    def test_status_carries_the_spool_depth_and_the_collection(self):
        store = self.store(FakeCollection(), PLANNING_MONGO_URI=URI,
                           PLANNING_MONGO_DB="planning",
                           PLANNING_MONGO_COLLECTION="planning")
        store.spool(self.work())
        st = store.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["spoolDepth"], 1)
        self.assertEqual(st["database"], "planning")
        self.assertEqual(st["collection"], "planning")
        self.assertEqual(st["spoolPath"], self.spool_file)


class CredentialsNeverReachTheOutput(StoreTestCase):
    def test_a_password_is_redacted(self):
        store = self.store(PLANNING_MONGO_URI=None)
        out = store.redact_uri("mongodb://claw:sup3rs3cret@mongo:27017/planning")
        self.assertNotIn("sup3rs3cret", out)
        self.assertIn("claw", out, "the user is worth seeing; the secret is not")

    def test_a_uri_without_credentials_is_unchanged(self):
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertEqual(store.redact_uri(URI), URI)

    def test_garbage_is_returned_rather_than_raising(self):
        # `status` is most often run BECAUSE the connection string is wrong. A
        # parser that raised on a malformed URI would take the diagnostic down
        # with it.
        store = self.store(PLANNING_MONGO_URI=None)
        for junk in ("", "not a uri", "user:pw@host", "mongodb://@/"):
            with self.subTest(junk=junk):
                self.assertIsInstance(store.redact_uri(junk), str)

    def test_status_reports_only_the_redacted_form(self):
        secret = "mongodb://claw:sup3rs3cret@mongo:27017"
        store = self.store(FakeCollection(), PLANNING_MONGO_URI=secret)
        self.assertNotIn("sup3rs3cret", json.dumps(store.status()))


class ChoosingABackend(StoreTestCase):
    """One file, two stores, and no guessing about which one is in use.

    The failure this defends against is the quiet one: writes landing in one
    store while reports read the other. Both halves work, nothing errors, and
    the data merely appears not to exist.
    """

    def test_a_mongo_uri_alone_selects_mongo(self):
        store = self.store(PLANNING_MONGO_URI=URI)
        self.assertEqual(store.BACKEND, "mongo")
        self.assertTrue(store.enabled())

    def test_a_cosmos_endpoint_alone_selects_cosmos(self):
        store = self.store(PLANNING_MONGO_URI=None,
                           PLANNING_COSMOS_ENDPOINT=ENDPOINT)
        self.assertEqual(store.BACKEND, "cosmos")
        self.assertTrue(store.enabled())

    def test_nothing_configured_is_no_backend_and_still_spools(self):
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertEqual(store.BACKEND, "")
        self.assertFalse(store.enabled())
        self.assertTrue(store.write(self.work()),
                        "the data is being produced now; it must be kept")
        self.assertEqual(store.spool_depth(), 1)

    def test_an_explicit_choice_beats_a_connection_string(self):
        # Both configured AND an explicit answer: the explicit answer is the
        # whole point of the variable, so it wins in both directions.
        store = self.store(PLANNING_BACKEND="cosmos", PLANNING_MONGO_URI=URI,
                           PLANNING_COSMOS_ENDPOINT=ENDPOINT)
        self.assertEqual(store.BACKEND, "cosmos")
        store = self.store(PLANNING_BACKEND="mongo", PLANNING_MONGO_URI=URI,
                           PLANNING_COSMOS_ENDPOINT=ENDPOINT)
        self.assertEqual(store.BACKEND, "mongo")

    def test_both_configured_with_no_choice_is_loud_and_deterministic(self):
        # Deterministic because two processes disagreeing about the answer is
        # the actual disaster; loud because it is a misconfiguration and not a
        # preference to be guessed at quietly.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            store = self.store(PLANNING_MONGO_URI=URI,
                               PLANNING_COSMOS_ENDPOINT=ENDPOINT)
        self.assertEqual(store.BACKEND, "mongo")
        self.assertIn("PLANNING_BACKEND", err.getvalue())
        self.assertIn("BOTH", err.getvalue())

    def test_naming_a_backend_is_not_configuring_one(self):
        # PLANNING_BACKEND decides WHICH store; it cannot conjure one. Saying
        # enabled() here would make every flush a request to nowhere.
        store = self.store(PLANNING_BACKEND="cosmos", PLANNING_MONGO_URI=None)
        self.assertEqual(store.BACKEND, "cosmos")
        self.assertFalse(store.enabled())
        self.assertTrue(store.write(self.work()))
        self.assertEqual(store.spool_depth(), 1)
        self.assertEqual(store.flush(), 0)

    def test_an_unknown_backend_name_falls_back_and_says_so(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            store = self.store(PLANNING_BACKEND="postgres",
                               PLANNING_MONGO_URI=URI)
        self.assertEqual(store.BACKEND, "mongo")
        self.assertIn("postgres", err.getvalue())


class TheEntraAudience(StoreTestCase):
    """The audience is the scheme and the host. Not the endpoint.

    Cosmos advertises itself with an explicit :443, and a scope carrying a
    port is rejected with a bare HTTP 400 whose body never reaches the caller
    — which reads exactly like a broken federated credential and sends the
    reader to Azure RBAC for an afternoon.
    """

    def test_the_port_is_not_part_of_the_audience(self):
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertEqual(
            store.token_resource("https://claw.documents.azure.com:443/"),
            "https://claw.documents.azure.com")

    def test_a_bare_host_still_produces_a_scheme(self):
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertEqual(store.token_resource("claw.documents.azure.com:443"),
                         "https://claw.documents.azure.com")

    def test_garbage_is_returned_rather_than_raising(self):
        store = self.store(PLANNING_MONGO_URI=None)
        for junk in ("", "://", "not an endpoint"):
            with self.subTest(junk=junk):
                self.assertIsInstance(store.token_resource(junk), str)

    def test_the_token_exchange_asks_for_the_portless_scope(self):
        # The assertion that would have saved the afternoon: not that a token
        # was requested, but what it was requested FOR.
        store = self.cosmos_store()
        store.probe()
        self.assertEqual(store._http.token_scopes,
                         ["https://claw-planning.documents.azure.com/.default"])

    def test_a_token_is_fetched_once_and_reused(self):
        # A runner flushes many times; an exchange per document would cost
        # more than writing it.
        store = self.cosmos_store()
        for i in range(3):
            store.spool(self.work(run_id=f"r{i}"))
        store.flush()
        self.assertEqual(len(store._http.token_scopes), 1)

    def test_the_token_is_wrapped_in_the_scheme_cosmos_expects(self):
        store = self.cosmos_store()
        store.write(self.work())
        headers = store._http.upserts[0][0]
        self.assertIn("type%3Daad", headers["Authorization"])
        self.assertIn("entra-token", headers["Authorization"])


class WritingToCosmos(StoreTestCase):
    def test_re_flushing_the_same_documents_stores_one_copy_each(self):
        # The same property the Mongo backend is held to, and for the same
        # reason: without it the spool would need a two-phase commit.
        store = self.cosmos_store()
        for run in ("r1", "r2"):
            store.spool(self.work(run_id=run))
        self.assertEqual(store.flush(), 2)
        self.assertEqual(store.spool_depth(), 0)

        for run in ("r1", "r2"):
            store.spool(self.work(run_id=run, calls=99))
        self.assertEqual(store.flush(), 2)
        fake = store._http
        self.assertEqual(len(fake.docs), 2, "a re-flush must replace, not add")
        self.assertEqual(len(fake.upserts), 4)
        self.assertEqual(fake.docs["work#r1#solver"]["llmCalls"], 99,
                         "the later write is the one that survives")

    def test_every_write_is_an_upsert_on_the_deterministic_id(self):
        store = self.cosmos_store()
        doc = self.work()
        store.write(doc)
        headers, body = store._http.upserts[0]
        self.assertEqual(headers["x-ms-documentdb-is-upsert"], "true")
        self.assertEqual(body["id"], doc["id"])
        self.assertEqual(json.loads(headers["x-ms-documentdb-partitionkey"]),
                         [doc["pk"]])

    def test_mongo_bookkeeping_does_not_travel_to_cosmos(self):
        # Closing a sprint writes back a document the store returned. On
        # MongoDB that carries `_id`; sending it here would store a field that
        # means nothing and that the other backend would then read back.
        store = self.cosmos_store()
        store.write({"id": "sprint#3", "_id": "sprint#3", "pk": "sprint#3",
                     "type": "sprint", "_etag": "\"0x1\"", "_ts": 1},
                    validate=False)
        body = store._http.upserts[0][1]
        self.assertEqual(body["id"], "sprint#3")
        for junk in ("_id", "_etag", "_ts"):
            self.assertNotIn(junk, body)

    def test_a_document_with_no_id_is_dropped_rather_than_duplicated(self):
        store = self.cosmos_store()
        sent, permanent, detail = store._upsert({"pk": "story#github#a/b#1"})
        self.assertFalse(sent)
        self.assertTrue(permanent, "it would duplicate on every re-flush")
        self.assertIn("id", detail)

    def test_a_missing_token_keeps_the_backlog(self):
        # No identity yet is a deployment state that gets fixed, and the spool
        # is the backlog that should then flush.
        store = self.cosmos_store()
        store._http.token_status = 400
        store.spool(self.work())
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 1)
        self.assertEqual(store._http.upserts, [],
                         "nothing may be sent without a token")

    def test_an_unusable_token_file_falls_back_and_says_which_path_was_used(self):
        # The precondition that failed is the whole diagnostic. "workload
        # identity unavailable" sends the reader to Azure RBAC; naming the
        # token file sends them to the process that is running, which is where
        # a sandboxed environment differs from a pod shell.
        store = self.cosmos_store(AZURE_FEDERATED_TOKEN_FILE=os.path.join(
            self.dir, "not-projected-here"))
        store._token_az_cli = lambda resource: ("az-session-token", 1e11)
        self.assertEqual(store.probe(), (True, ""))
        st = store.status()
        self.assertEqual(st["auth"], "az-cli")
        self.assertIn("not-projected-here", st["authNote"])


class WhatIsWorthRetryingOnCosmos(StoreTestCase):
    def test_the_line_between_permanent_and_transient(self):
        # Stated as a table for the same reason as the Mongo one: this single
        # predicate decides whether a document is kept or thrown away, and the
        # line has to sit in the same place on both backends.
        store = self.cosmos_store()
        for code in (400, 409, 413, 414):
            with self.subTest(permanent=code):
                self.assertTrue(store._is_permanent_status(code))
        for code in (0, 401, 403, 404, 408, 429, 500, 503):
            with self.subTest(transient=code):
                self.assertFalse(store._is_permanent_status(code))

    def test_an_authorisation_failure_keeps_the_backlog(self):
        # A 403 means the data-plane role has not been granted yet. Dropping
        # documents over it would throw away exactly what the spool is for.
        store = self.cosmos_store()
        store._http.doc_status = 403
        store.spool(self.work())
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 1)

    def test_a_container_that_does_not_exist_yet_keeps_the_backlog(self):
        store = self.cosmos_store()
        store._http.doc_status = 404
        store.spool(self.work())
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 1)

    def test_a_throttle_pauses_the_flush_and_preserves_order(self):
        store = self.cosmos_store()
        store._http.doc_status = 429
        for i in range(3):
            store.spool(self.work(run_id=f"r{i}"))
        self.assertEqual(store.flush(), 0)
        self.assertEqual([d["runId"] for d in self.spool_lines()],
                         ["r0", "r1", "r2"])

    def test_a_document_the_service_rejects_is_dropped(self):
        # It will fail identically forever, and a poisoned spool blocks
        # everything queued behind it.
        store = self.cosmos_store()
        store._http.doc_status = 400
        store.spool(self.work())
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 0)


class ReadingBackFromCosmos(StoreTestCase):
    """The filter dict is the portable interface. This is its translation."""

    def sql(self, store):
        return store._http.queries[-1][0]

    def params(self, store):
        return {p["name"]: p["value"] for p in store._http.queries[-1][1]}

    def test_an_equality_filter_becomes_parameterised_sql(self):
        # Parameterised, not interpolated: a value comes from a document and
        # must never be able to reach the SQL text.
        store = self.cosmos_store()
        store.query({"type": "story", "sprintId": 4})
        self.assertEqual(self.sql(store),
                         "SELECT * FROM c WHERE c.type = @p0 AND c.sprintId = @p1")
        self.assertEqual(self.params(store), {"@p0": "story", "@p1": 4})

    def test_ne_also_matches_a_document_without_the_field(self):
        # Mongo's $ne matches documents that lack the field entirely; a bare
        # <> in SQL evaluates to undefined for those and drops them. That
        # difference decides whether a sprint with no `state` counts as
        # finished, which is a number someone reads.
        store = self.cosmos_store()
        store.query({"type": "sprint", "state": {"$ne": "active"}})
        self.assertIn("NOT IS_DEFINED(c.state) OR c.state <> @p1",
                      self.sql(store))

    def test_an_anchored_regex_becomes_a_prefix_match(self):
        # The one regex the reports use is an anchored prefix over the
        # grouping key, and STARTSWITH can use the index where RegexMatch
        # cannot.
        store = self.cosmos_store()
        store.query({"type": "work",
                     "pk": {"$regex": "^story\\#github\\#acme/web\\#"}})
        self.assertIn("STARTSWITH(c.pk, @p1)", self.sql(store))
        self.assertEqual(self.params(store)["@p1"], "story#github#acme/web#")

    def test_an_unanchored_regex_still_works(self):
        store = self.cosmos_store()
        store.query({"pk": {"$regex": "web.*"}})
        self.assertIn("RegexMatch(c.pk, @p0)", self.sql(store))

    def test_the_mongo_primary_key_is_asked_for_by_its_cosmos_name(self):
        store = self.cosmos_store()
        store.query({"_id": "sprint#3"})
        self.assertIn("c.id = @p0", self.sql(store))

    def test_a_projection_becomes_a_select_list_that_keeps_the_id(self):
        # A row that cannot be written back is a trap: `planning` reads a
        # document, edits it and writes it again.
        store = self.cosmos_store()
        store.query({"type": "story"}, projection={"pk": 1, "number": 1})
        self.assertTrue(self.sql(store).startswith(
            "SELECT c.id, c.pk, c.number FROM c"))

    def test_a_pk_equality_scopes_the_query_to_one_partition(self):
        store = self.cosmos_store()
        store.query({"pk": "story#github#acme/web#42"})
        headers = store._http.queries[-1][2]
        self.assertEqual(json.loads(headers["x-ms-documentdb-partitionkey"]),
                         ["story#github#acme/web#42"])
        self.assertNotIn("x-ms-documentdb-query-enablecrosspartition", headers)

    def test_anything_else_fans_out_across_partitions(self):
        store = self.cosmos_store()
        store.query({"type": "sprint"})
        headers = store._http.queries[-1][2]
        self.assertEqual(
            headers["x-ms-documentdb-query-enablecrosspartition"], "true")

    def test_sort_and_limit_mean_the_same_thing_on_both_backends(self):
        # Applied here rather than in the query: a cross-partition ORDER BY
        # needs the query plan the SDKs fetch and the REST gateway refuses to
        # serve one without it. The CALLER must not have to know that.
        store = self.cosmos_store()
        store._http.rows = [{"id": "s1", "number": 1}, {"id": "s3", "number": 3},
                            {"id": "s2", "number": 2}]
        rows = store.query({"type": "sprint"}, sort=[("number", -1)], limit=2)
        self.assertEqual([r["number"] for r in rows], [3, 2])

    def test_cosmos_bookkeeping_is_stripped_from_the_rows(self):
        store = self.cosmos_store()
        store._http.rows = [{"id": "s1", "_rid": "x", "_etag": "y", "_ts": 1}]
        self.assertEqual(store.query({"type": "sprint"}), [{"id": "s1"}])

    def test_a_continuation_is_followed(self):
        store = self.cosmos_store()
        store._http.query_pages = [([{"id": "a"}], "next-page"),
                                   ([{"id": "b"}], "")]
        rows = store.query({"type": "sprint"})
        self.assertEqual([r["id"] for r in rows], ["a", "b"])

    def test_a_pipeline_is_refused_with_a_note_rather_than_faked(self):
        # An aggregation is MongoDB's own language. Translating $group badly
        # would return numbers that look like the answer, which is worse than
        # returning none — so this says what happened, out loud.
        store = self.cosmos_store()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rows = store.query(pipeline=[{"$group": {"_id": "$role"}}])
        self.assertEqual(rows, [])
        self.assertIn("MongoDB-only", err.getvalue())
        self.assertEqual(store._http.queries, [],
                         "nothing may be sent to Cosmos for a pipeline")

    def test_an_untranslatable_filter_returns_nothing_rather_than_more(self):
        # A silently dropped condition returns MORE rows than were asked for,
        # and nothing about the answer says so.
        store = self.cosmos_store()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rows = store.query({"type": {"$where": "this.x > 1"}})
        self.assertEqual(rows, [])
        self.assertIn("$where", err.getvalue())
        self.assertEqual(store._http.queries, [])

    def test_a_failing_query_is_an_empty_list(self):
        store = self.cosmos_store()
        store._http.query_status = 500
        self.assertEqual(store.query({"type": "story"}), [])

    def test_a_query_without_an_endpoint_is_empty_not_an_error(self):
        store = self.store(PLANNING_BACKEND="cosmos", PLANNING_MONGO_URI=None)
        self.assertEqual(store.query({"type": "sprint"}), [])

    def test_document_count_comes_from_the_header_not_an_aggregate(self):
        # The REST gateway refuses cross-partition aggregates outright, so
        # SELECT VALUE COUNT(1) would fail into "unknown" on a working store.
        store = self.cosmos_store()
        store._http.count = 41
        self.assertEqual(store.document_count(), 41)

    def test_document_count_is_none_when_it_cannot_be_had(self):
        store = self.cosmos_store()
        store._http.container_status = 403
        self.assertIsNone(store.document_count())


class TellingTheTruthAboutTheCosmosStore(StoreTestCase):
    def test_a_healthy_account_probes_clean(self):
        store = self.cosmos_store()
        self.assertEqual(store.probe(), (True, ""))

    def test_a_configured_account_is_not_a_working_one(self):
        store = self.cosmos_store()
        store._http.container_status = 403
        self.assertTrue(store.enabled())
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("not authorised", reason)

    def test_a_missing_container_is_named_as_such(self):
        store = self.cosmos_store()
        store._http.container_status = 404
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("planning/planning", reason)

    def test_status_says_which_backend_is_in_use(self):
        # Every other line in the status means something different depending
        # on this one.
        mongo = self.store(FakeCollection(), PLANNING_MONGO_URI=URI)
        self.assertEqual(mongo.status()["backend"], "mongo")
        cosmos = self.cosmos_store()
        st = cosmos.status()
        self.assertEqual(st["backend"], "cosmos")
        self.assertEqual(st["database"], "planning")
        self.assertEqual(st["collection"], "planning")
        self.assertEqual(st["spoolPath"], self.spool_file)

    def test_status_reports_which_identity_actually_produced_the_token(self):
        # Not which one the environment suggests: a token file that is present
        # but unreadable falls back to the az session, and reporting the
        # workload identity there sends the reader to Azure RBAC for a problem
        # that is in this process.
        store = self.cosmos_store()
        self.assertEqual(store.status()["auth"], "workload-identity")

    def test_the_reason_a_missing_driver_would_be_reported_does_not_apply(self):
        # There is no package to be missing on this backend, and printing an
        # empty driver would render as "pymongo IS NOT INSTALLED here" above a
        # store that is working perfectly.
        store = self.cosmos_store()
        self.assertTrue(store.driver_version())


class CredentialsNeverReachTheOutputOnEitherBackend(StoreTestCase):
    def test_an_account_key_on_an_endpoint_is_redacted(self):
        store = self.store(PLANNING_MONGO_URI=None)
        out = store.redact_uri(
            "https://claw.documents.azure.com:443/;AccountKey=sup3rs3cret;")
        self.assertNotIn("sup3rs3cret", out)
        self.assertIn("claw.documents.azure.com", out,
                      "the account is worth seeing; the key is not")

    def test_a_plain_endpoint_is_unchanged(self):
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertEqual(store.redact_uri(ENDPOINT), ENDPOINT)

    def test_status_reports_only_the_redacted_form_on_cosmos(self):
        secret = "https://claw.documents.azure.com:443/?AccountKey=sup3rs3cret"
        store = self.cosmos_store(PLANNING_COSMOS_ENDPOINT=secret)
        text = json.dumps(store.status())
        self.assertNotIn("sup3rs3cret", text)
        self.assertIn("claw.documents.azure.com", text)

    def test_status_reports_only_the_redacted_form_on_mongo(self):
        secret = "mongodb://claw:sup3rs3cret@mongo:27017"
        store = self.store(FakeCollection(), PLANNING_MONGO_URI=secret)
        self.assertNotIn("sup3rs3cret", json.dumps(store.status()))


if __name__ == "__main__":
    unittest.main()
