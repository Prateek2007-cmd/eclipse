"""Mission fact store at scale (PR #5, W5).

FTS5 recall, dedup on write, mission scoping, compaction,
per-mission budgets, expiry, and the migration that wires them
all up. All test data is fake (generated prices, roles, checks)
with fixed seeds for determinism.

Scale test: 10k seeded facts, recall asserted under 200ms
(the old full-scan recall measured ~80ms on the same 10k set —
see /tmp/w5-bench/bench.py — and grows linearly with the table;
the FTS path does not).
"""
import random
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.core import db  # noqa: E402
from server.memory import (  # noqa: E402
    budgets,
    compaction,
    facts_fts,
    facts_migrate,
    keyword,
)

# --- The OLD recall, copied verbatim from keyword.py @ a7426a7 ---
# Used as an oracle: the new FTS recall must return the same
# facts, in the same order, with the same scores, for every query.
_OLD_STOPWORDS = keyword.STOPWORDS


def _old_keywords(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if t not in _OLD_STOPWORDS and len(t) > 1}


def _old_recall(conn, query: str, limit: int = 5) -> list[dict]:
    query_keys = _old_keywords(query)
    if not query_keys:
        return []
    rows = conn.execute(
        "SELECT id, text, source, topic, created_at FROM facts"
    ).fetchall()
    scored = []
    for row in rows:
        fact_keys = _old_keywords(row["text"]) | _old_keywords(
            row["topic"] or ""
        )
        overlap = len(query_keys & fact_keys)
        if overlap == 0:
            continue
        score = overlap / len(query_keys)
        scored.append((score, row["created_at"], {
            "id": row["id"], "text": row["text"], "source": row["source"],
            "topic": row["topic"], "score": round(score, 3)}))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [s[2] for s in scored[:limit]]


PRODUCTS = ["headphones", "keyboard", "mouse", "monitor", "laptop",
            "chair", "desk", "webcam", "speaker", "router"]
STORES = ["amazon", "flipkart", "croma", "vijay sales", "reliance"]
TITLES = ["SDE", "ML engineer", "data analyst", "frontend dev",
          "backend dev", "QA engineer", "DevOps engineer"]
COMPANIES = ["fakecorp", "testlabs", "demoworks", "sampletech",
             "mocksoft", "placeholder inc", "example systems"]
OUTCOMES = ["matched", "applied", "skipped", "ineligible", "drafted"]
SITES = ["internshala", "linkedin", "naukri", "cutshort", "wellfound"]


def gen_scale_facts(n: int, seed: int = 1234):
    """Deterministic synthetic facts (~40% price, ~40% role,
    ~20% checks). Every text is unique per index within its
    scope, so the (scope, fact_hash) unique index never trips:
    prices are unique per index, roles carry a unique ref."""
    rng = random.Random(seed)
    base = 1793000000
    facts = []
    for i in range(n):
        roll = rng.random()
        created = base + i * 137
        if roll < 0.40:
            product = rng.choice(PRODUCTS)
            price = 499 + (i * 37) % 49500  # unique per i
            store = rng.choice(STORES)
            text = f"Price check: {product} at \u20b9{price:,} on {store}"
            topic, scope = "price", f"m_price_{rng.randint(1, 5)}"
        elif roll < 0.80:
            title = rng.choice(TITLES)
            company = rng.choice(COMPANIES)
            outcome = rng.choice(OUTCOMES)
            text = f"Role seen: {title} at {company} \u2014 {outcome} (ref {i})"
            topic, scope = "role", f"m_job_{rng.randint(1, 5)}"
        else:
            site = rng.choice(SITES)
            status = rng.choice(["ok", "changed", "error", "no change"])
            text = (f"Check {i}: {site} status {status}, "
                    f"{rng.randint(0, 9)} new items")
            topic, scope = "check", "global"
        facts.append((f"fact-{i:06x}", text, "mission", topic, scope, created))
    return facts


def gen_price_facts(n: int, scope: str, base_ts: int, day_span: int = 14):
    """n price facts spread over day_span days, oldest first.
    Returns (rows, prices) where rows are raw-insert tuples."""
    rows, prices = [], []
    step = (day_span * 86400) // max(1, n)
    for i in range(n):
        price = 999 + (i * 137) % 40000  # unique per i
        prices.append(price)
        rows.append((
            f"fact-p{i:05x}",
            f"Price check: headphones at \u20b9{price:,} on amazon",
            "mission", "price", scope, base_ts + i * step,
        ))
    return rows, prices


LEGACY_FACTS_DDL = (
    "CREATE TABLE facts("
    " id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT,"
    " topic TEXT, created_at INTEGER)"
)


def _legacy_db(path):
    """A genuinely pre-migration database: the facts table in
    its old shape (no fact_hash/scope columns, no FTS table, no
    unique index). db.migrate() now wires the facts migration
    in, so legacy state has to be built by hand."""
    conn = db.connect(path)
    conn.execute(LEGACY_FACTS_DDL)
    conn.commit()
    return conn


class _TmpDb(unittest.TestCase):
    migrated = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        if self.migrated:
            facts_migrate.apply_facts_migrations(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def seed_raw(self, rows):
        """Insert facts with explicit ids/timestamps (fast bulk
        path for scale tests), then re-run the migration to
        backfill hashes — which also proves idempotency."""
        self.conn.executemany(
            "INSERT INTO facts(id, text, source, topic, scope, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return facts_migrate.apply_facts_migrations(self.conn)

    def count_facts(self, scope=None):
        if scope is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM facts").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM facts WHERE scope = ?",
                (scope,)).fetchone()
        return int(row["c"])


class TestMigration(_TmpDb):
    def test_idempotent_second_run_changes_nothing(self):
        keyword.store_fact(self.conn, "User prefers dark mode", topic="prefs")
        report = facts_migrate.apply_facts_migrations(self.conn)
        self.assertFalse(report["added_fact_hash"])
        self.assertFalse(report["added_scope"])
        self.assertEqual(report["hashed"], 0)
        self.assertEqual(report["deduped"], 0)
        self.assertEqual(report["fts_inserted"], 0)
        self.assertEqual(report["fts_removed"], 0)
        self.assertEqual(self.count_facts(), 1)

    def test_backfills_facts_stored_before_migration(self):
        # A database with facts but no migration yet.
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "legacy.db"
        conn = _legacy_db(path)
        try:
            conn.executemany(
                "INSERT INTO facts(id, text, source, topic, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                [("fact-a", "User prefers dark mode", "user", "prefs", 1000),
                 ("fact-b", "Gateway host is example.org", "user", "infra", 2000)],
            )
            conn.commit()
            report = facts_migrate.apply_facts_migrations(conn)
            self.assertTrue(report["added_fact_hash"])
            self.assertTrue(report["added_scope"])
            self.assertEqual(report["hashed"], 2)
            self.assertEqual(report["fts_inserted"], 2)
            rows = conn.execute(
                "SELECT scope, fact_hash FROM facts").fetchall()
            self.assertTrue(all(r["scope"] == "global" for r in rows))
            self.assertTrue(all(r["fact_hash"] for r in rows))
            self.assertEqual(facts_fts.fts_count(conn), 2)
            hits = keyword.recall(conn, "dark mode")
            self.assertEqual(len(hits), 1)
        finally:
            conn.close()
            tmp.cleanup()

    def test_dedups_preexisting_duplicates(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "dup.db"
        conn = _legacy_db(path)
        try:
            conn.executemany(
                "INSERT INTO facts(id, text, source, topic, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                [("fact-a", "Same fact twice", "user", None, 1000),
                 ("fact-b", "Same fact twice", "user", None, 2000),
                 ("fact-c", "A different fact", "user", None, 3000)],
            )
            conn.commit()
            report = facts_migrate.apply_facts_migrations(conn)
            self.assertEqual(report["deduped"], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM facts")
                .fetchone()["c"], 2)
            # The latest-inserted duplicate survives.
            self.assertIsNotNone(
                conn.execute("SELECT id FROM facts WHERE id = 'fact-b'")
                .fetchone())
            self.assertIsNone(
                conn.execute("SELECT id FROM facts WHERE id = 'fact-a'")
                .fetchone())
        finally:
            conn.close()
            tmp.cleanup()

    def test_missing_facts_table_raises(self):
        conn = db.connect(Path(self.tmp.name) / "empty.db")
        try:
            with self.assertRaises(ValueError):
                facts_migrate.apply_facts_migrations(conn)
        finally:
            conn.close()

    def test_backfill_repairs_stale_fts_rows(self):
        keyword.store_fact(self.conn, "User prefers dark mode")
        # Simulate a missed delete trigger: remove the fact row
        # behind the trigger's back (temp table swap is overkill —
        # drop the trigger, delete, re-add it).
        self.conn.execute("DROP TRIGGER facts_fts_ad")
        self.conn.execute(
            "DELETE FROM facts WHERE text LIKE '%dark mode%'")
        self.conn.commit()
        self.assertEqual(facts_fts.fts_count(self.conn), 1)
        report = facts_migrate.apply_facts_migrations(self.conn)
        self.assertEqual(report["fts_removed"], 1)
        self.assertEqual(facts_fts.fts_count(self.conn), 0)


class TestUnmigratedFallback(unittest.TestCase):
    """Pre-migration databases keep working: legacy insert +
    full-scan recall. db.migrate() now wires the facts
    migration in, so the legacy state is built by hand with
    _legacy_db(); the fallback still matters for any database
    that bypasses migrate()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = _legacy_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_store_and_recall_without_migration(self):
        keyword.store_fact(self.conn, "User prefers dark mode", topic="prefs")
        hits = keyword.recall(self.conn, "dark mode")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["topic"], "prefs")
        self.assertIsNone(hits[0]["scope"])

    def test_scoped_recall_without_migration_is_empty(self):
        keyword.store_fact(self.conn, "User prefers dark mode")
        self.assertEqual(
            keyword.recall(self.conn, "dark mode", scope="m_1"), [])

    def test_scoped_summary_without_migration_is_empty(self):
        keyword.store_fact(self.conn, "User prefers dark mode")
        self.assertEqual(keyword.get_scoped_summary(self.conn, "m_1"), [])

    def test_no_dedup_without_migration(self):
        # Legacy behaviour: two identical inserts are two rows
        # (dedup needs the hash column).
        keyword.store_fact(self.conn, "Same fact")
        keyword.store_fact(self.conn, "Same fact")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM facts")
            .fetchone()["c"], 2)


class TestFtsRecall(_TmpDb):
    def test_fts_table_created_and_populated(self):
        self.assertTrue(facts_fts.fts_exists(self.conn))
        keyword.store_fact(self.conn, "User prefers dark mode")
        keyword.store_fact(self.conn, "Gateway host is example.org")
        self.assertEqual(facts_fts.fts_count(self.conn), 2)

    def test_triggers_keep_index_in_sync(self):
        fact_id = keyword.store_fact(
            self.conn, "User prefers dark mode", topic="prefs")
        self.assertEqual(len(keyword.recall(self.conn, "dark")), 1)
        # Update: the index follows the new text, not the old.
        # ("dark mode" would still match via "mode", so the
        # check uses the single term "dark".)
        self.conn.execute(
            "UPDATE facts SET text = ? WHERE id = ?",
            ("User prefers light mode", fact_id))
        self.conn.commit()
        self.assertEqual(keyword.recall(self.conn, "dark"), [])
        self.assertEqual(len(keyword.recall(self.conn, "light mode")), 1)
        # Delete: the fact leaves the index.
        self.assertTrue(keyword.delete_fact(self.conn, fact_id))
        self.assertEqual(facts_fts.fts_count(self.conn), 0)
        self.assertEqual(keyword.recall(self.conn, "light mode"), [])

    def test_recall_matches_old_semantics(self):
        # Distinct created_at per fact so (score, created_at)
        # totally orders the results — the comparison is exact.
        rows = gen_scale_facts(200, seed=99)
        rows.append(("fact-cafe", "User likes caf\u00e9 lattes in the morning",
                     "user", "prefs", "global", 1793000000 + 999999))
        self.seed_raw(rows)
        queries = [
            "headphones price",
            "SDE matched fakecorp",
            "price check",
            "where is the resume",
            "quantum entanglement",
            "what is the",
            "caf",
            "check changed",
            "role",
            # Zero full matches: the fast path finds nothing and
            # partial matches must fill the results exactly as
            # the old recall did (headphones=price facts,
            # fakecorp=role facts — no fact has both).
            "headphones fakecorp",
            "keyboard matched",
        ]
        for query in queries:
            with self.subTest(query=query):
                old = _old_recall(self.conn, query, limit=5)
                new = keyword.recall(self.conn, query, limit=5)
                self.assertEqual(
                    [h["id"] for h in new], [h["id"] for h in old])
                self.assertEqual(
                    [h["score"] for h in new], [h["score"] for h in old])

    def test_recall_matches_topic_keywords(self):
        keyword.store_fact(self.conn, "Resume is stored locally",
                           topic="resume")
        hits = keyword.recall(self.conn, "where is the resume")
        self.assertEqual(len(hits), 1)

    def test_match_query_sanitisation(self):
        # Hostile terms are dropped, never interpolated.
        self.assertEqual(
            facts_fts.build_match_query({"dark", 'x" OR 1=1 --', "a-b"}),
            "dark*")
        self.assertIsNone(facts_fts.build_match_query(set()))
        self.assertIsNone(
            facts_fts.build_match_query({'"; DROP TABLE facts; --'}))
        # A query full of FTS5 syntax chars recalls normally.
        keyword.store_fact(self.conn, "User prefers dark mode")
        hits = keyword.recall(self.conn, 'dark OR * "mode"')
        self.assertEqual(len(hits), 1)

    def test_positional_limit_still_works(self):
        # memory_api/loop call recall(conn, query, limit=N) — the
        # old positional shape must keep working.
        for i in range(3):
            keyword.store_fact(self.conn, f"resume fact number {i}",
                               topic="resume")
        self.assertEqual(len(keyword.recall(self.conn, "resume", 2)), 2)


class TestDedup(_TmpDb):
    def test_same_fact_twice_returns_same_id_one_row(self):
        first = keyword.store_fact(self.conn, "Price check: headphones at \u20b91,299")
        second = keyword.store_fact(self.conn, "Price check: headphones at \u20b91,299")
        self.assertEqual(first, second)
        self.assertEqual(self.count_facts(), 1)

    def test_same_text_different_scope_is_two_facts(self):
        a = keyword.store_fact(self.conn, "Price check: headphones", scope="m_a")
        b = keyword.store_fact(self.conn, "Price check: headphones", scope="m_b")
        self.assertNotEqual(a, b)
        self.assertEqual(self.count_facts(), 2)

    def test_normalisation_variants_dedup(self):
        first = keyword.store_fact(self.conn, "  Price   CHECK: Headphones ")
        second = keyword.store_fact(self.conn, "price check: headphones")
        self.assertEqual(first, second)
        self.assertEqual(self.count_facts(), 1)

    def test_different_text_is_a_different_fact(self):
        a = keyword.store_fact(self.conn, "Price check: headphones")
        b = keyword.store_fact(self.conn, "Price check: keyboard")
        self.assertNotEqual(a, b)
        self.assertEqual(self.count_facts(), 2)


class TestScoping(_TmpDb):
    def setUp(self):
        super().setUp()
        keyword.store_fact(self.conn, "Price check: headphones at \u20b91,299",
                           topic="price", scope="m_a")
        keyword.store_fact(self.conn, "Price check: keyboard at \u20b92,499",
                           topic="price", scope="m_b")
        keyword.store_fact(self.conn, "User prefers dark mode",
                           topic="prefs", scope="global")

    def test_scope_a_invisible_to_scope_b_recall(self):
        # Distinctive single terms: "price" is shared by both
        # scopes' facts, so a two-term query would legitimately
        # match the other scope's fact at 0.5.
        hits = keyword.recall(self.conn, "headphones", scope="m_b")
        self.assertEqual(hits, [])
        hits = keyword.recall(self.conn, "keyboard", scope="m_a")
        self.assertEqual(hits, [])

    def test_scoped_recall_finds_own_facts(self):
        hits = keyword.recall(self.conn, "headphones price", scope="m_a")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["scope"], "m_a")

    def test_unscoped_recall_sees_everything(self):
        hits = keyword.recall(self.conn, "price", limit=10)
        self.assertEqual(len(hits), 2)
        self.assertEqual({h["scope"] for h in hits}, {"m_a", "m_b"})

    def test_default_scope_is_global(self):
        keyword.store_fact(self.conn, "A fact with no scope given")
        row = self.conn.execute(
            "SELECT scope FROM facts WHERE text LIKE '%no scope%'"
        ).fetchone()
        self.assertEqual(row["scope"], "global")
        hits = keyword.recall(self.conn, "scope given", scope="global")
        self.assertEqual(len(hits), 1)

    def test_get_scoped_summary_newest_first(self):
        keyword.store_fact(self.conn, "Oldest mission fact", scope="m_a")
        keyword.store_fact(self.conn, "Newest mission fact", scope="m_a")
        summary = keyword.get_scoped_summary(self.conn, "m_a", limit=2)
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["text"], "Newest mission fact")
        self.assertEqual(summary[1]["text"], "Oldest mission fact")
        self.assertIn("created_at", summary[0])

    def test_get_scoped_summary_excludes_other_scopes(self):
        summary = keyword.get_scoped_summary(self.conn, "m_a", limit=10)
        self.assertTrue(all(
            "headphones" in f["text"] for f in summary))
        self.assertEqual(len(summary), 1)


class TestScale(_TmpDb):
    def test_10k_recall_under_200ms(self):
        rows = gen_scale_facts(10_000)
        self.seed_raw(rows)
        self.assertEqual(self.count_facts(), 10_000)
        self.assertEqual(facts_fts.fts_count(self.conn), 10_000)
        # The old full-scan recall, for the record (not asserted —
        # it is the baseline the FTS path replaces).
        t0 = time.perf_counter()
        old_hits = _old_recall(self.conn, "headphones price")
        old_ms = (time.perf_counter() - t0) * 1000
        print(f"\n[scale] old recall on 10k facts: {old_ms:.1f}ms")
        cases = [
            ("headphones price", None),
            ("SDE matched fakecorp", None),
            ("price check", None),
            # The mission-realistic path: scope filters first.
            ("headphones price", "m_price_1"),
            ("SDE matched", "m_job_3"),
        ]
        for query, scope in cases:
            times = []
            hits = []
            for _ in range(5):
                t0 = time.perf_counter()
                hits = keyword.recall(self.conn, query, scope=scope)
                times.append(time.perf_counter() - t0)
            times.sort()
            median_ms = times[len(times) // 2] * 1000
            print(f"[scale] new recall {query!r} scope={scope}: "
                  f"{median_ms:.2f}ms")
            self.assertLess(median_ms, 200,
                            f"recall {query!r} too slow: {median_ms:.1f}ms")
            self.assertTrue(hits, f"no hits for {query!r}")
        # The FTS path finds the same top hit as the full scan.
        new_hits = keyword.recall(self.conn, "headphones price")
        self.assertEqual(new_hits[0]["id"], old_hits[0]["id"])
        self.assertIn("headphones", new_hits[0]["text"])


class TestCompaction(_TmpDb):
    def test_price_compaction_preserves_min_max_and_count(self):
        base = 1793000000
        rows, prices = gen_price_facts(500, "m_price", base, day_span=14)
        self.seed_raw(rows)
        result = compaction.compact_mission_facts(
            self.conn, "m_price", threshold=500, keep_recent=0)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["compacted"], 500)
        self.assertEqual(result["raw_after"], 0)
        # 500 raw facts -> 1 summary fact.
        self.assertEqual(self.count_facts("m_price"), 1)
        summary = result["summary_text"]
        lo, hi = min(prices), max(prices)
        self.assertIn(f"\u20b9{lo:,}", summary)
        self.assertIn(f"\u20b9{hi:,}", summary)
        self.assertIn("500 checks", summary)
        self.assertIn("2 weeks", summary)
        row = self.conn.execute(
            "SELECT topic, source FROM facts WHERE scope = 'm_price'"
        ).fetchone()
        self.assertEqual(row["topic"], "summary")
        self.assertEqual(row["source"], "compaction")

    def test_default_keep_recent_keeps_newest_raw_facts(self):
        base = 1793000000
        rows, prices = gen_price_facts(500, "m_price", base, day_span=14)
        self.seed_raw(rows)
        result = compaction.compact_mission_facts(self.conn, "m_price")
        self.assertTrue(result["triggered"])
        self.assertEqual(result["compacted"], 400)
        self.assertEqual(result["raw_after"], 100)
        self.assertEqual(self.count_facts("m_price"), 101)
        # The summary covers the OLDEST 400 only.
        lo, hi = min(prices[:400]), max(prices[:400])
        self.assertIn(f"\u20b9{lo:,}", result["summary_text"])
        self.assertIn(f"\u20b9{hi:,}", result["summary_text"])
        self.assertIn("400 checks", result["summary_text"])
        # The surviving raw facts are the newest 100.
        newest = self.conn.execute(
            "SELECT MIN(created_at) AS c FROM facts"
            " WHERE scope = 'm_price' AND topic <> 'summary'"
        ).fetchone()["c"]
        self.assertEqual(newest, base + 400 * (14 * 86400 // 500))

    def test_below_threshold_is_noop(self):
        rows, _ = gen_price_facts(100, "m_price", 1793000000)
        self.seed_raw(rows)
        result = compaction.compact_mission_facts(self.conn, "m_price")
        self.assertFalse(result["triggered"])
        self.assertEqual(result["compacted"], 0)
        self.assertEqual(self.count_facts("m_price"), 100)

    def test_role_compaction_counts_outcomes(self):
        base = 1793000000
        rows = []
        for i in range(60):
            outcome = OUTCOMES[i % 5]  # 12 of each of 5 outcomes
            rows.append((
                f"fact-r{i:04x}",
                f"Role seen: SDE at fakecorp \u2014 {outcome} (ref {i})",
                "mission", "role", "m_job", base + i * 3600,
            ))
        self.seed_raw(rows)
        result = compaction.compact_mission_facts(
            self.conn, "m_job", threshold=50, keep_recent=0)
        self.assertTrue(result["triggered"])
        self.assertEqual(self.count_facts("m_job"), 1)
        summary = result["summary_text"]
        self.assertIn("Roles seen (60)", summary)
        for outcome in OUTCOMES[:5]:
            self.assertIn(f"12 {outcome}", summary)

    def test_generic_compaction_falls_back_to_keywords(self):
        base = 1793000000
        rows = []
        for i in range(60):
            rows.append((
                f"fact-g{i:04x}",
                f"Observation {i}: gateway node restarted after "
                f"maintenance window in region {i % 3}",
                "mission", "check", "m_ops", base + i * 3600,
            ))
        self.seed_raw(rows)
        result = compaction.compact_mission_facts(
            self.conn, "m_ops", threshold=50, keep_recent=0)
        self.assertTrue(result["triggered"])
        summary = result["summary_text"]
        self.assertIn("60 observations", summary)
        self.assertIn("gateway", summary)

    def test_second_compaction_is_noop(self):
        rows, _ = gen_price_facts(500, "m_price", 1793000000)
        self.seed_raw(rows)
        compaction.compact_mission_facts(
            self.conn, "m_price", threshold=500, keep_recent=0)
        result = compaction.compact_mission_facts(
            self.conn, "m_price", threshold=500, keep_recent=0)
        self.assertFalse(result["triggered"])
        self.assertEqual(self.count_facts("m_price"), 1)

    def test_compaction_leaves_other_missions_alone(self):
        rows, _ = gen_price_facts(500, "m_price", 1793000000)
        other = [("fact-x", "Unrelated fact", "mission", "note",
                  "m_other", 1793000000)]
        self.seed_raw(rows + other)
        compaction.compact_mission_facts(
            self.conn, "m_price", threshold=500, keep_recent=0)
        self.assertEqual(self.count_facts("m_other"), 1)
        self.assertEqual(self.count_facts("m_price"), 1)

    def test_summary_is_recallable(self):
        rows, _ = gen_price_facts(500, "m_price", 1793000000)
        self.seed_raw(rows)
        compaction.compact_mission_facts(
            self.conn, "m_price", threshold=500, keep_recent=0)
        hits = keyword.recall(self.conn, "price checks headphones",
                              scope="m_price")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["topic"], "summary")


class TestBudgets(_TmpDb):
    def test_enforce_budget_compacts_oldest_down_to_cap(self):
        base = 1793000000
        rows, _ = gen_price_facts(600, "m_price", base, day_span=14)
        self.seed_raw(rows)
        result = budgets.enforce_fact_budget(self.conn, "m_price", 100)
        self.assertEqual(result["before"], 600)
        self.assertEqual(result["after"], 100)
        self.assertEqual(result["compacted"], 501)
        self.assertEqual(result["summaries_created"], 1)
        # 99 newest raw facts + 1 summary; nothing deleted raw.
        raw = self.conn.execute(
            "SELECT COUNT(*) AS c FROM facts"
            " WHERE scope = 'm_price' AND topic <> 'summary'"
        ).fetchone()["c"]
        self.assertEqual(raw, 99)
        summaries = self.conn.execute(
            "SELECT COUNT(*) AS c FROM facts"
            " WHERE scope = 'm_price' AND topic = 'summary'"
        ).fetchone()["c"]
        self.assertEqual(summaries, 1)

    def test_budget_already_under_cap_is_noop(self):
        rows, _ = gen_price_facts(50, "m_price", 1793000000)
        self.seed_raw(rows)
        result = budgets.enforce_fact_budget(self.conn, "m_price", 100)
        self.assertEqual(result["before"], 50)
        self.assertEqual(result["after"], 50)
        self.assertEqual(result["compacted"], 0)

    def test_budget_merges_summaries_when_raw_runs_out(self):
        # Compact down to summaries only, then squeeze further:
        # the meta-summary path must kick in, never a raw delete.
        base = 1793000000
        rows, _ = gen_price_facts(500, "m_price", base, day_span=14)
        self.seed_raw(rows)
        compaction.compact_mission_facts(
            self.conn, "m_price", threshold=500, keep_recent=0)
        # One summary now; add 9 more summaries by compacting
        # fresh batches, then enforce a cap of 5.
        for batch in range(9):
            extra = []
            for i in range(60):
                price = 5000 + batch * 100 + i
                extra.append((
                    f"fact-b{batch}x{i:03x}",
                    f"Price check: mouse at \u20b9{price:,} on flipkart",
                    "mission", "price", "m_price",
                    base + 2000000 + batch * 100000 + i * 60,
                ))
            self.seed_raw(extra)
            compaction.compact_mission_facts(
                self.conn, "m_price", threshold=50, keep_recent=0)
        self.assertEqual(self.count_facts("m_price"), 10)
        result = budgets.enforce_fact_budget(self.conn, "m_price", 5)
        self.assertLessEqual(result["after"], 5)
        self.assertGreaterEqual(result["after"], 1)
        self.assertGreater(result["compacted"], 0)

    def test_budget_rejects_zero_cap(self):
        with self.assertRaises(ValueError):
            budgets.enforce_fact_budget(self.conn, "m_price", 0)

    def test_expire_stale_deletes_old_observations_only(self):
        now = int(time.time())
        day = 86400
        rows = [
            ("fact-old1", "Old price check headphones", "mission",
             "price", "m_a", now - 100 * day),
            ("fact-old2", "Old role seen SDE", "mission",
             "role", "m_a", now - 120 * day),
            ("fact-new1", "Recent price check keyboard", "mission",
             "price", "m_a", now - 10 * day),
            ("fact-prof1", "User lives in Hyderabad", "user",
             "profile", "profile", now - 400 * day),
            ("fact-prof2", "User prefers remote roles", "user",
             "profile", "global", now - 400 * day),
            ("fact-prof3", "User location note", "user",
             "note", "profile", now - 400 * day),
            ("fact-sum1", "[summary] Price checks (500 checks)",
             "compaction", "summary", "m_a", now - 400 * day),
        ]
        self.seed_raw(rows)
        deleted = budgets.expire_stale_facts(self.conn, ttl_days=90)
        self.assertEqual(deleted, 2)
        remaining = {
            r["id"] for r in
            self.conn.execute("SELECT id FROM facts").fetchall()
        }
        self.assertEqual(remaining, {
            "fact-new1", "fact-prof1", "fact-prof2", "fact-prof3",
            "fact-sum1",
        })

    def test_expire_scoped_to_one_mission(self):
        now = int(time.time())
        day = 86400
        rows = [
            ("fact-a1", "Old fact mission A", "mission", "price",
             "m_a", now - 100 * day),
            ("fact-b1", "Old fact mission B", "mission", "price",
             "m_b", now - 100 * day),
        ]
        self.seed_raw(rows)
        deleted = budgets.expire_stale_facts(
            self.conn, mission_id="m_a", ttl_days=90)
        self.assertEqual(deleted, 1)
        self.assertEqual(self.count_facts("m_b"), 1)
        self.assertEqual(self.count_facts("m_a"), 0)

    def test_expire_keeps_index_in_sync(self):
        now = int(time.time())
        self.seed_raw([(
            "fact-old", "Old price check headphones", "mission",
            "price", "m_a", now - 100 * 86400,
        )])
        self.assertEqual(facts_fts.fts_count(self.conn), 1)
        budgets.expire_stale_facts(self.conn, ttl_days=90)
        self.assertEqual(facts_fts.fts_count(self.conn), 0)
        self.assertEqual(keyword.recall(self.conn, "headphones"), [])


if __name__ == "__main__":
    unittest.main()
