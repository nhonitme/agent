"""Offline regression tests for optional POS invoice sources."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_agent as agent


class InvoiceListTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        agent._gdt_sem = asyncio.Semaphore(10)

    async def test_pos_transport_failure_after_three_attempts_keeps_other_sources(self):
        for inv_type in (0, 1):
            for action in ("fetch-invoice-list", "fetch-all-invoice"):
                with self.subTest(inv_type=inv_type, action=action):
                    expected = []
                    pos_attempts = 0
                    skipped_sources = []

                    async def fake_read(url, headers):
                        nonlocal pos_attempts
                        if "/sco-query/" in url:
                            pos_attempts += 1
                            raise agent.RequestException("Simulated POS connection failure")
                        item = {
                            "khhdon": "C26TAA", "nbmst": "000",
                            "shdon": str(len(expected) + 1), "khmshdon": "1",
                        }
                        expected.append(item)
                        return 200, {"datas": [item], "state": ""}

                    with patch.object(agent, "_get_token", new=AsyncMock(return_value="token")), \
                            patch.object(agent, "_read_gdt_json", new=fake_read), \
                            patch.object(agent.asyncio, "sleep", new=AsyncMock()), \
                            self.assertLogs(agent.log, level="WARNING") as logs:
                        items = await agent._fetch_invoice_list_core(
                            "demo", "pass", "20260101", "20260131", inv_type, action,
                            skipped_sources=skipped_sources,
                        )

                    self.assertEqual(pos_attempts, 3)
                    self.assertEqual(len(expected), 2 if inv_type == 0 else 1)
                    self.assertEqual(items, expected)
                    self.assertEqual(len(skipped_sources), 1)
                    self.assertEqual(skipped_sources[0]["reason_code"], "GDT_TRANSPORT_ERROR")
                    self.assertTrue(any("Bỏ qua nguồn máy tính tiền" in line for line in logs.output))
                    self.assertEqual(agent._progress["demo"]["source_index"], 3 if inv_type == 0 else 2)

    async def test_standard_source_transport_failure_still_propagates(self):
        for inv_type in (0, 1):
            with self.subTest(inv_type=inv_type):
                reader = AsyncMock(side_effect=agent.RequestException("Simulated connection failure"))
                with patch.object(agent, "_get_token", new=AsyncMock(return_value="token")), \
                        patch.object(agent, "_read_gdt_json", new=reader), \
                        patch.object(agent.asyncio, "sleep", new=AsyncMock()):
                    with self.assertRaises(agent._GDTTransportError):
                        await agent._fetch_invoice_list_core(
                            "demo", "pass", "20260101", "20260131", inv_type,
                            "fetch-invoice-list",
                        )
                self.assertEqual(reader.await_count, 3)

    async def test_successful_pos_source_is_included(self):
        standard = {"khhdon": "C26TAA", "nbmst": "000", "shdon": "1", "khmshdon": "1"}
        pos = {"khhdon": "C26MAA", "nbmst": "000", "shdon": "2", "khmshdon": "1"}

        async def fake_read(url, headers):
            return 200, {"datas": [pos if "/sco-query/" in url else standard], "state": ""}

        with patch.object(agent, "_get_token", new=AsyncMock(return_value="token")), \
                patch.object(agent, "_read_gdt_json", new=fake_read):
            items = await agent._fetch_invoice_list_core(
                "demo", "pass", "20260101", "20260131", 1, "fetch-invoice-list"
            )
        self.assertEqual(items, [standard, pos])


if __name__ == "__main__":
    unittest.main()
