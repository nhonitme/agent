"""Offline regression tests for GDT detail retry and worker behavior."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_agent as agent


def _items(count):
    return [
        {"khhdon": "C26TAA", "nbmst": "000", "shdon": str(i),
         "khmshdon": "1"}
        for i in range(count)
    ]


class DetailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old = {
            name: getattr(agent, name)
            for name in (
                "DETAIL_WORKERS", "DETAIL_MAX_ATTEMPTS", "DETAIL_REQUEST_TIMEOUT",
                "DETAIL_429_COOLDOWN", "DETAIL_429_ABORT_AFTER",
                "DETAIL_BATCH_TIMEOUT",
            )
        }
        agent.DETAIL_WORKERS = 3
        agent.DETAIL_MAX_ATTEMPTS = 2
        agent.DETAIL_REQUEST_TIMEOUT = 0.02
        agent.DETAIL_429_COOLDOWN = 0.01
        agent.DETAIL_429_ABORT_AFTER = 4
        agent.DETAIL_BATCH_TIMEOUT = 5
        agent._gdt_sem = asyncio.Semaphore(10)
        agent._token_cache.clear()

    async def asyncTearDown(self):
        for name, value in self.old.items():
            setattr(agent, name, value)

    async def test_parallel_success_keeps_input_order_and_bounds_workers(self):
        active = 0
        peak = 0

        async def fake_get(url, username, password, gate):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            shdon = parse_qs(urlsplit(url).query)["shdon"][0]
            await asyncio.sleep(0.005 * (5 - int(shdon) % 5))
            active -= 1
            return {"value": shdon}

        with patch.object(agent, "_gdt_get_detail", new=fake_get):
            results, failed = await agent._fetch_invoice_details_core(
                "demo", "pass", _items(12), "fetch-invoice-details"
            )

        self.assertEqual(failed, 0)
        self.assertEqual(
            [row["detail"]["value"] for row in results],
            [str(i) for i in range(12)],
        )
        self.assertLessEqual(peak, 3)
        self.assertGreater(peak, 1)

    async def test_timeout_only_fails_one_item(self):
        async def slow_read(url, headers):
            await asyncio.sleep(0.5)
            return 200, {"ok": True}

        with patch.object(agent, "_get_token", new=AsyncMock(return_value="token")), \
                patch.object(agent, "_read_gdt_json", new=slow_read):
            rows, failed = await agent._fetch_invoice_details_core(
                "demo", "pass", _items(1), "fetch-invoice-details"
            )
        self.assertEqual(failed, 1)
        self.assertIsNone(rows[0]["detail"])
        self.assertEqual(rows[0]["detail_error_code"], "DETAIL_FETCH_FAILED")

    async def test_429_retries_once_with_shared_gate(self):
        responses = [(429, None), (200, {"value": 42})]

        async def fake_read(url, headers):
            return responses.pop(0)

        with patch.object(agent, "_get_token", new=AsyncMock(return_value="token")), \
                patch.object(agent, "_read_gdt_json", new=fake_read):
            gate = agent._DetailRateGate()
            data = await agent._gdt_get_detail(
                "https://example.invalid/details", "demo", "pass", gate
            )
        self.assertEqual(data["value"], 42)
        self.assertTrue(gate.rate_limited)
        self.assertFalse(gate.paused)

    async def test_repeated_429_aborts_remaining_items(self):
        requests = 0

        async def always_429(url, headers):
            nonlocal requests
            requests += 1
            return 429, None

        with patch.object(agent, "_get_token", new=AsyncMock(return_value="token")), \
                patch.object(agent, "_read_gdt_json", new=always_429):
            rows, failed = await agent._fetch_invoice_details_core(
                "demo", "pass", _items(20), "fetch-invoice-details"
            )
        self.assertEqual(failed, 20)
        self.assertTrue(all(row["detail"] is None for row in rows))
        self.assertTrue(
            any(row.get("detail_error_code") == "GDT_RATE_LIMITED" for row in rows)
        )
        self.assertLess(requests, 20)

    async def test_successes_are_preserved_when_one_detail_fails(self):
        async def fake_get(url, username, password, gate):
            shdon = parse_qs(urlsplit(url).query)["shdon"][0]
            if shdon == "2":
                raise RuntimeError("Simulated GDT timeout")
            return {"value": shdon}

        with patch.object(agent, "_gdt_get_detail", new=fake_get):
            rows, failed = await agent._fetch_invoice_details_core(
                "demo", "pass", _items(5), "fetch-invoice-details"
            )
        self.assertEqual(failed, 1)
        self.assertEqual([r["shdon"] for r in rows], [str(i) for i in range(5)])
        self.assertEqual(rows[0]["detail"]["value"], "0")
        self.assertIsNone(rows[2]["detail"])
        self.assertEqual(rows[4]["detail"]["value"], "4")

    async def test_batch_deadline_returns_partial_instead_of_hanging(self):
        agent.DETAIL_BATCH_TIMEOUT = 0.02

        async def very_slow_get(url, username, password, gate):
            await asyncio.sleep(10)
            return {"value": 1}

        with patch.object(agent, "_gdt_get_detail", new=very_slow_get):
            rows, failed = await agent._fetch_invoice_details_core(
                "demo", "pass", _items(5), "fetch-invoice-details"
            )
        self.assertEqual(failed, 5)
        self.assertEqual(len(rows), 5)
        self.assertTrue(
            all(r.get("detail_error_code") == "DETAIL_BATCH_TIMEOUT" for r in rows)
        )


if __name__ == "__main__":
    unittest.main()
