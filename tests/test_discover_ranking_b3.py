"""B3 Chaturbate multi-page ranking adapter — local bypass only (no /api/discover)."""

from __future__ import annotations

import ast
import asyncio
import unittest
from pathlib import Path

from app.services.discover_ranking import (
    DiscoverRankingService,
    RankingPoolBudget,
    RankingMode,
    slice_pool,
)
from app.services.discover_ranking_chaturbate import (
    B3_MAX_PAGES,
    B3_MAX_REQUESTS,
    B3_TIMEOUT_SECONDS,
    ChaturbateRankingAdapterError,
    attach_chaturbate_num_users_evidence,
    build_chaturbate_ranking_pool,
    clamp_b3_budget,
    make_chaturbate_page_fetcher,
    model_from_roomlist_room,
    wrap_chaturbate_api_roomlist,
)
from app.services.discover_ranking_types import annotate_model_viewer_fields


ROOT = Path(__file__).resolve().parents[1]


def _room(username: str, num_users: int, **extra):
    room = {
        "username": username,
        "display_name": username,
        "num_users": num_users,
        "current_show": "public",
        "tags": extra.pop("tags", []),
        "room_subject": extra.pop("subject", ""),
        "gender": extra.pop("gender", "f"),
    }
    room.update(extra)
    return room


class FixtureRoomlist:
    """limit/offset multipage roomlist fixture (no network)."""

    def __init__(self, pages: dict[int, list]):
        self.pages = pages
        self.calls: list[tuple[int, int, int]] = []

    async def __call__(self, page: int, limit: int, **kwargs):
        offset = int(kwargs.get("offset", (page - 1) * limit))
        self.calls.append((page, limit, offset))
        batch = list(self.pages.get(page, []))
        return {"rooms": batch[:limit]}


class FakeChaturbateAPI:
    def __init__(self, pages: dict[int, list], *, fail_on_page: int | None = None):
        self.pages = pages
        self.fail_on_page = fail_on_page
        self.calls: list[tuple[int, int]] = []

    async def get_live_models(self, page=1, limit=24, gender="", search="", tag=""):
        self.calls.append((page, limit))
        if self.fail_on_page is not None and page == self.fail_on_page:
            raise RuntimeError("upstream boom")
        rooms = self.pages.get(page, [])
        # Mimic production parser: viewers from num_users, drop num_users key.
        models = []
        for room in rooms[:limit]:
            models.append(
                {
                    "username": room["username"],
                    "display_name": room["username"],
                    "viewers": room["num_users"],
                    "is_online": True,
                    "room_status": "public",
                    "tags": [],
                    "subject": "",
                    "gender": "f",
                }
            )
        return {
            "models": models,
            "total": sum(len(v) for v in self.pages.values()),
            "page": page,
            "limit": limit,
            "total_pages": max(1, len(self.pages)),
        }


class EvidenceTests(unittest.TestCase):
    def test_num_users_exact_evidence_preserved(self):
        model = model_from_roomlist_room(_room("alice", 42))
        self.assertIsNotNone(model)
        self.assertEqual(42, model["num_users"])
        self.assertEqual("num_users", model["viewer_count_source"])
        self.assertEqual("exact", model["viewer_count_precision_hint"])
        self.assertTrue(model["viewer_count_present"])
        annotate_model_viewer_fields(model)
        self.assertEqual(42, model["viewer_count"])
        self.assertEqual("exact", model["viewer_count_precision"])
        self.assertTrue(model["viewer_count_reliable"])

    def test_attach_helper_sets_b11_fields(self):
        m = attach_chaturbate_num_users_evidence(
            {"username": "bob", "num_users": 7, "viewers": 7}
        )
        self.assertEqual("num_users", m["viewer_count_source"])
        self.assertEqual("exact", m["viewer_count_precision_hint"])


class AdapterMultipageTests(unittest.IsolatedAsyncioTestCase):
    def _three_pages(self):
        # Page1 local max 40; page2 has global high 500 — classic page_local defect.
        return {
            1: [
                _room("p1_a", 40),
                _room("p1_b", 30),
                _room("dup_user", 10),
            ],
            2: [
                _room("p2_high", 500),
                _room("dup_user", 10),
                _room("p2_c", 20),
            ],
            3: [
                _room("p3_a", 5),
            ],
        }

    async def test_limit_offset_multipage_adapter_calls(self):
        src = FixtureRoomlist(self._three_pages())
        fetch = make_chaturbate_page_fetcher(src)
        page1 = await fetch(1, 3)
        page2 = await fetch(2, 3)
        self.assertEqual([(1, 3, 0), (2, 3, 3)], src.calls)
        self.assertEqual("p1_a", page1[0]["username"])
        self.assertEqual(500, page2[0]["num_users"])

    async def test_page2_high_enters_global_top(self):
        src = FixtureRoomlist(self._three_pages())
        service = DiscoverRankingService(ttl_seconds=60)
        snap = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=make_chaturbate_page_fetcher(src),
            budget=RankingPoolBudget(max_pages=3, max_requests=3, timeout_seconds=5),
            page_size=3,
        )
        names = [m["username"] for m in snap.models]
        self.assertEqual("p2_high", names[0])
        self.assertEqual(500, snap.models[0]["viewer_count"])
        self.assertEqual("exact", snap.models[0]["viewer_count_precision"])
        self.assertEqual(RankingMode.MULTI_PAGE_GLOBAL.value, snap.ranking_mode)
        self.assertTrue(snap.pool_id.startswith("pl_"))

        # Contrast: page1-only local top would be p1_a, not p2_high.
        page1_only = [m["username"] for m in (await make_chaturbate_page_fetcher(src)(1, 3))]
        self.assertEqual("p1_a", page1_only[0])
        self.assertNotIn("p2_high", page1_only)

    async def test_cross_page_stable_id_dedupe(self):
        src = FixtureRoomlist(self._three_pages())
        service = DiscoverRankingService(ttl_seconds=60)
        snap = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=make_chaturbate_page_fetcher(src),
            budget=RankingPoolBudget(max_pages=3, max_requests=3),
            page_size=3,
        )
        ids = [m["stable_id"] for m in snap.models]
        self.assertEqual(1, ids.count("chaturbate:dup_user"))
        self.assertEqual(len(ids), len(set(ids)))

    async def test_snapshot_slice_no_dup_and_stable_order(self):
        pages = {
            1: [_room(f"u{i}", 100 - i) for i in range(4)],
            2: [_room("tail", 1)],
        }
        src = FixtureRoomlist(pages)
        service = DiscoverRankingService(ttl_seconds=60)
        snap = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=make_chaturbate_page_fetcher(src),
            budget=RankingPoolBudget(max_pages=2, max_requests=2),
            page_size=4,
        )
        s1 = slice_pool(snap, page=1, limit=2)
        s2 = slice_pool(snap, page=2, limit=2)
        ids1 = {m["stable_id"] for m in s1["models"]}
        ids2 = {m["stable_id"] for m in s2["models"]}
        self.assertEqual(set(), ids1 & ids2)

        again = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=make_chaturbate_page_fetcher(src),
            budget=RankingPoolBudget(max_pages=2, max_requests=2),
            page_size=4,
        )
        self.assertEqual(snap.pool_id, again.pool_id)
        self.assertEqual(
            [m["stable_id"] for m in snap.models],
            [m["stable_id"] for m in again.models],
        )

    async def test_budget_partial_max_pages(self):
        src = FixtureRoomlist(self._three_pages())
        service = DiscoverRankingService(ttl_seconds=60)
        snap = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=make_chaturbate_page_fetcher(src),
            budget=RankingPoolBudget(max_pages=2, max_requests=3, timeout_seconds=5),
            page_size=3,
        )
        self.assertFalse(snap.is_complete)
        self.assertEqual("max_pages", snap.partial_reason)
        self.assertIsNotNone(snap.partial_reason)

    async def test_timeout_partial(self):
        async def slow_page(page, limit, **kwargs):
            await asyncio.sleep(0.2)
            return {"rooms": [_room("late", 1)]}

        service = DiscoverRankingService(ttl_seconds=60)
        snap = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=make_chaturbate_page_fetcher(slow_page),
            budget=RankingPoolBudget(max_pages=3, max_requests=3, timeout_seconds=0.05),
            page_size=1,
        )
        self.assertFalse(snap.is_complete)
        self.assertEqual("timeout", snap.partial_reason)

    async def test_budget_clamp_respects_b3_caps(self):
        capped = clamp_b3_budget(
            RankingPoolBudget(max_pages=99, max_requests=99, timeout_seconds=99)
        )
        self.assertEqual(B3_MAX_PAGES, capped.max_pages)
        self.assertEqual(B3_MAX_REQUESTS, capped.max_requests)
        self.assertEqual(B3_TIMEOUT_SECONDS, capped.timeout_seconds)

    async def test_api_wrap_restores_num_users_and_ranks(self):
        api = FakeChaturbateAPI(self._three_pages())
        service = DiscoverRankingService(ttl_seconds=60)
        snap = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=wrap_chaturbate_api_roomlist(api),
            budget=RankingPoolBudget(max_pages=3, max_requests=3),
            page_size=3,
        )
        self.assertEqual([(1, 3), (2, 3), (3, 3)], api.calls)
        self.assertEqual("p2_high", snap.models[0]["username"])
        self.assertEqual("exact", snap.models[0]["viewer_count_precision"])
        self.assertEqual(500, snap.models[0]["num_users"])

    async def test_adapter_failure_controlled(self):
        api = FakeChaturbateAPI(self._three_pages(), fail_on_page=1)
        service = DiscoverRankingService(ttl_seconds=60)
        with self.assertRaises(ChaturbateRankingAdapterError):
            await build_chaturbate_ranking_pool(
                service=service,
                fetch_page=wrap_chaturbate_api_roomlist(api),
                budget=RankingPoolBudget(max_pages=1, max_requests=1),
                page_size=3,
            )

    async def test_invalid_payload_controlled(self):
        async def bad_page(page, limit, **kwargs):
            return "not-a-payload"

        fetch = make_chaturbate_page_fetcher(bad_page)
        with self.assertRaises(ChaturbateRankingAdapterError):
            await fetch(1, 2)


class ProductionIsolationTests(unittest.TestCase):
    """Prove B3 / B2 ranking service is not on the production Discover path."""

    FORBIDDEN_MODULES = {
        "discover_ranking_chaturbate",
        "app.services.discover_ranking_chaturbate",
    }
    # Production may use discover_ranking_types (B1); must not use the pool service.
    FORBIDDEN_POOL_IMPORTS = {
        "discover_ranking",
        "app.services.discover_ranking",
    }

    def _imported_names(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names.add(mod)
                for alias in node.names:
                    names.add(f"{mod}.{alias.name}" if mod else alias.name)
                    names.add(alias.name)
        return names

    def test_discover_api_does_not_import_b3_or_pool_service(self):
        path = ROOT / "app" / "api" / "discover.py"
        names = self._imported_names(path)
        for bad in self.FORBIDDEN_MODULES:
            self.assertNotIn(bad, names)
        # B4 may import discover_ranking_wire; must not import pool/adapter directly.
        self.assertNotIn("discover_ranking", names)
        self.assertNotIn("..services.discover_ranking", names)
        joined = " ".join(sorted(names))
        self.assertNotIn("discover_ranking_chaturbate", joined)
        self.assertIn("discover_ranking_types", joined)
        self.assertIn("discover_ranking_wire", joined)

    def test_no_production_app_file_imports_b3(self):
        app_root = ROOT / "app"
        allowed_ranking_owners = {
            "discover_ranking_chaturbate.py",
            "discover_ranking.py",
            "discover_ranking_wire.py",
            "discover_ranking_providers.py",
        }
        offenders = []
        for path in app_root.rglob("*.py"):
            if path.name in allowed_ranking_owners:
                continue
            text = path.read_text(encoding="utf-8")
            if "discover_ranking_chaturbate" in text:
                offenders.append(str(path.relative_to(ROOT)))
            # Pool service import outside ranking modules / wire / tests.
            if path.name == "discover_ranking_types.py":
                continue
            if "from .discover_ranking import" in text or "from ..services.discover_ranking import" in text:
                offenders.append(f"pool-import:{path.relative_to(ROOT)}")
        self.assertEqual([], offenders)

    def test_b3_adapter_does_not_own_frontend_pool_ui(self):
        # B5 owns discover.js sort/pool_id wiring; B3 adapter stays server-side only.
        adapter = (ROOT / "app" / "services" / "discover_ranking_chaturbate.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("selectedSortMode", adapter)
        self.assertNotIn("rankingPoolId", adapter)
        self.assertNotIn("renderSortControls", adapter)


if __name__ == "__main__":
    unittest.main()
