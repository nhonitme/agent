"""Offline API tests for skipped invoice source notices."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_agent as agent


NOTICE = "Nếu quý khách hàng không xuất loại hóa đơn này thì có thể bỏ qua"
ITEM = {"khhdon": "C26TAA", "nbmst": "000", "shdon": "1", "khmshdon": "1"}


class InvoiceListWarningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        agent._gdt_sem = asyncio.Semaphore(10)
        app = web.Application()
        app.router.add_post("/fetch-invoice-list", agent.handle_fetch_invoice_list)
        app.router.add_post("/fetch-all-invoice", agent.handle_fetch_all_invoice)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def post(self, endpoint, inv_type=1):
        response = await self.client.post(endpoint, json={
            "username": "warning-test", "password": "test",
            "date_from": "20260101", "date_to": "20260131", "inv_type": inv_type,
        })
        return response.status, await response.json()

    async def test_both_endpoints_return_pos_skip_notice_and_retained_items(self):
        async def fake_get(url, *args, **kwargs):
            if "/sco-query/" in url:
                raise agent._GDTTransportError("Simulated retry exhaustion")
            return {"datas": [ITEM], "state": ""}

        async def fake_details(username, password, items, action):
            return [{**item, "detail": {"ok": True}} for item in items], 0

        for endpoint in ("/fetch-invoice-list", "/fetch-all-invoice"):
            for inv_type in (0, 1):
                with self.subTest(endpoint=endpoint, inv_type=inv_type):
                    with patch.object(agent, "_gdt_get", new=fake_get), \
                            patch.object(agent, "_fetch_invoice_details_core", new=fake_details):
                        status, payload = await self.post(endpoint, inv_type)
                    self.assertEqual(status, 200)
                    self.assertTrue(payload["success"])
                    self.assertEqual(payload["count"], 1)
                    self.assertEqual(payload["items"][0]["shdon"], ITEM["shdon"])
                    self.assertEqual(len(payload["skipped_sources"]), 1)
                    warning = payload["skipped_sources"][0]
                    source = "purchase" if inv_type == 0 else "sold"
                    self.assertEqual(warning["source"], "sco-query/invoices/" + source)
                    self.assertEqual(warning["reason_code"], "GDT_TRANSPORT_ERROR")
                    self.assertEqual(warning["type_search"], 8 if inv_type == 0 else None)
                    self.assertIn(NOTICE, warning["message"])
                    self.assertIn(warning["source_name"], warning["message"])
                    self.assertIn(warning["message"], payload["message"])

    async def test_business_error_on_later_page_reports_source_and_keeps_prior_page(self):
        async def fake_get(url, *args, **kwargs):
            if "/sco-query/" in url:
                return {"datas": [], "state": ""}
            if "state=" in url:
                return {"path": "/invoices/sold", "timestamp": "test", "message": "GDT error"}
            return {"datas": [ITEM], "state": "next-page"}

        with patch.object(agent, "_gdt_get", new=fake_get):
            status, payload = await self.post("/fetch-invoice-list")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [ITEM])
        warning = payload["skipped_sources"][0]
        self.assertEqual(warning["source"], "query/invoices/sold")
        self.assertEqual(warning["page"], 2)
        self.assertEqual(warning["reason_code"], "GDT_RESPONSE_ERROR")
        self.assertIn(NOTICE, payload["message"])

    async def test_other_skipped_source_reports_type_search_and_continues(self):
        async def fake_get(url, *args, **kwargs):
            if "ttxly==5" in url:
                raise RuntimeError("Simulated source error")
            return {"datas": [ITEM], "state": ""}

        with patch.object(agent, "_gdt_get", new=fake_get):
            status, payload = await self.post("/fetch-invoice-list", inv_type=0)
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        warning = payload["skipped_sources"][0]
        self.assertEqual(warning["source"], "query/invoices/purchase")
        self.assertEqual(warning["type_search"], 5)
        self.assertEqual(warning["reason_code"], "GDT_REQUEST_ERROR")
        self.assertIn(NOTICE, payload["message"])

    async def test_no_skipped_sources_returns_empty_list_without_skip_notice(self):
        for endpoint in ("/fetch-invoice-list", "/fetch-all-invoice"):
            with self.subTest(endpoint=endpoint):
                with patch.object(agent, "_gdt_get", new=AsyncMock(return_value={"datas": []})), \
                        patch.object(agent, "_fetch_invoice_details_core", new=AsyncMock(return_value=([], 0))):
                    status, payload = await self.post(endpoint)
                self.assertEqual(status, 200)
                self.assertEqual(payload["skipped_sources"], [])
                self.assertNotIn(NOTICE, payload["message"])

    async def test_skip_notice_survives_partial_details_or_detail_exception(self):
        async def fake_get(url, *args, **kwargs):
            if "/sco-query/" in url:
                raise agent._GDTTransportError("Simulated retry exhaustion")
            return {"datas": [ITEM], "state": ""}

        for detail_error in (False, True):
            with self.subTest(detail_error=detail_error):
                details = (AsyncMock(side_effect=RuntimeError("Simulated detail error"))
                           if detail_error else AsyncMock(return_value=([{**ITEM, "detail": None}], 1)))
                with patch.object(agent, "_gdt_get", new=fake_get), \
                        patch.object(agent, "_fetch_invoice_details_core", new=details):
                    status, payload = await self.post("/fetch-all-invoice")
                self.assertEqual(status, 500 if detail_error else 207)
                self.assertFalse(payload["success"])
                self.assertEqual(len(payload["skipped_sources"]), 1)
                self.assertIn(NOTICE, payload["message"])


if __name__ == "__main__":
    unittest.main()
