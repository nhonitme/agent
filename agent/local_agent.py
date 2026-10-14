"""
Local Agent — GDT proxy thuần túy.
Không phụ thuộc server trung tâm. Không lưu DB.
AMNote truyền credentials mỗi request, agent gọi GDT bằng IP local, trả dữ liệu thô về.
AMNote tự lưu DB theo logic sẵn có.

Endpoints:
  GET  /health              — kiểm tra agent còn sống
  GET  /progress            — tiến độ kéo dữ liệu theo từng username
  POST /fetch-all-invoice   — lấy toàn bộ HĐ kèm chi tiết (1 lần gọi duy nhất)
  POST /fetch-invoice-list  — chỉ lấy danh sách summary hóa đơn
  POST /fetch-invoice-details — lấy detail theo items từ API danh sách
  GET  /progress/invoice-list — tiến độ API danh sách
  GET  /progress/invoice-details — tiến độ API chi tiết

Mọi request chỉ được chấp nhận từ localhost.

Body /fetch-all-invoice:
  {
    "username":  "0123456789",
    "password":  "mat_khau_gdt",
    "date_from": "20260101",
    "date_to":   "20260731",
    "inv_type":  1,          // 0 = mua vào, 1 = bán ra
    "Lag":       "VIET"      // VIET | ENG | KOR | CHINA (mặc định VIET)
  }
  Trả về: {
    "success": true,
    "count":   120,
    "items": [
      { ...summary..., "detail": { ...chi tiết GDT... } },
      ...
    ]
  }

GET /progress?username=0123456789
  Trả về: {
    "0123456789": {
      "status":     "fetching_detail",
      "action":     "fetch-all-invoice",
      "phase":      "detail",
      "total_found": 120,
      "detail_done": 47,
      "detail_total": 120,
      "detail_failed": 0,
      "message":    "Chi tiết 47/120...",
      "updated_at": "2026-07-22T14:43:34"
    }
  }

config.env:
  AGENT_HOST=127.0.0.1
  AGENT_PORT=8099
  MAX_CONCURRENT=10
  MAX_HTTP_CONCURRENT=10
  MAX_DETAIL_ITEMS=5000
  MAX_REQUEST_BODY_MB=16
  GDT_PAGE_SIZE=30
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import sys
import threading
import uuid
import zipfile
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode
from xml.dom import minidom

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import (
    ChunkedEncodingError,
    IncompleteRead,
    RequestException,
)
from aiohttp import web

# ── Base directory: hoạt động đúng cả khi chạy .py lẫn .exe ──────────────────
if getattr(sys, "frozen", False):
    _base_dir = os.path.dirname(sys.executable)
else:
    _base_dir = os.path.dirname(os.path.abspath(__file__))

# ── Load config.env ────────────────────────────────────────────────────────────
_env_file = os.path.join(_base_dir, "config.env")
if os.path.exists(_env_file):
    with open(_env_file, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

# ── Phiên bản ứng dụng ──────────────────────────────────────────────────────
# Fallback khi chạy .py (không có resource version trong file). Khi build .exe,
# version thật được đọc trực tiếp từ metadata của AMConnectGDT.exe (version_info.txt).
APP_VERSION_FALLBACK = "1.0.8"


def _get_app_version() -> str:
    """Đọc FileVersion từ chính AMConnectGDT.exe (khi frozen); fallback khi chạy .py."""
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            from ctypes import wintypes

            exe_path = sys.executable
            size = ctypes.windll.version.GetFileVersionInfoSizeW(exe_path, None)
            if size:
                buffer = ctypes.create_string_buffer(size)
                ctypes.windll.version.GetFileVersionInfoW(exe_path, 0, size, buffer)
                value = ctypes.c_void_p()
                length = wintypes.UINT()
                if ctypes.windll.version.VerQueryValueW(
                    buffer,
                    r"\VarFileInfo\Translation",
                    ctypes.byref(value),
                    ctypes.byref(length),
                ) and length.value:
                    codepage = ctypes.cast(
                        value, ctypes.POINTER(wintypes.WORD)
                    )
                    lang, page = codepage[0], codepage[1]
                    sub_block = (
                        f"\\StringFileInfo\\{lang:04x}{page:04x}\\FileVersion"
                    )
                    if ctypes.windll.version.VerQueryValueW(
                        buffer,
                        sub_block,
                        ctypes.byref(value),
                        ctypes.byref(length),
                    ) and length.value:
                        return ctypes.wstring_at(value).strip()
        except Exception as e:
            log.warning("Không đọc được version từ exe (%s)", e)
    return APP_VERSION_FALLBACK


# ── Cấu hình ───────────────────────────────────────────────────────────────────
AGENT_PORT     = int(os.getenv("AGENT_PORT",     "8099"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "10"))
AGENT_HOST     = os.getenv("AGENT_HOST", "127.0.0.1").strip()
MAX_HTTP_CONCURRENT = int(os.getenv("MAX_HTTP_CONCURRENT", "10"))
HTTP_QUEUE_TIMEOUT  = float(os.getenv("HTTP_QUEUE_TIMEOUT", "5"))
MAX_DETAIL_ITEMS    = int(os.getenv("MAX_DETAIL_ITEMS", "5000"))
MAX_REQUEST_BODY_MB = int(os.getenv("MAX_REQUEST_BODY_MB", "16"))
GDT_PAGE_SIZE       = int(os.getenv("GDT_PAGE_SIZE", "30"))
GDT_CA_BUNDLE       = os.getenv("GDT_CA_BUNDLE", "").strip()
# Detail workers are capped separately from the global GDT connection pool.
DETAIL_WORKERS            = int(os.getenv("DETAIL_WORKERS", "3"))
DETAIL_MAX_ATTEMPTS       = int(os.getenv("DETAIL_MAX_ATTEMPTS", "2"))
DETAIL_REQUEST_TIMEOUT    = float(os.getenv("DETAIL_REQUEST_TIMEOUT", "25"))
DETAIL_429_COOLDOWN       = float(os.getenv("DETAIL_429_COOLDOWN", "10"))
DETAIL_429_ABORT_AFTER    = int(os.getenv("DETAIL_429_ABORT_AFTER", "4"))
DETAIL_TRANSPORT_ABORT_AFTER = int(os.getenv("DETAIL_TRANSPORT_ABORT_AFTER", "3"))
DETAIL_BATCH_TIMEOUT      = float(os.getenv("DETAIL_BATCH_TIMEOUT", "3600"))

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
if AGENT_HOST not in _LOOPBACK_HOSTS:
    raise RuntimeError(
        "AGENT_HOST chỉ được phép là 127.0.0.1, ::1 hoặc localhost. "
        "Agent có chứa thông tin đăng nhập GDT nên không được mở trực tiếp ra LAN."
    )
if not 1 <= AGENT_PORT <= 65535:
    raise RuntimeError("AGENT_PORT phải nằm trong khoảng 1..65535.")
if (
    not 1 <= MAX_CONCURRENT <= 10
    or not 1 <= MAX_HTTP_CONCURRENT <= 100
    or not 1 <= MAX_DETAIL_ITEMS <= 20000
    or not 1 <= MAX_REQUEST_BODY_MB <= 50
    or not 1 <= GDT_PAGE_SIZE <= 50
    or HTTP_QUEUE_TIMEOUT <= 0
    or not 1 <= DETAIL_WORKERS <= 10
    or not 1 <= DETAIL_MAX_ATTEMPTS <= 5
    or DETAIL_REQUEST_TIMEOUT <= 0
    or DETAIL_429_COOLDOWN <= 0
    or not 2 <= DETAIL_429_ABORT_AFTER <= 20
    or not 2 <= DETAIL_TRANSPORT_ABORT_AFTER <= 20
    or DETAIL_BATCH_TIMEOUT <= 0
):
    raise RuntimeError(
        "MAX_CONCURRENT phải trong khoảng 1..10, MAX_HTTP_CONCURRENT trong "
        "khoảng 1..100, MAX_DETAIL_ITEMS trong khoảng 1..20000, "
        "MAX_REQUEST_BODY_MB trong khoảng 1..50, GDT_PAGE_SIZE trong khoảng "
        "1..50 và HTTP_QUEUE_TIMEOUT phải lớn hơn 0. "
        "DETAIL_WORKERS 1..10, DETAIL_MAX_ATTEMPTS 1..5, "
        "DETAIL_429_ABORT_AFTER và DETAIL_TRANSPORT_ABORT_AFTER 2..20; "
        "các timeout/cooldown phải > 0."
    )

# ── GDT constants ──────────────────────────────────────────────────────────────
GDT_BASE = "https://hoadondientu.gdt.gov.vn/api"
# Header nghiệp vụ GDT. UA / Accept-Encoding do curl_cffi gắn theo impersonate.
# Frontend axios luôn gửi request-id (UUID v4) — thiếu bị WAF 403.
_GDT_HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "vi",
    "Origin":          "https://hoadondientu.gdt.gov.vn",
    "Referer":         "https://hoadondientu.gdt.gov.vn/",
}
_GDT_IMPERSONATE = "chrome120"


def _gdt_headers(**extra: str) -> dict:
    """Header GDT chuẩn + request-id mới mỗi lần (giống interceptor frontend)."""
    return {
        **_GDT_HEADERS,
        "request-id": str(uuid.uuid4()),
        "Action": "",
        **extra,
    }

# ── Logging — chỉ ghi vào file (không cần console vì đã ẩn cửa sổ CMD) ───────
_log_file = os.path.join(_base_dir, "agent.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("agent")

# ── i18n: Lag = VIET | ENG | KOR | CHINA ───────────────────────────────────────
_SUPPORTED_LAGS = ("VIET", "ENG", "KOR", "CHINA")
_current_lag: contextvars.ContextVar[str] = contextvars.ContextVar("lag", default="VIET")

_MESSAGES: dict[str, dict[str, str]] = {
    "localhost_only": {
        "VIET": "Agent chỉ cho phép kết nối localhost",
        "ENG": "Agent only allows localhost connections",
        "KOR": "Agent는 localhost 연결만 허용합니다",
        "CHINA": "Agent仅允许本地连接",
    },
    "client_addr_unknown": {
        "VIET": "Không xác định được địa chỉ client",
        "ENG": "Unable to determine client address",
        "KOR": "클라이언트 주소를 확인할 수 없습니다",
        "CHINA": "无法确定客户端地址",
    },
    "agent_overloaded": {
        "VIET": "Agent đang quá tải, vui lòng thử lại",
        "ENG": "Agent is overloaded, please try again",
        "KOR": "Agent가 과부하 상태입니다. 다시 시도해 주세요",
        "CHINA": "Agent过载，请重试",
    },
    "content_type_json": {
        "VIET": "Content-Type phải là application/json",
        "ENG": "Content-Type must be application/json",
        "KOR": "Content-Type은 application/json이어야 합니다",
        "CHINA": "Content-Type必须为application/json",
    },
    "body_must_json": {
        "VIET": "Body phải là JSON",
        "ENG": "Body must be JSON",
        "KOR": "Body는 JSON이어야 합니다",
        "CHINA": "Body必须是JSON",
    },
    "body_must_object": {
        "VIET": "Body phải là JSON object",
        "ENG": "Body must be a JSON object",
        "KOR": "Body는 JSON 객체여야 합니다",
        "CHINA": "Body必须是JSON对象",
    },
    "cred_must_str": {
        "VIET": "username và password phải là chuỗi",
        "ENG": "username and password must be strings",
        "KOR": "username과 password는 문자열이어야 합니다",
        "CHINA": "username和password必须是字符串",
    },
    "missing_cred": {
        "VIET": "Thiếu: username, password",
        "ENG": "Missing: username, password",
        "KOR": "누락: username, password",
        "CHINA": "缺少：username, password",
    },
    "date_must_str": {
        "VIET": "date_from và date_to phải là chuỗi",
        "ENG": "date_from and date_to must be strings",
        "KOR": "date_from과 date_to는 문자열이어야 합니다",
        "CHINA": "date_from和date_to必须是字符串",
    },
    "fields_must_str": {
        "VIET": "username, password, date_from và date_to phải là chuỗi",
        "ENG": "username, password, date_from and date_to must be strings",
        "KOR": "username, password, date_from, date_to는 문자열이어야 합니다",
        "CHINA": "username、password、date_from和date_to必须是字符串",
    },
    "missing_fields": {
        "VIET": "Thiếu: username, password, date_from, date_to",
        "ENG": "Missing: username, password, date_from, date_to",
        "KOR": "누락: username, password, date_from, date_to",
        "CHINA": "缺少：username, password, date_from, date_to",
    },
    "inv_type_01": {
        "VIET": "inv_type phải là 0 hoặc 1",
        "ENG": "inv_type must be 0 or 1",
        "KOR": "inv_type은 0 또는 1이어야 합니다",
        "CHINA": "inv_type必须为0或1",
    },
    "inv_type_buy_sell": {
        "VIET": "inv_type phải là 0 (mua vào) hoặc 1 (bán ra)",
        "ENG": "inv_type must be 0 (purchase) or 1 (sales)",
        "KOR": "inv_type은 0(매입) 또는 1(매출)이어야 합니다",
        "CHINA": "inv_type必须为0（购进）或1（销项）",
    },
    "date_format": {
        "VIET": "Ngày phải có định dạng YYYYMMDD hoặc YYYY-MM-DD",
        "ENG": "Date must be YYYYMMDD or YYYY-MM-DD",
        "KOR": "날짜는 YYYYMMDD 또는 YYYY-MM-DD 형식이어야 합니다",
        "CHINA": "日期格式必须为YYYYMMDD或YYYY-MM-DD",
    },
    "date_range": {
        "VIET": "date_from không được lớn hơn date_to",
        "ENG": "date_from must not be greater than date_to",
        "KOR": "date_from은 date_to보다 클 수 없습니다",
        "CHINA": "date_from不能大于date_to",
    },
    "job_running": {
        "VIET": "Tài khoản này đang có một tác vụ khác chạy",
        "ENG": "This account already has another job running",
        "KOR": "이 계정에 이미 다른 작업이 실행 중입니다",
        "CHINA": "该账户已有其他任务在运行",
    },
    "items_must_array": {
        "VIET": "items phải là một mảng hóa đơn",
        "ENG": "items must be an array of invoices",
        "KOR": "items는 세금계산서 배열이어야 합니다",
        "CHINA": "items必须是发票数组",
    },
    "items_limit": {
        "VIET": "items vượt giới hạn {max} hóa đơn",
        "ENG": "items exceeds the limit of {max} invoices",
        "KOR": "items가 한도 {max}건을 초과합니다",
        "CHINA": "items超过{max}张发票上限",
    },
    "item_must_object": {
        "VIET": "items[{index}] phải là JSON object",
        "ENG": "items[{index}] must be a JSON object",
        "KOR": "items[{index}]는 JSON 객체여야 합니다",
        "CHINA": "items[{index}]必须是JSON对象",
    },
    "item_field_invalid": {
        "VIET": "items[{index}].{field} không hợp lệ",
        "ENG": "items[{index}].{field} is invalid",
        "KOR": "items[{index}].{field}가 유효하지 않습니다",
        "CHINA": "items[{index}].{field}无效",
    },
    "item_field_too_long": {
        "VIET": "items[{index}].{field} vượt quá 100 ký tự",
        "ENG": "items[{index}].{field} exceeds 100 characters",
        "KOR": "items[{index}].{field}가 100자를 초과합니다",
        "CHINA": "items[{index}].{field}超过100个字符",
    },
    "logging_in": {
        "VIET": "Đang đăng nhập (lần {attempt}/5)...",
        "ENG": "Logging in to GDT (attempt {attempt}/5)...",
        "KOR": "GDT 로그인 중 ({attempt}/5)...",
        "CHINA": "正在登录GDT（第{attempt}/5次）...",
    },
    "login_failed": {
        "VIET": "Đăng nhập thuế thất bại sau 5 lần thử captcha",
        "ENG": "GDT login failed after 5 captcha attempts",
        "KOR": "캡차 5회 시도 후 GDT 로그인 실패",
        "CHINA": "尝试验证码5次后GDT登录失败",
    },
    "xml_download_ok": {
        "VIET": "Tải XML hóa đơn thành công",
        "ENG": "Invoice XML downloaded successfully",
        "KOR": "인보이스 XML 다운로드 성공",
        "CHINA": "发票XML下载成功",
    },
    "xml_not_found_in_zip": {
        "VIET": "Không tìm thấy invoice.xml trong file zip trả về từ GDT",
        "ENG": "invoice.xml not found in ZIP returned by GDT",
        "KOR": "GDT가 반환한 ZIP에서 invoice.xml을 찾을 수 없음",
        "CHINA": "在GDT返回的ZIP中找不到invoice.xml",
    },
    "xml_missing_params": {
        "VIET": "Thiếu tham số: cần nbmst, khhdon, shdon, khmshdon",
        "ENG": "Missing required params: nbmst, khhdon, shdon, khmshdon",
        "KOR": "필수 파라미터 누락: nbmst, khhdon, shdon, khmshdon",
        "CHINA": "缺少必填参数：nbmst、khhdon、shdon、khmshdon",
    },
    "login_success": {
        "VIET": "Đăng nhập GDT thành công",
        "ENG": "GDT login successful",
        "KOR": "GDT 로그인 성공",
        "CHINA": "GDT登录成功",
    },
    "login_rejected": {
        "VIET": "GDT từ chối đăng nhập (sai tài khoản/mật khẩu hoặc captcha)",
        "ENG": "GDT rejected login (wrong username/password or captcha)",
        "KOR": "GDT 로그인 거부 (잘못된 계정/비밀번호 또는 캡차)",
        "CHINA": "GDT拒绝登录（账号/密码错误或验证码错误）",
    },
    "gdt_request_failed": {
        "VIET": "Kết nối thuế thất bại sau {attempts} lần thử: {error}",
        "ENG": "GDT request failed after {attempts} attempts: {error}",
        "KOR": "{attempts}회 시도 후 GDT 요청 실패: {error}",
        "CHINA": "尝试{attempts}次后GDT请求失败：{error}",
    },
    "token_expired": {
        "VIET": "Token vẫn hết hạn sau khi đăng nhập lại",
        "ENG": "GDT token still expired after re-login",
        "KOR": "재로그인 후에도 GDT 토큰이 만료된 상태입니다",
        "CHINA": "重新登录后GDT令牌仍过期",
    },
    "list_phase": {
        "VIET": "danh sách",
        "ENG": "list",
        "KOR": "목록",
        "CHINA": "列表",
    },
    "source_searching": {
        "VIET": "Nguồn {source_index}/{source_total} [{phase_label}] trang {page} — đang tìm...",
        "ENG": "Source {source_index}/{source_total} [{phase_label}] page {page} — searching...",
        "KOR": "소스 {source_index}/{source_total} [{phase_label}] {page}페이지 — 검색 중...",
        "CHINA": "来源 {source_index}/{source_total} [{phase_label}] 第{page}页 — 正在查找...",
    },
    "source_done": {
        "VIET": "Nguồn {source_index}/{source_total} [{label}] xong — đang tìm...",
        "ENG": "Source {source_index}/{source_total} [{label}] done — searching...",
        "KOR": "소스 {source_index}/{source_total} [{label}] 완료 — 검색 중...",
        "CHINA": "来源 {source_index}/{source_total} [{label}] 完成 — 正在查找...",
    },
    "sold": {
        "VIET": "bán ra",
        "ENG": "sales",
        "KOR": "매출",
        "CHINA": "销项",
    },
    "sold_pos": {
        "VIET": "bán ra - máy tính tiền",
        "ENG": "sales - POS",
        "KOR": "매출 - 포스",
        "CHINA": "销项 - 收银机",
    },
    "purchase_coded": {
        "VIET": "mua vào - hóa đơn có mã",
        "ENG": "purchase - coded invoice",
        "KOR": "매입 - 코드 있는 세금계산서",
        "CHINA": "购进 - 有码发票",
    },
    "purchase_uncoded": {
        "VIET": "mua vào - hóa đơn không mã",
        "ENG": "purchase - uncoded invoice",
        "KOR": "매입 - 코드 없는 세금계산서",
        "CHINA": "购进 - 无码发票",
    },
    "purchase_pos": {
        "VIET": "mua vào - máy tính tiền",
        "ENG": "purchase - POS",
        "KOR": "매입 - 포스",
        "CHINA": "购进 - 收银机",
    },
    "detail_phase": {
        "VIET": "chi tiết",
        "ENG": "detail",
        "KOR": "상세",
        "CHINA": "明细",
    },
    "detail_progress": {
        "VIET": "Đã lấy chi tiết {done}/{total} — HĐ {current}",
        "ENG": "Fetched detail {done}/{total} — Inv {current}",
        "KOR": "상세 조회 {done}/{total} — 세금계산서 {current}",
        "CHINA": "已获取明细 {done}/{total} — 发票 {current}",
    },
    "detail_error_progress": {
        "VIET": "Đã xử lý {done}/{total} — lỗi {failed} hóa đơn",
        "ENG": "Processed {done}/{total} — {failed} invoice errors",
        "KOR": "처리됨 {done}/{total} — 오류 {failed}건",
        "CHINA": "已处理 {done}/{total} — {failed}张发票出错",
    },
    "detail_fetch_failed": {
        "VIET": "Không lấy được chi tiết từ TCT",
        "ENG": "Failed to fetch detail from GDT",
        "KOR": "GDT에서 상세를 가져오지 못했습니다",
        "CHINA": "无法从GDT获取明细",
    },
    "detail_invalid": {
        "VIET": "TCT không trả về dữ liệu chi tiết hợp lệ",
        "ENG": "GDT did not return valid detail data",
        "KOR": "GDT가 유효한 상세 데이터를 반환하지 않았습니다",
        "CHINA": "GDT未返回有效明细数据",
    },
    "starting_list": {
        "VIET": "Đang khởi động lấy danh sách hóa đơn...",
        "ENG": "Starting invoice list fetch...",
        "KOR": "세금계산서 목록 조회를 시작합니다...",
        "CHINA": "正在开始获取发票列表...",
    },
    "list_done": {
        "VIET": "Hoàn thành danh sách: {total} hóa đơn.",
        "ENG": "List completed: {total} invoices.",
        "KOR": "목록 완료: {total}건.",
        "CHINA": "列表完成：{total}张发票。",
    },
    "starting": {
        "VIET": "Đang khởi động...",
        "ENG": "Starting...",
        "KOR": "시작 중...",
        "CHINA": "正在启动...",
    },
    "preparing_detail": {
        "VIET": "Chuẩn bị lấy chi tiết {total} hóa đơn...",
        "ENG": "Preparing to fetch details for {total} invoices...",
        "KOR": "{total}건 상세 조회 준비 중...",
        "CHINA": "准备获取{total}张发票明细...",
    },
    "partial_detail_done": {
        "VIET": "Hoàn thành một phần: {failed}/{total} hóa đơn lỗi chi tiết.",
        "ENG": "Partially completed: {failed}/{total} detail errors.",
        "KOR": "일부 완료: {failed}/{total}건 상세 오류.",
        "CHINA": "部分完成：{failed}/{total}张发票明细出错。",
    },
    "detail_done": {
        "VIET": "Hoàn thành chi tiết: {total} hóa đơn.",
        "ENG": "Detail completed: {total} invoices.",
        "KOR": "상세 완료: {total}건.",
        "CHINA": "明细完成：{total}张发票。",
    },
    "all_done": {
        "VIET": "Hoàn thành: {total} hóa đơn.",
        "ENG": "Completed: {total} invoices.",
        "KOR": "완료: {total}건.",
        "CHINA": "完成：{total}张发票。",
    },
}


def _normalize_lag(value) -> str:
    if not isinstance(value, str):
        return "VIET"
    lag = value.strip().upper()
    if lag in _SUPPORTED_LAGS:
        return lag
    aliases = {
        "VN": "VIET", "VI": "VIET", "VIETNAMESE": "VIET",
        "EN": "ENG", "ENGLISH": "ENG",
        "KR": "KOR", "KO": "KOR", "KOREAN": "KOR",
        "CN": "CHINA", "ZH": "CHINA", "CHINESE": "CHINA",
    }
    return aliases.get(lag, "VIET")


def _set_lag(value) -> str:
    lag = _normalize_lag(value)
    _current_lag.set(lag)
    return lag


def _apply_lag_from_body(body: dict) -> str:
    return _set_lag(body.get("Lag", body.get("lag", "VIET")))


def _t(key: str, **kwargs) -> str:
    lag = _current_lag.get()
    entry = _MESSAGES.get(key) or {}
    text = entry.get(lag) or entry.get("VIET") or key
    return text.format(**kwargs) if kwargs else text


# ── HTTP client GDT ────────────────────────────────────────────────────────────
# curl_cffi impersonate Chrome TLS/JA3 — httpx bị GDT WAF chặn (403 hành vi không hợp lệ).
_gdt_verify = GDT_CA_BUNDLE if GDT_CA_BUNDLE else True
_gdt = AsyncSession(
    impersonate=_GDT_IMPERSONATE,
    verify=_gdt_verify,
    timeout=30,
    max_clients=max(5, MAX_CONCURRENT),
)


class _GDTTransportError(RuntimeError):
    """Lỗi mạng/protocol có thể thử lại với page size nhỏ hơn."""

# ── Token cache: username → (token, cached_at, credential_fingerprint) ─────────
_token_cache: dict[str, tuple[str, datetime, str]] = {}
_token_lock = asyncio.Lock()

# ── Semaphore giới hạn số request GDT đồng thời ───────────────────────────────
_gdt_sem: asyncio.Semaphore
_http_sem: asyncio.Semaphore

# ── Progress tracking: trạng thái kéo dữ liệu theo username và API ───────────
_progress: dict[str, dict] = {}
_progress_invoice_list: dict[str, dict] = {}
_progress_invoice_details: dict[str, dict] = {}
_active_usernames: set[str] = set()


def _credential_fingerprint(username: str, password: str) -> str:
    """Gắn token cache với đúng bộ credentials mà không giữ thêm password thô."""
    value = f"{username}\0{password}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _log_subject(username: str) -> str:
    """Mã định danh ổn định dùng trong log, không ghi username/MST thật."""
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:12]
    return f"user:{digest}"


def _set_progress(username: str, **kw) -> None:
    """
    Cập nhật tiến độ. AMNote poll GET /progress để hiện UI.

    Fields:
      status        — "starting" | "fetching" | "fetching_detail" | "done" | "error"
      phase         — "list" | "detail" | "done" | "error"
      phase_label   — mô tả pha hiện tại (vd: "mua vào SCO", "chi tiết")
      total_found   — số HĐ unique đã tìm được (chỉ tăng trong một run)
      total_done    — alias tương thích; với list = total_found
      source_index  — nguồn đang chạy (1-based)
      source_total  — tổng số nguồn cần quét
      run_id        — id run hiện tại để client bỏ progress cũ
      detail_done   — số HĐ đã lấy chi tiết xong
      detail_total  — tổng số HĐ cần lấy chi tiết
      current_inv   — HĐ đang kéo ngay lúc này (khhdon-shdon)
      message       — chuỗi hiển thị cho người dùng
    """
    state = {
        "status":        kw.get("status",        "idle"),
        "action":        kw.get("action",        ""),
        "phase":         kw.get("phase",         ""),
        "phase_label":   kw.get("phase_label",   ""),
        "total_found":   kw.get("total_found",   0),
        "total_done":    kw.get("total_done",    0),
        "total_failed":  kw.get("total_failed",  0),
        "source_index":  kw.get("source_index",  0),
        "source_total":  kw.get("source_total",  0),
        "run_id":        kw.get("run_id",        ""),
        "detail_done":   kw.get("detail_done",   0),
        "detail_total":  kw.get("detail_total",  0),
        "detail_failed": kw.get("detail_failed", 0),
        "current_inv":   kw.get("current_inv",   ""),
        "message":       kw.get("message",       ""),
        "updated_at":    datetime.now().isoformat(timespec="seconds"),
    }
    _progress[username] = state
    action = state["action"]
    if action == "fetch-invoice-list":
        _progress_invoice_list[username] = state
    elif action == "fetch-invoice-details":
        _progress_invoice_details[username] = state


# ══════════════════════════════════════════════════════════════════════════════
# GDT helpers
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_date(d: str, end_of_day: bool = False) -> str:
    """'20260101' → '01/01/2026T00:00:00'"""
    d = d.replace("-", "").strip()
    parsed = datetime.strptime(d, "%Y%m%d")
    t = "23:59:59" if end_of_day else "00:00:00"
    return f"{parsed:%d/%m/%Y}T{t}"


# ══════════════════════════════════════════════════════════════════════════════
# Captcha SVG solver (copy từ captcha_service.py)
# GDT trả captcha dạng SVG, cần giải bằng cách đếm path segments
# ══════════════════════════════════════════════════════════════════════════════

_SVG_CHAR_MAP: dict = {
    "A": ["25","25","25","25","25","25","25","25","25","29","25","25","25","22","25","17","17","17","18","24","21","21","21","24","22","25","28","28","28","28","25","28","28","28","28","21","28","28","28","31","16","35","36","35","36","12","37","37","37","31","22","18","18","18","14","24","11","11","11","11","25","11","11","11","11","12","32","24","24","24","32","39","36","36","36","36","34","35","35","35","32","30","28","35","35","35","35","35","36","36","36","35","36","38","38","38","38","39","44","40","40","40","35","34","27","27","27","24","31","20","20","12","13","13","28","13","13","12","13","12","26","10","11","10","11","25","11","11","10","10","24","10","10","10","14","19","25","25","25","33","11","37","37","37","37","14","36","36","36","37","12","39","39","39","38","18","37","38","37","33","23","30","30","30","30","27","30","30","30","30","31","30","30","30","32","34","35","21","21","21","23","28","25","25","25","25","23","27","20"],
    "B": ["24","24","24","28","163","32","33","33","32","168","32","32","32","32","171","31","31","31","30","172","29","29","29","28","172","28","28","28","25","169","24","24","24","24","168","24","24","24","24","166","24","24","24","24","164","24","14","14","14","17","164","21","21","21","22","168","22","22","22","22","171","18","18","18","16","170","15","15","15","15","164","14","14","14","14","162","14","37","37","37","30","161","23","23","23","15","158","9","9","9","11","168","12","12","11","12","174","15","15","15","17","173","20","20","20","22","171","22","22","22","23","175","28","28","28","30","175","31","31","31","32","174","33","33","33","34","172","34","34","34","34","169","34","34","34","34","159","37","37","37","37","37","175","37","37","37","37","177","36","36","36","35","178","34","34","34","33","177","29","29","29","26","174","25","25","24","24","22","176","19","19","19","18","176","16","16","16","15","175","14","14","14","14","175","14","14","14","13","174","12","12","12","11","168","11","11","11","11","157","8","8","8","15","161","23","22","22","30","158","37","37","36","36","37","159","39","39","39","37","169","37","37","37","37","172","37","37","37","37","118","37","23","23","23","31","111","31","31","31","31","107","32","32","32","28","108","24","24","24","20","107","17","17","17","17","114","17","17","17","17","117","18","18","18","20","118","23"],
    "C": ["34","34","34","34","159","32","32","33","31","157","28","28","28","27","157","23","23","23","22","157","20","21","20","15","159","14","14","14","12","169","11","11","11","11","172","11","11","11","11","175","11","11","11","11","178","11","11","11","12","176","15","15","15","14","171","14","14","14","14","169","14","14","14","14","162","16","16","16","18","160","21","21","21","22","160","23","23","23","28","162","30","30","30","32","169","32","32","32","32","175","31","30","30","33","177","35","35","35","35","173","35","35","35","35","165","34","38","38","38","35","178","33","33","32","33","176","33","33","33","32","176","32","32","30","30","32","169","32","32","32","32","163","30","30","30","28","162","25","25","25","21","164","19","18","19","17","171","16","16","16","16","173","16","16","16","16","177","17","17","17","15","179","12","12","12","12","178","12","12","12","12","177","12","12","12","11","178","10","10","10","10","175","10","10","10","10","172","10","10","10","10","158","13","13","13","15","156","19","19","19","20","156","23","23","23","26","157","28","28","28","31","159","33","33","33","33","160","34","34","34","36","169","37","37","37","38","180","38"],
    "D": ["15","15","15","15","104","14","14","14","18","106","23","23","23","28","105","32","32","32","32","111","32","32","32","32","117","29","29","29","27","118","23","23","23","21","118","19","19","19","15","112","15","37","37","37","31","103","24","24","24","16","99","9","9","9","11","112","12","12","12","12","121","19","19","19","22","122","23","23","23","28","121","30","30","30","31","119","32","32","32","33","115","34","34","34","34","111","34","34","34","35","100","37","37","36","36","36","122","35","35","35","33","123","30","30","30","29","123","25","25","21","21","18","122","16","16","16","16","121","15","15","15","15","12","114","11","11","11","11","105","10","10","10","10","98","8","8","8","15","103","24","24","24","31","99","37","37","37","37","101","36","37","37","37","100","39","39","39","38","107","37","37","37","37","112","37","37","37","37","118","37","23","23","23","31","111","31","31","31","31","107","32","32","32","28","108","24","24","24","20","107","17","17","17","17","114","17","17","17","17","117","18","18","18","20","118","23"],
    "E": ["9","9","9","11","139","12","12","12","12","147","10","10","10","11","146","13","13","13","14","141","14","15","15","15","135","14","14","14","17","136","21","21","21","21","139","21","21","21","21","143","21","21","21","22","143","23","23","24","24","24","136","24","24","24","28","135","32","32","32","31","140","32","32","32","32","146","33","33","33","33","146","34","34","36","36","34","140","34","34","34","34","131","37","37","37","30","133","23","23","23","16","130","9","37","37","37","37","132","36","36","36","37","132","38","39","39","37","141","37","37","37","37","150","40","40","40","37","148","35","35","35","35","146","34","34","34","33","146","32","32","32","31","140","31","31","31","31","137","31","31","31","30","138","29","29","26","26","26","141","26","26","26","26","145","27","26","27","26","145","25","25","23","23","23","23","143","23","23","23","22","143","21","21","21","21","140","21","21","21","21","138","21","21","21","20","137","17","17","17","17","139","17","17","17","17","148","15","15","15","13","149","10","11","11","11","147","12","12","12","11","148","9","9","9","12","138","11","11","11","11","129","8","8","8","15","132","22","23","23","31","130","37"],
    "F": ["21","21","21","21","26","21","22","21","21","30","21","21","21","22","30","23","23","24","24","24","23","24","24","24","30","22","35","35","35","36","18","37","36","37","30","20","23","23","23","15","17","9","9","9","11","26","12","12","12","12","34","10","10","10","11","34","12","12","13","13","14","28","14","14","14","15","22","14","14","14","17","23","21","9","9","9","11","25","11","11","11","11","16","8","8","8","15","20","23","22","23","30","17","37","37","37","37","20","36","36","36","37","19","39","38","39","37","24","37","37","37","32","25","26","26","26","26","28","26","26","26","26","32","26","27","27","25","32","25","25","23","23","23","30","23","23","23","22","30","21","21","21","21","28","21","21","21","21","25","21","21","19","19","18","25","17","17","17","17","27","17","17","17","17","35","15","15","15","13","36","10","10","10","11","34","12","12","12","11","35","9"],
    "G": ["18","18","18","14","54","14","14","14","14","48","15","15","15","17","45","21","21","23","23","32","54","32","32","32","32","58","31","31","31","30","60","28","28","28","26","56","25","25","25","25","25","52","25","25","25","24","52","22","23","23","23","64","22","22","22","23","64","24","24","24","25","64","25","25","25","25","62","25","25","25","25","61","25","25","25","27","63","29","29","29","30","63","30","30","30","35","54","35","35","35","35","45","33","33","33","30","43","25","25","25","24","42","21","22","22","19","42","18","18","18","15","44","13","13","13","11","51","11","11","11","11","11","61","11","11","11","12","65","15","15","15","16","62","18","20","20","20","19","67","17","17","17","16","65","14","14","14","12","60","11","11","11","11","54","11","10","11","11","11","44","13","13","13","14","42","18","18","18","17","42","23","23","23","24","42","27","27","27","31","44","33","33","33","34","45","35","35","35","37","54","37","37","37","37","37","60","37","37","37","37","64","37","37","37","36","66","34","34","34","32","65","30","30","30","29","64","28","28","28","28","66","28","28","28","25","66","24","24","24","24","65","24","24","24","24","64","24","24","24","23","64","22","22","22","22","52","22","22","22","23","52","24","24","26","26","26","52","26","26","26","26","53","26","26","26","26","53","27","27","27","27","53","28","28","28","28","55","28","28","27","28","57","28","28","28","28","59","28","28","28","28","30","58","31","31","31","32","54","32","32","32","32","48","30","30","30","28","47","25","25","25","20","49","18","18","18","17","56","16","16","16","16","61","18","18","18","18","62","18","18","18","18","19","63","20","20","20"],
    "H": ["23","23","23","23","23","48","23","23","23","30","46","35","35","36","36","43","37","37","37","31","45","24","24","24","16","41","9","9","9","9","45","10","10","10","15","48","20","20","20","21","54","21","21","21","21","60","20","20","20","16","62","11","11","11","10","66","10","10","10","16","63","24","24","24","30","65","36","36","36","35","61","35","35","35","30","60","23","35","35","35","35","63","36","36","36","36","64","38","38","38","39","69","40","40","40","34","65","25","25","25","17","68","10","11","11","11","11","66","11","11","11","10","67","9","9","9","10","62","11","11","11","15","60","20","20","20","20","55","20","20","20","20","50","20","20","20","17","49","13","13","13","13","46","12","12","12","11","45","10","10","10","9","41","8","8","8","15","45","24","24","24","31","42","37","37","37","37","44","36","37","37","37","43","39","39","39","38","48","37","37","37","32","50","26","26","26","26","55","26","26","26","25","59","26","26","26","31","61","35"],
    "I": [],
    "J": ["35","35","35","35","108","35","35","35","35","106","35","35","35","35","104","34","34","34","33","106","31","31","31","32","110","32","32","32","32","111","31","32","31","31","112","30","30","30","25","112","21","21","21","16","112","12","12","11","11","116","10","10","10","17","114","24","24","24","28","115","31","31","31","32","115","32","32","32","33","115","33","33","33","34","111","35","38","38","38","37","37","37","37","118","37","37","37","36","118","35","35","35","29","116","23","23","22","16","118","11","10","10","11","116","12","12","12","11","117","9","10","10","11","112","11","11","11","16","111","20","20","21","26","111","30","30","30","32","110","32","32","32","32","106","30","30","30","33","103","34","34","34","35","105","35","35","35","35","105","36","36","36","37","108","37","37","37","37","112","37","37","37","38","116","38"],
    "K": ["10","10","10","15","77","20","20","20","16","87","11","11","11","11","93","10","10","16","16","19","80","22","22","22","31","93","36","36","36","35","87","34","35","35","29","77","23","23","23","30","76","35","35","35","35","75","35","36","36","36","36","72","37","37","37","30","75","22","23","23","15","71","9","9","9","9","9","9","10","75","10","10","10","10","9","70","8","8","8","15","75","24","24","24","31","72","37","37","36","36","37","72","39","39","39","38","77","38","38","37","37","37","78","37","37","37","31","79","26","27","26","29","83","31","31","31","32","87","35","35","35","35","36","90","37","37","37","38","98","40","40","40","31","82","24","24","24","20","95","11","11","11","12","91","12","12","12","11","94","9","9","9","11","87","11","11","11","13","79","18","18","18","15","78","13","13","13","13","76","12","12","12","12","75","10"],
    "L": [],
    "M": ["18","18","18","20","59","26","26","30","30","32","55","34","34","34","34","54","34","34","34","34","53","34","34","34","33","49","26","26","26","20","45","17","17","17","20","45","24","24","24","30","43","36","36","36","36","39","37","37","37","31","42","24","24","24","15","37","8","8","8","9","41","9","9","9","16","54","29","29","29","22","60","19","19","19","13","67","10","10","10","9","71","9","9","9","15","66","24","24","24","30","69","36","36","36","36","66","35","35","35","30","63","24","24","24","21","64","18","35","35","35","36","67","36","36","36","37","69","38","38","38","38","74","40","40","40","33","68","24","23","24","16","72","10","10","11","10","9","72","8","8","8","9","68","9","9","9","15","55","28","28","28","17","45","12","12","12","12","44","12","11","10","10","9","41","9","9","9","8","36","7","7","7","14","42","24","24","24","31","38","37","37","37","37","40","37","37","37","37","39","39","39","39","38","44","38","38","38","31","47","23","23","23","27","52","35","35","35","35","53","35","35","35","35","35","55","37","37","37","37","56","37","37","37","37","58","37","37","37","33","63","25","25","25","30","65","36"],
    "N": ["27","17","17","20","19","24","24","24","30","17","36","36","36","36","14","37","37","37","31","16","24","24","24","16","12","8","8","8","8","13","9","9","9","20","33","29","29","29","27","33","24","24","24","17","35","11","11","11","10","39","10","10","10","16","35","23","23","23","30","38","36","36","36","36","37","36","36","36","32","27","27","27","40","40","40","40","42","40","40","40","40","33","37","24","24","24","17","41","10","11","10","11","38","11","11","11","11","40","9","9","9","10","35","10","10","11","17","32","24","24","24","26","32","28","28","28","23","23","18","18","8","8","8","11","7","7","7","15","16","24","24","24","31","13","37","37","37","37","15","37","37","37","37","14","39","39","39","38","18","37","38","37","31","21","24","24","24","22","21","20","20","20","33","41","40"],
    "O": [],
    "P": ["14","14","14","18","136","22","22","22","22","139","22","22","22","22","22","143","20","20","20","19","143","18","18","18","15","139","14","14","15","14","135","14","24","25","24","31","135","35","35","35","36","131","37","37","37","30","133","23","23","23","15","130","9","9","9","12","141","12","11","12","12","147","15","15","15","17","146","20","20","20","21","145","22","22","22","24","141","24","24","24","25","136","25","27","27","27","27","148","22","22","22","19","149","17","16","16","15","148","14","14","14","14","147","13","13","13","13","146","12","12","13","11","141","11","11","11","11","130","8","8","8","15","133","22","23","22","30","131","37","37","37","37","133","36","36","37","37","38","132","39","39","39","38","137","37","37","37","32","138","27","27","27","27","140","27","27","27","27","142","27","17","17","17","17","143","17","17","17","17","143","18","18","17","18","143","18","18","18","19","142","20","20","20","21","141","21","21","21","21","138","21","21","21","21","137","17","17","17","17","17","142","17"],
    "Q": ["14","14","14","14","107","14","14","15","15","104","15","15","15","17","101","21","21","21","24","101","27","27","27","31","106","32","32","32","33","109","33","33","33","33","33","110","33","33","32","33","110","33","33","32","32","114","32","32","28","28","28","111","27","27","26","26","28","116","30","30","30","29","119","23","23","23","22","119","21","21","21","16","114","15","15","15","14","110","14","36","36","36","37","121","38","38","34","34","35","110","35","35","35","35","100","33","33","33","31","98","28","28","28","26","98","23","23","23","22","97","20","20","20","19","97","18","18","18","15","99","13","13","13","12","104","11","12","11","11","109","11","11","11","11","120","14","14","14","16","122","21","21","21","24","122","26","26","26","30","118","32","32","33","34","123","36","12","12","12","11","109","11","11","11","11","105","11","11","11","11","99","13","13","13","15","97","18","18","18","18","97","20","20","20","22","97","23","23","23","25","98","28","28","28","31","100","33","33","33","34","101","35","35","35","37","109","37","37","37","37","112","38","37","37","38","119","37","37","37","37","125","41","41","41","40","127","39","39","39","39","125","38","38","36","36","36","36","122","35","35","35","32","123","28","28","28","27","124","27","27","27","26","124","25","25","25","20","123","17","17","17","16","122","15","15","15","13","118","12","16","17","17","16","118","18","18","18","19","118","21","21","21","22","118","23","23","23","28","116","30","30","30","29","112","26","26","26","27","110","28","28","28","29","112","30","30","30","30","31","113","32","32","32","32","110","32","32","32","32","32","32","32","104","31","30","31","28","102","25","25","25","17","112","17"],
    "R": ["14","14","14","18","21","21","21","21","21","26","22","22","21","22","29","18","18","18","16","28","15","15","15","15","25","14","14","14","14","20","14","23","23","23","29","20","35","35","35","35","16","37","37","37","30","19","23","23","23","15","15","9","9","9","12","27","12","12","12","12","33","15","15","15","16","33","16","16","17","17","21","30","23","23","23","24","28","24","24","24","24","31","35","36","36","36","35","30","35","35","35","30","21","23","35","35","35","36","36","37","33","37","37","37","38","40","40","40","40","33","28","26","26","26","26","26","34","22","22","21","19","34","17","17","17","14","33","14","14","14","13","32","13","13","13","11","27","11","11","11","11","14","8","8","8","15","18","23","22","22","31","16","37","37","37","37","17","36","36","39","39","38","22","37","37","37","32","23","27","27","27","32","30","35","17","17","17","17","29","17","18","18","18","19","28","20","20","20","21","27","21","21","21","21","25","21","21","21","21","24","21","21","21","20","23","17","17","17","17","25","17","17","17","17","29","17"],
    "S": ["35","35","35","35","133","34","34","34","34","130","30","30","29","29","29","133","28","28","28","30","135","31","31","31","32","138","32","32","32","32","142","28","28","28","26","139","25","25","25","24","134","22","22","22","21","131","16","16","16","16","130","15","15","15","14","130","14","14","14","12","132","12","12","12","11","138","11","11","11","11","141","11","11","11","12","143","12","12","12","12","146","15","15","15","16","142","17","17","17","14","138","14","14","14","14","135","15","15","15","15","134","17","17","17","19","137","21","21","21","21","142","23","22","22","24","145","28","28","28","29","145","30","30","30","33","144","34","34","34","35","138","35","37","37","37","37","144","37","37","37","37","147","37","37","37","36","148","34","34","34","33","148","31","31","31","26","145","25","25","25","23","142","22","22","20","20","20","136","20","20","19","19","19","18","137","17","17","17","16","140","16","16","16","16","142","17","17","17","17","142","18","17","18","17","143","17","17","17","18","144","19","19","19","19","148","17","17","17","14","146","13","13","13","12","144","11","11","12","11","138","11","11","11","11","131","11","11","11","12","130","13","13","14","14","131","16","16","16","20","133","22","22","22","24","136","25","25","25","25","141","27","27","27","27","27","27","28","142","28","28","28","32","138","32","32","32","32","135","31","31","31","31","135","30","30","30","30","135","29","29","29","30","134","30","30","30","29","134","28","28","28","29","130","30","30","30","31","130","32","32","32","34","132","35","35","34","36","136","37","37","37","37","140","37"],
    "T": ["34","34","34","34","52","34","35","35","29","53","24","24","24","20","52","14","14","14","14","44","12","12","10","10","10","43","9","9","9","11","54","12","12","12","12","65","10","10","10","11","64","13","13","13","14","56","15","14","14","19","56","24","24","24","29","56","34","9","9","9","11","54","11","11","11","11","42","8","8","8","9","44","13","13","12","13","46","13","13","14","14","47","16","16","16","17","52","17","17","17","20","52","24","24","24","29","52","35","35","35","35","54","35","35","35","35","54","36","36","36","36","54","37","37","37","37","56","37","37","37","37","59","37","37","37","32","58","27","27","27","21","58","17","17","17","16","65","15","15","15","13","67","10","10","10","11","65","11","11","11","10","66","9"],
    "U": [],
    "V": ["35","35","28","28","15","69","9","9","9","10","73","11","10","10","17","83","30","30","29","21","87","20","20","20","14","92","11","11","11","11","97","10","10","10","14","89","22","22","22","25","84","35","35","35","35","83","35","35","35","35","82","35","37","37","37","27","91","23","23","23","15","98","11","11","10","11","95","12","12","10","10","10","98","9","9","9","10","92","10","10","11","16","83","27","27","27","17","77","13","13","13","13","75","13","13","13","12","74","10","10","10","9","68","8","8","8","15","79","28","28","28","31","81","35","35","35","35","83","35","35","35","36","83","37","37","37","37","85","37","37","37","37","87","37"],
    "W": ["35","35","35","31","84","22","22","22","20","83","18","18","27","27","32","76","35","35","35","35","73","35","35","35","34","73","33","33","33","27","71","21","21","21","14","62","9","9","9","10","66","10","10","10","18","76","29","29","29","26","79","21","21","21","13","82","12","12","12","12","14","85","16","16","21","21","26","90","29","29","29","18","99","10","10","10","10","104","9","9","9","14","95","20","20","20","26","92","33","33","33","34","92","35","35","35","35","35","90","35","38","38","38","34","95","30","30","30","19","105","10","11","10","11","102","11","11","11","10","105","9","9","9","9","99","10","10","10","17","90","26","27","27","25","86","13","13","13","13","85","13","13","12","13","12","84","11","11","11","11","14","79","19","19","19","24","77","27","27","27","19","70","12","12","12","12","69","12","12","12","11","66","10","10","10","9","61","8","8","8","13","70","19","19","19","26","73","33","32","33","34","73","36","36","36","36","74","36","36","35","35","74","35","35","36","37","37","74","38","38","38","37","78","37","37","37","32","84","22","22","23","31","90","35","35","35","35","91","35","35","35","36","93","37","37","37","38","96","38"],
    "X": ["25","25","25","31","104","35","35","35","36","100","37","36","37","31","109","23","23","23","16","99","9","9","9","10","104","11","11","11","16","111","20","20","20","16","117","11","11","11","11","122","10","10","10","16","112","22","23","22","31","122","36","36","36","35","117","35","35","35","32","111","25","35","35","35","35","118","35","35","35","36","120","38","37","38","38","127","40","40","40","37","148","35","35","35","35","146","34","34","34","33","146","32","32","32","31","140","31","31","31","31","137","31","31","31","30","138","29","29","26","26","26","141","26","26","26","26","145","27","26","27","26","145","25","25","23","23","23","23","143","23","23","23","22","143","21","21","21","21","140","21","21","21","21","138","21","21","21","20","137","17","17","17","17","139","17","17","17","17","148","15","15","15","13","149","10","11","11","11","147","12","12","12","11","148","9","9","9","12","138","11","11","11","11","129","8","8","8","15","132","22","23","23","31","130","37"],
    "Y": ["34","34","34","34","34","166","35","34","34","28","166","24","24","24","18","154","9","9","9","10","160","11","11","11","16","168","22","22","21","16","175","11","11","11","11","180","10","10","10","16","169","24","24","24","31","170","34","12","9","9","10","175","11","11","11","15","168","20","20","20","17","163","13","13","13","13","162","13","13","13","12","160","10","10","10","10","153","8","8","8","16","166","24","24","24","31","165","35","35","35","35","167","35","35","36","36","36","167","37","37","37","37","173","37","37","37","32","171","26","26","26","18","182","11","10","12"],
    "Z": ["33","34","34","31","23","24","24","24","18","30","14","14","14","15","26","15","15","15","15","17","13","13","13","11","15","9","9","9","11","26","12","12","12","12","36","10","10","10","11","35","11","11","13","13","17","28","23","23","27","27","30","21","32","32","32","31","28","32","32","32","32","34","33","33","34","34","35","35","36","36","36","34","25","34","34","34","34","16","37","37","37","36","17","34","37","37","37","37","17","36","36","36","37","16","39","39","39","37","28","37","37","37","37","39","40","40","40","37","37","35","35","35","35","35","34","35","34","34","34","32","32","32","31","25","31","31","31","29","31","23","23","23","17","36","15","15","15","13","38","10","10","11","11","36","12","11","11","11","37","9","9","9","11","26","11","11","11","11","15","9","9","9","11","17","13","13","13","14","18","14","14","14","15","19","16","16","16","17","28","17","17","17","20","22","25","25","33","33","35","15","37"],
    1: [],
    2: ["32","32","32","33","175","36","35","35","34","166","34","34","34","35","158","37","37","37","36","158","36","36","36","32","166","28","28","28","23","171","18","18","18","16","170","15","15","15","14","167","14","14","14","14","167","14","14","14","14","164","15","15","15","16","163","19","19","19","19","160","18","18","18","17","159","15","15","15","14","160","12","12","13","11","165","11","11","11","11","11","170","11","11","11","12","175","14","14","14","15","175","15","15","15","16","175","17","17","17","25","165","32","32","32","32","168","32","32","32","32","174","32","12","12","12","11","170","11","11","11","11","10","160","12","12","12","13","159","15","15","15","16","159","18","18","18","19","161","19","19","20","20","20","161","21","21","21","21","165","21","21","21","21","165","21","21","21","19","166","17","17","17","16","169","16","16","16","16","171","16","16","16","17","171","17","17","17","18","171","18","18","18","23","165","28","28","28","30","158","36","36","36","37","157","37","37","37","36","160","36","36","36","36","158","38","38","38","38","158","39","39","39","37","169","37","37","37","37","179","39","39","37","37","36","177","35","35","35","35","175","34","34","34","33","175","32","32","32","31","170","31","31","31","27","176","19","18","15","16","14","175","13","13","13","13","13","175","13","13","13","12","174","12"],
    3: ["35","35","35","35","75","34","34","34","34","74","32","32","32","32","74","29","29","29","29","77","29","29","29","29","31","78","32","32","32","32","81","32","32","32","32","83","32","32","32","32","84","31","31","31","30","85","28","28","28","26","84","24","24","24","23","79","23","23","22","22","22","81","22","22","22","22","84","21","21","21","19","85","18","18","18","17","85","17","17","17","16","84","15","15","15","14","81","14","14","14","14","79","14","14","14","15","77","16","16","16","17","77","17","17","17","17","74","16","16","17","15","74","14","14","14","12","75","12","12","12","11","78","11","11","11","11","84","11","11","11","12","89","16","16","16","17","88","19","19","19","22","85","22","23","23","23","88","26","26","26","28","89","30","30","30","34","84","35","35","35","35","78","35","37","37","37","37","87","37","37","37","37","90","37","37","37","36","91","34","34","34","33","91","31","31","31","26","88","25","25","24","24","24","23","90","18","18","18","18","91","17","17","17","16","91","16","16","16","14","89","13","13","13","13","12","88","12","12","12","11","81","11","11","11","11","78","11","11","11","11","75","11","11","11","12","73","14","14","14","13","73","17","17","17","17","76","17","17","17","18","75","18","18","18","19","76","19","19","19","20","79","20","20","20","18","80","17","17","17","16","83","16","16","16","16","85","16","16","16","16","85","17","17","17","17","17","18","85","18","18","18","19","84","21","21","21","22","81","22","22","22","22","79","22","22","22","22","79","23","23","23","23","82","24","24","24","24","24","24","81","25","25","25","25","85","26","26","26","26","85","28","28","28","31","82","31","32","32","32","80","32","32","32","31","79","31","31","31","31","30","30","30","30","77","30","30","30","30","77","30","30","30","29","77","29","29","29","29","77","28","28","28","28","74","29","29","29","29","74","30","30","30","31","73","32","32","32","34","75","35","35","35","37","81","37","37","37","37","83","37"],
    4: ["27","28","27","27","169","27","27","27","25","169","22","22","22","20","169","17","17","17","19","162","27","35","35","35","35","169","34","34","34","32","169","30","30","30","29","158","31","31","31","30","159","29","29","29","26","164","20","20","20","15","171","11","11","11","11","173","11","11","11","17","171","24","24","24","26","171","27","27","27","27","27","174","27","27","27","28","174","30","30","30","30","171","30","30","30","32","172","35","27","27","27","27","174","27","27","27","27","26","173","24","24","24","18","175","12","12","12","12","173","12","12","12","12","174","10","10","10","11","170","11","11","11","15","161","24","24","21","21","22","162","22","22","31","31","31","160","31","31","31","31","32","159","33","33","33","32","168","32","32","32","33","168","35","35","35","35","171","35","35","35","36","171","37","37","37","37","176","38","38","38","36","174","33","33","33","33","33","177","34","34","34","31","176","30","30","30","29","174","29","29","29","28","174","27","27","27","27","26","168","23","23","23","24","168","25","25","25","26","168","27","27","27","27","167","27","27","27","27","166","27"],
    5: ["35","35","35","35","102","33","33","33","32","104","30","30","30","32","108","32","32","32","32","113","31","31","31","29","114","27","27","27","22","109","22","22","22","22","105","23","23","23","23","23","20","104","17","17","17","14","104","11","11","11","12","111","12","12","12","12","119","11","11","12","12","13","118","14","14","14","15","111","15","15","15","15","108","15","14","15","15","107","20","20","20","19","110","19","19","19","19","115","21","21","21","22","117","26","26","26","31","116","33","33","33","35","111","35","35","35","35","107","35","37","37","37","37","119","36","36","36","35","120","33","33","33","30","119","25","25","25","23","117","22","22","22","22","22","22","21","115","20","20","20","20","19","110","19","19","19","19","18","110","17","17","17","17","119","16","16","16","14","121","11","11","12","12","11","119","10","10","10","11","112","12","12","11","12","103","10","11","11","15","104","19","19","19","21","104","23","23","23","23","105","24","24","24","23","106","23","23","23","24","105","25","25","25","26","106","26","26","26","24","111","24","24","24","24","114","25","25","25","26","114","27","27","27","29","113","30","30","30","32","109","32","32","32","32","108","32","32","32","32","106","31","31","31","31","31","31","105","29","29","29","31","102","33","33","33","34","103","35","35","35","36","107","37","37","37","37","113","37"],
    6: ["24","24","24","24","107","25","25","25","26","105","28","28","28","30","106","31","31","31","32","109","32","32","32","32","112","31","31","31","30","114","29","29","29","28","114","28","28","28","26","112","25","25","25","24","110","24","35","35","35","35","104","34","34","34","33","102","31","31","32","31","102","30","30","30","29","102","29","29","29","26","103","25","25","25","23","104","22","22","22","17","113","11","11","11","11","118","10","10","10","17","108","22","22","22","21","110","21","21","21","21","115","23","23","23","25","116","28","28","28","29","117","30","30","30","33","114","34","34","34","34","109","35","37","37","37","37","115","37","37","37","37","118","36","36","37","36","119","34","34","34","34","119","33","32","32","31","119","31","31","31","26","116","24","24","24","24","116","24","24","24","24","24","23","115","22","22","22","21","112","21","21","21","19","116","16","16","10","10","11","116","12","12","12","11","119","9","9","9","10","113","11","11","11","17","104","22","22","21","24","102","30","30","30","31","102","32","32","32","33","103","34","34","34","35","103","35","35","35","36","105","36","36","36","37","108","37","37","37","37","112","37","26","26","26","26","113","26","26","26","27","113","28","28","28","30","112","31","31","31","31","109","32","32","32","32","107","31","32","32","31","107","31","31","31","27","110","26","26","26","26","111","26"],
    7: ["22","22","22","22","144","21","22","21","22","144","23","23","24","24","24","142","24","24","24","24","141","24","24","24","30","134","35","35","35","36","131","36","36","36","30","138","24","24","24","24","23","135","22","22","21","22","139","22","22","18","18","16","143","15","14","14","15","140","15","15","15","15","130","12","13","11","11","10","129","9","9","9","12","139","12","12","12","12","149","10","10","10","11","148","11","11","11","16","142","22","13","13","11","11","11","148","12","12","12","12","11","149","11","11","11","11","150","9","9","9","12","139","11","11","11","11","128","8","8","8","10","130","13","13","13","13","132","14","14","14","14","132","16","16","16","17","141","17","17","17","18","139","21","21","21","21","135","21","21","21","22","135","23","23","24","25","26","26","33","130","37","37","37","36","133","36","36","36","37","131","38","38","38","37","137","37","37","37","33","142","26","26","27","27","26","146","25","25","23","23","23","145","23","23","23","23","144","23","23","23","23","144","23","23","22","23","17","150","13"],
    8: ["24","24","24","24","135","25","25","25","26","133","28","28","28","30","135","31","31","31","32","138","32","32","32","32","141","31","31","31","30","142","28","28","28","26","141","25","25","25","24","138","24","14","14","14","14","135","15","15","15","15","134","17","17","17","21","138","21","21","21","21","141","20","20","20","19","142","17","17","17","16","141","15","15","15","14","138","14","35","35","35","35","35","132","34","34","34","33","131","30","30","30","23","135","22","22","22","21","131","16","16","16","14","131","14","14","14","11","135","11","11","11","11","140","11","12","12","12","145","14","14","14","15","145","15","15","15","16","145","16","16","16","21","141","22","22","22","23","145","28","28","28","28","145","30","30","30","32","144","33","34","33","34","138","35","37","37","37","37","37","147","37","37","37","36","148","34","34","34","33","148","31","31","31","26","144","24","24","24","24","24","24","22","146","18","18","18","18","147","17","17","17","16","147","15","15","16","14","145","14","14","14","13","13","13","144","12","12","12","12","11","138","11","11","11","11","135","11","11","11","11","133","11","11","11","12","130","13","13","13","14","130","14","14","15","15","131","17","17","17","21","134","22","22","22","22","23","130","29","28","29","31","130","32","32","32","33","131","34","34","34","35","132","35","35","35","37","140","37","26","26","26","26","141","26","27","26","27","142","28","28","28","28","142","29","28","29","31","140","32","32","32","32","138","32","32","32","32","136","32","32","32","31","135","30","30","30","28","137","27","27","27","26","140","26","16","16","17","17","141","17","17","17","18","19","140","20","20","20","21","138","21","21","21","21","137","20","20","20","20","137","20","19","20","18","137","17","17","17","17","140","17"],
    9: ["14","14","14","14","14","21","16","16","16","17","21","18","18","18","20","22","21","21","21","23","25","23","22","23","22","28","22","21","21","21","30","18","18","18","16","28","15","15","15","14","25","14","23","23","26","26","30","23","34","35","34","35","19","36","35","35","29","27","24","24","24","25","25","25","25","25","25","20","23","23","23","22","18","17","17","17","17","18","16","16","16","15","18","14","14","14","13","19","12","12","12","11","22","11","11","11","11","25","11","11","11","11","28","11","11","11","12","31","12","12","12","13","33","15","15","15","15","33","18","18","18","20","32","21","21","21","22","31","23","26","26","26","24","35","19","19","19","17","35","16","16","16","14","33","13","13","13","13","13","32","12","12","12","11","25","11","11","11","11","21","11","11","11","11","18","12","12","11","12","17","14","14","14","15","18","17","17","17","20","18","20","21","20","22","20","24","24","24","24","21","24","24","24","25","21","26","26","26","27","25","27","27","27","30","22","32","32","36","36","35","22","35","35","35","36","19","38","38","38","37","26","37","37","37","34","32","27","27","27","27","26","33","26","17","16","17","16","28","17","17","17","17","29","17","17","17","17","29","17","18","18","18","29","18","18","18","20","28","21","21","21","22","25","22","22","22","22","23","22","22","22","21","23","20","20","20","20","23","20","20","20","18","25","17","17","17","17","27","17"],
}


def _svg_count_char(count: int, ar_add_ar: list) -> str:
    for i in range(1, 10):
        if i in _SVG_CHAR_MAP and len(_SVG_CHAR_MAP[i]) == count:
            return str(i)
    for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if not _SVG_CHAR_MAP.get(char):
            continue
        if len(_SVG_CHAR_MAP[char]) == count:
            if char == "B" and len(ar_add_ar) > 1 and ar_add_ar[1].split(".")[0] == "35":
                return "S"
            if char == "N" and len(ar_add_ar) > 1 and ar_add_ar[1].split(".")[0] == "25":
                return "X"
            return char
    return "-"


def _solve_svg_captcha(svg_content: str) -> str:
    doc = minidom.parseString(svg_content)
    ar_add: dict[str, int] = {}
    ar_add_ar: dict[str, list] = {}
    ar_index: list[str] = []
    for path in doc.getElementsByTagName("path"):
        fill = path.getAttribute("fill")
        if fill and fill != "none":
            d_attr = path.getAttribute("d")
            parts = d_attr.split(" ")
            raw_id = parts[0].replace("M", "")
            id_val = raw_id.split(".")[0]
            ar_add[id_val] = len(parts) - 1
            ar_add_ar[id_val] = parts
            ar_index.append(id_val)

    def _sort_key(v):
        try:
            return (0, int(v))
        except ValueError:
            return (1, v)

    result = ""
    for idx in sorted(set(ar_index), key=_sort_key):
        result += _svg_count_char(ar_add[idx], ar_add_ar[idx])
    return result


async def _get_captcha() -> tuple[str, str]:
    key, captcha_text = "", ""
    for _ in range(10):
        try:
            r = await _gdt.get(f"{GDT_BASE}/captcha", headers=_gdt_headers())
            r.raise_for_status()
            data = r.json()
            key = data["key"]
            captcha_text = _solve_svg_captcha(data["content"])
            if "-" not in captcha_text:
                return key, captcha_text
            log.debug("Captcha chưa rõ (%s), thử lại...", captcha_text)
        except Exception as e:
            log.error("Lấy captcha lỗi: %s", e)
            return "", ""
    return key, captcha_text


async def _gdt_login(username: str, password: str) -> Optional[str]:
    """Login GDT, thử captcha tối đa 5 lần."""
    subject = _log_subject(username)
    # Warm-up cookie F5/WAF giống trình duyệt mở trang trước khi login
    try:
        await _gdt.get(
            "https://hoadondientu.gdt.gov.vn/",
            headers=_gdt_headers(Accept="text/html,application/xhtml+xml"),
        )
    except Exception as e:
        log.debug("Warm-up GDT homepage lỗi (bỏ qua): %s", e)

    for attempt in range(1, 6):
        _set_progress(username,
            status="logging_in",
            action="login",
            message=_t("logging_in", attempt=attempt),
        )
        key, value = await _get_captcha()
        try:
            r = await _gdt.post(
                f"{GDT_BASE}/security-taxpayer/authenticate",
                json={"username": username, "password": password,
                      "cvalue": value, "ckey": key},
                headers=_gdt_headers(
                    **{
                        "Content-Type": "application/json;charset=UTF-8",
                        "End-Point": "/lien-he",
                    }
                ),
            )
            try:
                d = r.json()
            except Exception:
                d = {}
            if "token" in d:
                log.info("[%s] Đăng nhập GDT OK (lần %d)", subject, attempt)
                return d["token"]
            msg = d.get("message") or (r.text or "").strip() or f"HTTP {r.status_code}"
            # WAF chặn — backoff, không spam authenticate
            if r.status_code == 403 or "không hợp lệ" in msg.lower() or "blocked" in msg.lower():
                wait = min(2 ** attempt, 16)
                log.error("[%s] GDT chặn login (HTTP %s): %s — chờ %ds...",
                          subject, r.status_code, msg, wait)
                await asyncio.sleep(wait)
                continue
            if "captcha" not in msg.lower():
                log.error("[%s] GDT từ chối đăng nhập: %s", subject, msg)
                return None
            log.warning("[%s] Captcha sai, thử lại %d/5...", subject, attempt)
        except Exception as e:
            log.error("[%s] Login lỗi lần %d: %s", subject, attempt, e)
            await asyncio.sleep(min(2 ** attempt, 8))
    return None


async def _get_token(username: str, password: str) -> Optional[str]:
    fingerprint = _credential_fingerprint(username, password)
    async with _token_lock:
        cached = _token_cache.get(username)
        if cached:
            token, ts, cached_fingerprint = cached
            if (
                hmac.compare_digest(fingerprint, cached_fingerprint)
                and (datetime.now() - ts).total_seconds() < 22 * 3600
            ):
                return token
            _token_cache.pop(username, None)
        token = await _gdt_login(username, password)
        if token:
            _token_cache[username] = (token, datetime.now(), fingerprint)
        return token


async def _read_gdt_json(url: str, headers: dict) -> tuple[int, Optional[dict]]:
    """
    Đọc response theo từng chunk để có thể cứu một JSON đã nhận đủ dữ liệu khi
    GDT quên gửi chunk kết thúc trước lúc đóng socket.
    """
    async with _gdt.stream("GET", url, headers=headers) as response:
        if response.status_code in (401, 429):
            return response.status_code, None

        response.raise_for_status()
        chunks: list[bytes] = []
        try:
            async for chunk in response.aiter_content():
                chunks.append(chunk)
        except (ChunkedEncodingError, IncompleteRead, RequestException) as exc:
            payload = b"".join(chunks)
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise exc
            log.warning(
                "GDT đóng stream thiếu chunk kết thúc; đã khôi phục JSON hợp lệ (%d bytes)",
                len(payload),
            )
            return response.status_code, data

        return response.status_code, json.loads(b"".join(chunks))


async def _gdt_get(
    url: str,
    username: str,
    password: str,
    max_attempts: int = 5,
) -> dict:
    subject = _log_subject(username)
    for login_round in range(2):
        token = await _get_token(username, password)
        if not token:
            raise RuntimeError(_t("login_failed"))

        for attempt in range(max_attempts):
            try:
                headers = _gdt_headers(
                    **{
                        "Authorization": f"Bearer {token}",
                        "End-Point": "/tra-cuu/tra-cuu-hoa-don",
                    }
                )
                status_code, data = await _read_gdt_json(url, headers)

                if status_code == 429:
                    wait = 5 * (2 ** attempt)
                    log.warning("429 rate-limit — chờ %ds...", wait)
                    await asyncio.sleep(wait)
                    continue

                if status_code == 401:
                    log.warning("[%s] Token hết hạn (401), đăng nhập lại...", subject)
                    _token_cache.pop(username, None)
                    break

                if not isinstance(data, dict):
                    raise json.JSONDecodeError("GDT response không phải JSON object", "", 0)
                return data

            except Exception as e:
                log.warning(
                    "[%s] GDT GET lỗi %s (lần %d/%d): %s",
                    subject,
                    type(e).__name__,
                    attempt + 1,
                    max_attempts,
                    e,
                )
                if attempt == max_attempts - 1:
                    error_type = (
                        _GDTTransportError
                        if isinstance(e, (RequestException, json.JSONDecodeError))
                        else RuntimeError
                    )
                    raise error_type(
                        _t("gdt_request_failed", attempts=max_attempts, error=e)
                    ) from e
                await asyncio.sleep(min(2 ** attempt, 8))

    raise RuntimeError(_t("token_expired"))


class _DetailBatchPaused(RuntimeError):
    """Stop new detail work when GDT rate limits or becomes unavailable."""


class _DetailTransportFailure(RuntimeError):
    """GDT detail transport failed after its bounded retry budget."""


class _DetailRateGate:
    """One shared cooldown and circuit breaker for all workers in a detail batch."""

    def __init__(self) -> None:
        self.cooldown_until = 0.0
        self.consecutive_429 = 0
        self.rate_limited = False
        self.paused = False
        self.pause_code = ""
        self.consecutive_transport_errors = 0
        self.recovery_successes = 0
        self.serial_lock = asyncio.Lock()

    async def wait_ready(self) -> None:
        # Cooldown happens OUTSIDE _gdt_sem: a sleeping worker holds no GDT slot.
        if self.paused:
            raise _DetailBatchPaused(self.pause_code)
        remaining = self.cooldown_until - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(remaining)
        if self.paused:
            raise _DetailBatchPaused(self.pause_code)

    def on_429(self) -> None:
        self.rate_limited = True
        self.recovery_successes = 0
        self.consecutive_429 += 1
        self.cooldown_until = max(
            self.cooldown_until,
            asyncio.get_running_loop().time() + DETAIL_429_COOLDOWN,
        )
        if self.consecutive_429 >= DETAIL_429_ABORT_AFTER:
            self.paused = True
            self.pause_code = "GDT_RATE_LIMITED"

    def on_transport_error(self) -> None:
        self.consecutive_transport_errors += 1
        if self.consecutive_transport_errors >= DETAIL_TRANSPORT_ABORT_AFTER:
            self.paused = True
            self.pause_code = "GDT_UNAVAILABLE"

    def on_success(self) -> None:
        self.consecutive_429 = 0
        self.consecutive_transport_errors = 0
        if self.rate_limited:
            self.recovery_successes += 1
            if self.recovery_successes >= 10:
                # Gradual recovery: return to the small worker pool after
                # 10 consecutive successful serialized requests.
                self.rate_limited = False
                self.recovery_successes = 0
                log.info("GDT detail ổn định sau 10 HĐ; khôi phục worker pool")


async def _gdt_get_detail(
    url: str, username: str, password: str, gate: _DetailRateGate
) -> dict:
    """Bounded detail retries; shared cooldown and serialized requests after a 429."""
    subject = _log_subject(username)
    for login_round in range(2):
        token = await _get_token(username, password)
        if not token:
            raise RuntimeError(_t("login_failed"))
        token_expired = False

        for attempt in range(DETAIL_MAX_ATTEMPTS):
            async def _request_once() -> tuple[int, Optional[dict]]:
                headers = _gdt_headers(**{
                    "Authorization": f"Bearer {token}",
                    "End-Point": "/tra-cuu/tra-cuu-hoa-don",
                })
                # Hold a semaphore slot only for the actual HTTP request.
                async with _gdt_sem:
                    return await asyncio.wait_for(
                        _read_gdt_json(url, headers),
                        timeout=DETAIL_REQUEST_TIMEOUT,
                    )

            try:
                await gate.wait_ready()
                if gate.rate_limited:
                    # After any 429, allow just one in-flight detail request per batch.
                    async with gate.serial_lock:
                        await gate.wait_ready()
                        status_code, data = await _request_once()
                else:
                    status_code, data = await _request_once()
            except _DetailBatchPaused:
                raise
            except Exception as exc:
                log.warning(
                    "[%s] Detail GET lỗi %s (lần %d/%d): %s",
                    subject, type(exc).__name__, attempt + 1,
                    DETAIL_MAX_ATTEMPTS, exc,
                )
                if attempt + 1 >= DETAIL_MAX_ATTEMPTS:
                    raise _DetailTransportFailure(
                        _t("gdt_request_failed", attempts=DETAIL_MAX_ATTEMPTS, error=exc)
                    ) from exc
                await asyncio.sleep(1)
                continue

            if status_code == 429:
                gate.on_429()
                log.warning(
                    "[%s] Detail HTTP 429 (%d/%d); cooldown chung %.1fs, "
                    "giảm về 1 request đồng thời",
                    subject, gate.consecutive_429, DETAIL_429_ABORT_AFTER,
                    DETAIL_429_COOLDOWN,
                )
                if gate.paused:
                    raise _DetailBatchPaused(
                        "GDT liên tục trả HTTP 429; tạm dừng lấy chi tiết"
                    )
                if attempt + 1 >= DETAIL_MAX_ATTEMPTS:
                    raise RuntimeError("GDT trả HTTP 429; có thể thử lại chi tiết sau")
                continue

            if status_code == 401:
                _token_cache.pop(username, None)
                token_expired = True
                log.warning("[%s] Detail token hết hạn (401), đăng nhập lại", subject)
                break

            if not isinstance(data, dict) or "path" in data:
                raise RuntimeError(_t("detail_invalid"))
            gate.on_success()
            return data

        if not token_expired:
            break
    raise RuntimeError(_t("token_expired"))


async def _gdt_get_bytes(
    url: str,
    username: str,
    password: str,
    max_attempts: int = 5,
) -> bytes:
    """Giống _gdt_get() nhưng trả về bytes thô (dùng cho endpoint trả về file zip)."""
    subject = _log_subject(username)
    for login_round in range(2):
        token = await _get_token(username, password)
        if not token:
            raise RuntimeError(_t("login_failed"))

        for attempt in range(max_attempts):
            try:
                headers = _gdt_headers(
                    **{
                        "Authorization": f"Bearer {token}",
                        "End-Point": "/tra-cuu/tra-cuu-hoa-don",
                    }
                )
                r = await _gdt.get(url, headers=headers)

                if r.status_code == 429:
                    wait = 5 * (2 ** attempt)
                    log.warning("429 rate-limit — chờ %ds...", wait)
                    await asyncio.sleep(wait)
                    continue

                if r.status_code == 401:
                    log.warning("[%s] Token hết hạn (401), đăng nhập lại...", subject)
                    _token_cache.pop(username, None)
                    break

                r.raise_for_status()
                return r.content

            except Exception as e:
                log.warning(
                    "[%s] GDT GET bytes lỗi %s (lần %d/%d): %s",
                    subject, type(e).__name__, attempt + 1, max_attempts, e,
                )
                if attempt == max_attempts - 1:
                    raise RuntimeError(
                        _t("gdt_request_failed", attempts=max_attempts, error=e)
                    ) from e
                await asyncio.sleep(min(2 ** attempt, 8))

    raise RuntimeError(_t("token_expired"))


async def _fetch_all_pages(
    base_url: str, query: str, username: str, password: str,
    phase_label: str = "",
    total_so_far: "list[int] | None" = None,   # counter tổng HĐ unique trước nguồn này
    progress_action: str = "fetch-all-invoice",
    source_index: int = 0,
    source_total: int = 0,
    run_id: str = "",
) -> list:
    """
    Phân trang tự động. Cập nhật _progress sau mỗi trang.
    total_found luôn là số unique đã có từ các nguồn trước (monotonic trong run).
    """
    if not phase_label:
        phase_label = _t("list_phase")
    results = []
    state   = ""
    page    = 0
    unique_so_far = total_so_far[0] if total_so_far else 0
    while True:
        page += 1
        _set_progress(username,
            status="fetching",
            action=progress_action,
            phase="list",
            phase_label=phase_label,
            total_found=unique_so_far,
            total_done=unique_so_far,
            total_failed=0,
            source_index=source_index,
            source_total=source_total,
            run_id=run_id,
            detail_done=0,
            detail_total=0,
            current_inv="",
            message=_t(
                "source_searching",
                source_index=source_index,
                source_total=source_total,
                phase_label=phase_label,
                page=page,
            ),
        )
        url = base_url + query + (f"&state={state}" if state else "")
        try:
            async with _gdt_sem:
                # Tối đa 3 lần thử / trang. Nếu vẫn không kết nối được thì trả lỗi
                # cho client, tránh báo thành công với danh sách hóa đơn thiếu.
                resp = await _gdt_get(url, username, password, max_attempts=3)
        except _GDTTransportError as e:
            log.error(
                "[%s] GDT lỗi kết nối danh sách [%s] trang %d sau 3 lần thử: %s",
                _log_subject(username),
                phase_label,
                page,
                e,
            )
            raise
        if not isinstance(resp, dict):
            break
        if "path" in resp and "timestamp" in resp:
            # Lỗi nghiệp vụ GDT trên nguồn này — bỏ nguồn, không fail cả list.
            log.warning(
                "[%s] GDT trả lỗi danh sách [%s] trang %d — bỏ nguồn: %s",
                _log_subject(username),
                phase_label,
                page,
                resp.get("message", "GDT error"),
            )
            break
        results.extend(resp.get("datas") or [])
        state = resp.get("state") or ""
        if not state:
            break
    return results


def _detail_url(khhdon: str, nbmst: str, shdon: str, khmshdon: str) -> str:
    prefix = "sco-query" if len(khhdon) > 3 and khhdon[3] == "M" else "query"
    query = urlencode({
        "nbmst": nbmst,
        "khhdon": khhdon,
        "shdon": shdon,
        "khmshdon": khmshdon,
    })
    return f"{GDT_BASE}/{prefix}/invoices/detail?{query}"


def _xml_url(khhdon: str, nbmst: str, shdon: str, khmshdon: str) -> str:
    prefix = "sco-query" if len(khhdon) > 3 and khhdon[3] == "M" else "query"
    query = urlencode({
        "nbmst": nbmst,
        "khhdon": khhdon,
        "shdon": shdon,
        "khmshdon": khmshdon,
    })
    return f"{GDT_BASE}/{prefix}/invoices/export-xml?{query}"


def _claim_username_job(username: str) -> bool:
    """Chỉ cho phép một job GDT hoạt động trên mỗi username."""
    if username in _active_usernames:
        return False
    _active_usernames.add(username)
    current_task = asyncio.current_task()
    if current_task is not None:
        current_task.add_done_callback(
            lambda _task, active_username=username:
                _active_usernames.discard(active_username)
        )
    return True


def _list_configs(inv_type: int) -> list[tuple[str, str, Optional[int], str]]:
    if inv_type == 1:
        return [
            ("query",     "sold", None, _t("sold")),
            ("sco-query", "sold", None, _t("sold_pos")),
        ]
    return [
        ("query",     "purchase", 5, _t("purchase_coded")),
        ("query",     "purchase", 6, _t("purchase_uncoded")),
        ("sco-query", "purchase", 8, _t("purchase_pos")),
    ]


async def _fetch_invoice_list_core(
    username: str,
    password: str,
    date_from: str,
    date_to: str,
    inv_type: int,
    progress_action: str,
    run_id: str = "",
) -> list[dict]:
    """Lấy, phân trang và deduplicate toàn bộ summary hóa đơn."""
    df = _fmt_date(date_from, end_of_day=False)
    dt = _fmt_date(date_to, end_of_day=True)
    all_items: list[dict] = []
    seen: set[tuple] = set()
    total_counter = [0]
    configs = _list_configs(inv_type)
    source_total = len(configs)

    for source_index, (prefix, endpoint, type_search, label) in enumerate(configs, start=1):
        if type_search is not None:
            query = (
                f"?sort=tdlap:desc&size={GDT_PAGE_SIZE}"
                f"&search=tdlap=ge={df};tdlap=le={dt};ttxly=={type_search}"
            )
        else:
            query = (
                f"?sort=tdlap:desc&size={GDT_PAGE_SIZE}"
                f"&search=tdlap=ge={df};tdlap=le={dt}"
            )

        total_counter[0] = len(all_items)
        try:
            page_items = await _fetch_all_pages(
                f"{GDT_BASE}/{prefix}/invoices/{endpoint}",
                query,
                username,
                password,
                phase_label=label,
                total_so_far=total_counter,
                progress_action=progress_action,
                source_index=source_index,
                source_total=source_total,
                run_id=run_id,
            )
        except _GDTTransportError:
            # Đã thử kết nối đủ 3 lần ở _fetch_all_pages; chuyển lỗi lên endpoint
            # để client nhận success=false thay vì dữ liệu danh sách không đầy đủ.
            raise
        except Exception as e:
            # Không fail cả fetch-invoice-list — bỏ nguồn lỗi, sang nguồn tiếp theo.
            log.warning(
                "[%s] Bỏ qua nguồn %d/%d [%s], tiếp tục: %s",
                _log_subject(username),
                source_index,
                source_total,
                label,
                e,
            )
            page_items = []
        for item in page_items:
            key = (
                item.get("khhdon"),
                str(item.get("shdon", "")),
                item.get("nbmst"),
                str(item.get("khmshdon", "")),
            )
            if key not in seen:
                seen.add(key)
                all_items.append(item)

        # Chỉ tăng (hoặc giữ nguyên) — không bao giờ báo lùi số HĐ trong cùng một run.
        total_counter[0] = len(all_items)
        _set_progress(
            username,
            status="fetching",
            action=progress_action,
            phase="list",
            phase_label=label,
            total_found=len(all_items),
            total_done=len(all_items),
            total_failed=0,
            source_index=source_index,
            source_total=source_total,
            run_id=run_id,
            message=_t(
                "source_done",
                source_index=source_index,
                source_total=source_total,
                label=label,
            ),
        )

    return all_items


async def _fetch_invoice_details_core(
    username: str,
    password: str,
    items: list[dict],
    progress_action: str,
) -> tuple[list[dict], int]:
    """Bounded worker queue, preserving input order and successful partial results."""
    total = len(items)
    done_count = 0
    failed_count = 0
    results: list[Optional[dict]] = [None] * total
    gate = _DetailRateGate()
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(total):
        queue.put_nowait(index)

    def _record(index: int, data: Optional[dict], error_code: str = "") -> None:
        nonlocal done_count, failed_count
        inv = items[index]
        done_count += 1
        if error_code:
            failed_count += 1
        result = {
            **inv,
            "detail": data,
            "detail_error": _t("detail_fetch_failed") if error_code else None,
        }
        if error_code:
            # Existing clients can keep checking detail_error/detail is None;
            # new clients can selectively resubmit only failed items.
            result["detail_error_code"] = error_code
        results[index] = result
        current = f"{inv.get('khhdon', '')}-{inv.get('shdon', '')}"
        _set_progress(
            username,
            status="fetching_detail",
            action=progress_action,
            phase="detail",
            phase_label=_t("detail_phase"),
            total_found=total,
            detail_done=done_count,
            detail_total=total,
            detail_failed=failed_count,
            current_inv=current if not error_code else "",
            message=(
                _t("detail_error_progress",
                   done=done_count, total=total, failed=failed_count)
                if error_code else
                _t("detail_progress", done=done_count, total=total, current=current)
            ),
        )

    async def _worker() -> None:
        while not gate.paused:
            try:
                index = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                inv = items[index]
                url = _detail_url(
                    str(inv.get("khhdon", "")), str(inv.get("nbmst", "")),
                    str(inv.get("shdon", "")), str(inv.get("khmshdon", "")),
                )
                try:
                    data = await _gdt_get_detail(url, username, password, gate)
                    _record(index, data)
                except _DetailBatchPaused:
                    _record(index, None, gate.pause_code or "GDT_RATE_LIMITED")
                except _DetailTransportFailure as exc:
                    gate.on_transport_error()
                    log.warning(
                        "[%s] Detail lỗi kết nối liên tiếp (%d/%d): %s",
                        _log_subject(username), gate.consecutive_transport_errors,
                        DETAIL_TRANSPORT_ABORT_AFTER, type(exc).__name__,
                    )
                    _record(index, None, "DETAIL_FETCH_FAILED")
                except Exception as exc:
                    log.warning(
                        "[%s] Không lấy được chi tiết HĐ (%s)",
                        _log_subject(username), type(exc).__name__,
                    )
                    _record(index, None, "DETAIL_FETCH_FAILED")
            finally:
                queue.task_done()

    worker_count = min(total, DETAIL_WORKERS, MAX_CONCURRENT)
    try:
        await asyncio.wait_for(
            asyncio.gather(*(_worker() for _ in range(worker_count))),
            timeout=DETAIL_BATCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning(
            "[%s] Detail batch quá thời gian %.1fs; trả về dữ liệu partial",
            _log_subject(username), DETAIL_BATCH_TIMEOUT,
        )

    # Do not silently drop invoices when the circuit breaker or deadline fires.
    # Any not-yet-started/in-flight item is returned with an explicit error.
    remaining_code = (
        gate.pause_code if gate.paused else "DETAIL_BATCH_TIMEOUT"
    )
    for index, result in enumerate(results):
        if result is None:
            _record(index, None, remaining_code)
    return [result for result in results if result is not None], failed_count

# ══════════════════════════════════════════════════════════════════════════════
# HTTP Handlers
# ══════════════════════════════════════════════════════════════════════════════

_SECURITY_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@web.middleware
async def security_middleware(
    request: web.Request, handler
) -> web.StreamResponse:
    """
    Chỉ nhận traffic loopback trên mọi endpoint.
    """
    _set_lag(request.rel_url.query.get("Lag", request.rel_url.query.get("lag", "VIET")))
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if peer:
        try:
            if not ipaddress.ip_address(peer[0]).is_loopback:
                return web.json_response(
                    {"success": False, "error": _t("localhost_only")},
                    status=403,
                    headers=_SECURITY_RESPONSE_HEADERS,
                )
        except ValueError:
            return web.json_response(
                {"success": False, "error": _t("client_addr_unknown")},
                status=403,
                headers=_SECURITY_RESPONSE_HEADERS,
            )

    try:
        await asyncio.wait_for(_http_sem.acquire(), timeout=HTTP_QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        return web.json_response(
            {"success": False, "error": _t("agent_overloaded")},
            status=503,
            headers={**_SECURITY_RESPONSE_HEADERS, "Retry-After": "2"},
        )

    try:
        response = await handler(request)
    finally:
        _http_sem.release()

    response.headers.update(_SECURITY_RESPONSE_HEADERS)
    return response


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status":         "ok",
        "host":           AGENT_HOST,
        "port":           AGENT_PORT,
        "max_concurrent": MAX_CONCURRENT,
        "detail_workers": min(DETAIL_WORKERS, MAX_CONCURRENT),
        "detail_max_attempts": DETAIL_MAX_ATTEMPTS,
        "gdt_page_size":  GDT_PAGE_SIZE,
        "active_jobs":    len(_active_usernames),
    })


async def handle_progress(request: web.Request) -> web.Response:
    """
    GET /progress                   → tất cả username
    GET /progress?username=xxx      → chỉ 1 username
    """
    return _progress_response(request, _progress)


def _progress_response(
    request: web.Request, progress_store: dict[str, dict]
) -> web.Response:
    idle = {
        "status": "idle", "action": "", "phase": "", "phase_label": "",
        "total_found": 0, "detail_done": 0, "detail_total": 0,
        "detail_failed": 0, "current_inv": "", "message": "", "updated_at": "",
    }
    username = request.rel_url.query.get("username")
    if username:
        data = {username: progress_store.get(username, idle)}
    else:
        data = progress_store
    return web.json_response(data)


async def handle_progress_invoice_list(request: web.Request) -> web.Response:
    """GET /progress/invoice-list[?username=xxx]."""
    username = request.rel_url.query.get("username")
    idle = {
        "status": "idle",
        "action": "fetch-invoice-list",
        "phase": "",
        "phase_label": "",
        "total_found": 0,
        "total_done": 0,
        "total_failed": 0,
        "message": "",
        "updated_at": "",
    }

    def _list_progress_view(state: dict) -> dict:
        return {
            "status": state.get("status", "idle"),
            "action": state.get("action", "fetch-invoice-list"),
            "phase": state.get("phase", ""),
            "phase_label": state.get("phase_label", ""),
            "total_found": state.get("total_found", 0),
            "total_done": state.get("total_done", state.get("total_found", 0)),
            "total_failed": state.get("total_failed", 0),
            "message": state.get("message", ""),
            "updated_at": state.get("updated_at", ""),
        }

    if username:
        state = _progress_invoice_list.get(username)
        return web.json_response({
            username: _list_progress_view(state) if state is not None else idle
        })
    return web.json_response({
        key: _list_progress_view(state)
        for key, state in _progress_invoice_list.items()
    })


async def handle_progress_invoice_details(request: web.Request) -> web.Response:
    """GET /progress/invoice-details[?username=xxx]."""
    return _progress_response(request, _progress_invoice_details)


async def _read_json_body(request: web.Request) -> tuple[Optional[dict], Optional[web.Response]]:
    # Cho phép Lag trên query string khi chưa đọc được body (middleware/parse lỗi).
    _set_lag(request.rel_url.query.get("Lag", request.rel_url.query.get("lag", "VIET")))
    if request.content_type != "application/json":
        return None, web.json_response(
            {"success": False, "error": _t("content_type_json")},
            status=415,
        )
    try:
        body = await request.json()
    except Exception:
        return None, web.json_response(
            {"success": False, "error": _t("body_must_json")},
            status=400,
        )
    if not isinstance(body, dict):
        return None, web.json_response(
            {"success": False, "error": _t("body_must_object")},
            status=400,
        )
    _apply_lag_from_body(body)
    return body, None


def _extract_credentials(
    body: dict,
) -> tuple[Optional[str], Optional[str], Optional[web.Response]]:
    username_value = body.get("username")
    password_value = body.get("password")
    if not isinstance(username_value, str) or not isinstance(password_value, str):
        return None, None, web.json_response(
            {"success": False, "error": _t("cred_must_str")},
            status=400,
        )
    username = username_value.strip()
    password = password_value
    if not username or not password:
        return None, None, web.json_response(
            {"success": False, "error": _t("missing_cred")},
            status=400,
        )
    return username, password, None


async def handle_download_xml(request: web.Request) -> web.Response:
    """
    POST /download-invoice-xml
    Body: {
      "username":  "0123456789",
      "password":  "mat_khau",
      "nbmst":     "0123456789",
      "khhdon":    "1C22TAA",
      "shdon":     "123",
      "khmshdon":  "01GTKT0/001",
      "Lag":       "VIET"
    }
    Response 200: {"success": true, "xml": "<Invoice>...</Invoice>", "message": "..."}
    Response 4xx: {"success": false, "error": "..."}
    """
    body, error = await _read_json_body(request)
    if error is not None:
        return error
    assert body is not None
    username, password, error = _extract_credentials(body)
    if error is not None:
        return error
    assert username is not None and password is not None

    nbmst    = str(body.get("nbmst",    "")).strip()
    khhdon   = str(body.get("khhdon",   "")).strip()
    shdon    = str(body.get("shdon",    "")).strip()
    khmshdon = str(body.get("khmshdon", "")).strip()

    if not all([nbmst, khhdon, shdon, khmshdon]):
        return web.json_response(
            {"success": False, "error": _t("xml_missing_params")},
            status=400,
        )

    url = _xml_url(khhdon, nbmst, shdon, khmshdon)
    log.info("[%s] Tải XML: %s", _log_subject(username), url)

    try:
        async with _gdt_sem:
            zip_bytes = await _gdt_get_bytes(url, username, password)
    except Exception as exc:
        log.error("[%s] Tải XML thất bại: %s", _log_subject(username), exc)
        return web.json_response(
            {"success": False, "error": str(exc)},
            status=502,
        )

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            xml_content = zf.read("invoice.xml").decode("utf-8")
    except KeyError:
        return web.json_response(
            {"success": False, "error": _t("xml_not_found_in_zip")},
            status=502,
        )
    except Exception as exc:
        return web.json_response(
            {"success": False, "error": f"Giải nén ZIP thất bại: {exc}"},
            status=502,
        )

    return web.json_response({
        "success": True,
        "xml":     xml_content,
        "message": _t("xml_download_ok"),
    })


async def handle_login(request: web.Request) -> web.Response:
    """
    POST /login
    Body: {"username": "...", "password": "...", "Lag": "VIET"}
    Thử đăng nhập GDT và cache token nếu thành công.
    Response 200: {"success": true,  "message": "..."}
    Response 401: {"success": false, "error":   "..."}
    """
    body, error = await _read_json_body(request)
    if error is not None:
        return error
    assert body is not None
    username, password, error = _extract_credentials(body)
    if error is not None:
        return error
    assert username is not None and password is not None

    try:
        token = await _get_token(username, password)
    except Exception as exc:
        log.error("[%s] /login lỗi không mong đợi: %s", _log_subject(username), exc)
        return web.json_response(
            {"success": False, "error": str(exc)},
            status=500,
        )

    if token:
        return web.json_response({"success": True, "message": _t("login_success")})

    return web.json_response(
        {"success": False, "error": _t("login_rejected")},
        status=401,
    )


async def handle_fetch_invoice_list(request: web.Request) -> web.Response:
    """
    POST /fetch-invoice-list
    Body giống /fetch-all-invoice; response chỉ chứa danh sách summary.
    """
    body, error = await _read_json_body(request)
    if error is not None:
        return error
    assert body is not None
    username, password, error = _extract_credentials(body)
    if error is not None:
        return error
    assert username is not None and password is not None

    date_from_value = body.get("date_from")
    date_to_value = body.get("date_to")
    if not isinstance(date_from_value, str) or not isinstance(date_to_value, str):
        return web.json_response(
            {"success": False, "error": _t("date_must_str")},
            status=400,
        )
    date_from = date_from_value.strip()
    date_to = date_to_value.strip()
    try:
        inv_type = int(body.get("inv_type", 1))
    except (TypeError, ValueError):
        return web.json_response(
            {"success": False, "error": _t("inv_type_01")},
            status=400,
        )
    if inv_type not in (0, 1):
        return web.json_response(
            {"success": False, "error": _t("inv_type_buy_sell")},
            status=400,
        )
    try:
        parsed_from = datetime.strptime(date_from.replace("-", ""), "%Y%m%d")
        parsed_to = datetime.strptime(date_to.replace("-", ""), "%Y%m%d")
    except ValueError:
        return web.json_response(
            {"success": False, "error": _t("date_format")},
            status=400,
        )
    if parsed_from > parsed_to:
        return web.json_response(
            {"success": False, "error": _t("date_range")},
            status=400,
        )
    if not _claim_username_job(username):
        return web.json_response(
            {"success": False, "error": _t("job_running")},
            status=409,
        )

    action = "fetch-invoice-list"
    run_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    source_total = len(_list_configs(inv_type))
    # Xóa progress cũ ngay lập tức để client không poll phải 3/3 của lần trước.
    _set_progress(
        username,
        status="starting",
        action=action,
        phase="list",
        phase_label="",
        total_found=0,
        total_done=0,
        total_failed=0,
        source_index=0,
        source_total=source_total,
        run_id=run_id,
        detail_done=0,
        detail_total=0,
        detail_failed=0,
        current_inv="",
        message=_t("starting_list"),
    )
    try:
        items = await _fetch_invoice_list_core(
            username, password, date_from, date_to, inv_type, action, run_id=run_id
        )
    except Exception as e:
        _set_progress(
            username,
            status="error",
            action=action,
            phase="error",
            run_id=run_id,
            message=str(e),
        )
        log.error(
            "[%s] fetch-invoice-list lỗi: %s", _log_subject(username), e
        )
        return web.json_response({"success": False, "error": str(e)}, status=500)

    total = len(items)
    _set_progress(
        username,
        status="done",
        action=action,
        phase="done",
        total_found=total,
        total_done=total,
        total_failed=0,
        source_index=source_total,
        source_total=source_total,
        run_id=run_id,
        message=_t("list_done", total=total),
    )
    log.info(
        "[%s] fetch-invoice-list hoàn thành: %d HĐ %s→%s",
        _log_subject(username), total, date_from, date_to,
    )
    return web.json_response({"success": True, "count": total, "items": items})


async def handle_fetch_invoice_details(request: web.Request) -> web.Response:
    """
    POST /fetch-invoice-details
    Nhận mảng items từ /fetch-invoice-list và gắn detail cho từng hóa đơn.
    """
    body, error = await _read_json_body(request)
    if error is not None:
        return error
    assert body is not None
    username, password, error = _extract_credentials(body)
    if error is not None:
        return error
    assert username is not None and password is not None

    items = body.get("items")
    if not isinstance(items, list):
        return web.json_response(
            {"success": False, "error": _t("items_must_array")},
            status=400,
        )
    if len(items) > MAX_DETAIL_ITEMS:
        return web.json_response(
            {
                "success": False,
                "error": _t("items_limit", max=MAX_DETAIL_ITEMS),
            },
            status=413,
        )
    required_fields = ("khhdon", "nbmst", "shdon", "khmshdon")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return web.json_response(
                {"success": False, "error": _t("item_must_object", index=index)},
                status=400,
            )
        for field in required_fields:
            value = item.get(field)
            if value is None or not isinstance(value, (str, int)) or not str(value).strip():
                return web.json_response(
                    {
                        "success": False,
                        "error": _t("item_field_invalid", index=index, field=field),
                    },
                    status=400,
                )
            if len(str(value)) > 100:
                return web.json_response(
                    {
                        "success": False,
                        "error": _t("item_field_too_long", index=index, field=field),
                    },
                    status=400,
                )
    if not _claim_username_job(username):
        return web.json_response(
            {"success": False, "error": _t("job_running")},
            status=409,
        )

    action = "fetch-invoice-details"
    total = len(items)
    _set_progress(
        username,
        status="starting",
        action=action,
        phase="detail",
        total_found=total,
        detail_total=total,
        message=_t("preparing_detail", total=total),
    )
    try:
        results, failed_count = await _fetch_invoice_details_core(
            username, password, items, action
        )
    except Exception as e:
        _set_progress(
            username, status="error", action=action, phase="error", message=str(e)
        )
        log.error(
            "[%s] fetch-invoice-details lỗi: %s", _log_subject(username), e
        )
        return web.json_response({"success": False, "error": str(e)}, status=500)

    is_partial = failed_count > 0
    _set_progress(
        username,
        status="partial" if is_partial else "done",
        action=action,
        phase="done",
        total_found=total,
        detail_done=total,
        detail_total=total,
        detail_failed=failed_count,
        message=(
            _t("partial_detail_done", failed=failed_count, total=total)
            if is_partial else _t("detail_done", total=total)
        ),
    )
    payload = {
        "success": not is_partial,
        "partial": is_partial,
        "count": total,
        "detail_error_count": failed_count,
        "items": results,
    }
    return web.json_response(payload, status=207 if is_partial else 200)


async def handle_fetch_all_invoice(request: web.Request) -> web.Response:
    """
    POST /fetch-all-invoice
    Gộp fetch-list + fetch-detail thành 1 lần gọi.
    Tự động lấy toàn bộ danh sách (tất cả trang, tất cả type_search),
    sau đó lấy chi tiết từng HĐ song song (giới hạn bởi MAX_CONCURRENT).

    Body:
      username   — tài khoản GDT
      password   — mật khẩu GDT
      date_from  — "20260101"
      date_to    — "20260731"
      inv_type   — 0 (mua vào) | 1 (bán ra)
      Lag        — "VIET" | "ENG" | "KOR" | "CHINA" (mặc định VIET)

    Response:
      {
        "success": true,
        "count":   120,
        "items": [
          { ...summary fields..., "detail": { ...GDT detail... } },
          ...
        ]
      }
    """
    # Cho phép Lag trên query string khi chưa đọc được body.
    _set_lag(request.rel_url.query.get("Lag", request.rel_url.query.get("lag", "VIET")))
    if request.content_type != "application/json":
        return web.json_response(
            {"success": False, "error": _t("content_type_json")},
            status=415,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": _t("body_must_json")}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"success": False, "error": _t("body_must_object")}, status=400)

    _apply_lag_from_body(body)

    username_value = body.get("username")
    password_value = body.get("password")
    date_from_value = body.get("date_from")
    date_to_value = body.get("date_to")
    if not all(isinstance(v, str) for v in (
        username_value, password_value, date_from_value, date_to_value
    )):
        return web.json_response(
            {
                "success": False,
                "error": _t("fields_must_str"),
            },
            status=400,
        )

    username  = username_value.strip()
    password  = password_value
    date_from = date_from_value.strip()
    date_to   = date_to_value.strip()
    try:
        inv_type = int(body.get("inv_type", 1))
    except (TypeError, ValueError):
        return web.json_response(
            {"success": False, "error": _t("inv_type_01")},
            status=400,
        )

    if not all([username, password, date_from, date_to]):
        return web.json_response(
            {"success": False, "error": _t("missing_fields")},
            status=400,
        )
    if inv_type not in (0, 1):
        return web.json_response(
            {"success": False, "error": _t("inv_type_buy_sell")},
            status=400,
        )

    try:
        parsed_from = datetime.strptime(date_from.replace("-", ""), "%Y%m%d")
        parsed_to = datetime.strptime(date_to.replace("-", ""), "%Y%m%d")
    except ValueError:
        return web.json_response(
            {"success": False, "error": _t("date_format")},
            status=400,
        )
    if parsed_from > parsed_to:
        return web.json_response(
            {"success": False, "error": _t("date_range")},
            status=400,
        )

    if not _claim_username_job(username):
        return web.json_response(
            {"success": False, "error": _t("job_running")},
            status=409,
        )

    action = "fetch-all-invoice"
    _set_progress(
        username, status="starting", action=action, message=_t("starting")
    )

    try:
        all_items = await _fetch_invoice_list_core(
            username, password, date_from, date_to, inv_type, action
        )
    except Exception as e:
        _set_progress(
            username, status="error", action=action, phase="error", message=str(e)
        )
        log.error("[%s] fetch-all-invoice (list) lỗi: %s", _log_subject(username), e)
        return web.json_response({"success": False, "error": str(e)}, status=500)

    total = len(all_items)
    log.info(
        "[%s] fetch-all-invoice: %d HĐ unique, bắt đầu lấy chi tiết...",
        _log_subject(username), total,
    )

    try:
        results, failed_count = await _fetch_invoice_details_core(
            username, password, all_items, action
        )
    except Exception as e:
        _set_progress(
            username, status="error", action=action, phase="error", message=str(e)
        )
        log.error("[%s] fetch-all-invoice (detail) lỗi: %s", _log_subject(username), e)
        return web.json_response({"success": False, "error": str(e)}, status=500)

    is_partial = failed_count > 0
    _set_progress(username,
        status="partial" if is_partial else "done",
        action=action,
        phase="done",
        phase_label="",
        total_found=total,
        detail_done=total,
        detail_total=total,
        detail_failed=failed_count,
        current_inv="",
        message=(
            _t("partial_detail_done", failed=failed_count, total=total)
            if is_partial else _t("all_done", total=total)
        ),
    )
    log.info(
        "[%s] fetch-all-invoice hoàn thành: %d HĐ, %d lỗi chi tiết, %s→%s",
        _log_subject(username), total, failed_count, date_from, date_to,
    )
    payload = {
        "success": not is_partial,
        "partial": is_partial,
        "count": total,
        "detail_error_count": failed_count,
        "items": results,
    }
    return web.json_response(payload, status=207 if is_partial else 200)


# ══════════════════════════════════════════════════════════════════════════════
# System Tray Icon (pystray + Pillow)
# Chạy trên main thread — ẩn cửa sổ CMD, hiện icon ở góc phải taskbar Windows
# ══════════════════════════════════════════════════════════════════════════════

# def _make_icon_image():
#     """Tạo icon 64×64 màu xanh — không cần file ảnh ngoài."""
#     from PIL import Image, ImageDraw
#     img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
#     draw = ImageDraw.Draw(img)
#     # Nền tròn xanh dương
#     draw.ellipse([2, 2, 62, 62], fill="#1565C0")
#     # Chữ "A" trắng đơn giản bằng đường thẳng
#     draw.line([(20, 50), (32, 14)], fill="white", width=5)
#     draw.line([(44, 50), (32, 14)], fill="white", width=5)
#     draw.line([(23, 36), (41, 36)], fill="white", width=4)
#     return img

def _make_icon_image():
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    blue = "#1565C0"
    white = "#FFFFFF"

    # Mũi tên lớn, cực ít padding
    draw.arc([2, 2, 62, 62], start=205, end=338, fill=blue, width=10)
    draw.arc([2, 2, 62, 62], start=22, end=155, fill=blue, width=10)

    draw.polygon([(51, 7), (63, 12), (54, 24)], fill=blue)
    draw.polygon([(13, 57), (1, 52), (10, 40)], fill=blue)

    # Hộp trắng lớn hơn
    draw.rounded_rectangle([12, 17, 52, 47], radius=8, fill=white)

    # AM thật to
    try:
        font = ImageFont.truetype("arialbd.ttf", 24)
    except:
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
        except:
            font = ImageFont.load_default()

    text = "AM"
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]

    x = (64 - w) / 2
    y = (64 - h) / 2 - 2

    draw.text((x, y), text, font=font, fill=blue)

    return img

def _run_tray() -> None:
    """Hiện system tray icon. Block main thread cho đến khi user chọn Thoát."""
    try:
        import pystray

        def _open_log(icon, item):
            import subprocess
            subprocess.Popen(["notepad.exe", _log_file])

        def _show_version(icon, item):
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"AMnote GDT\n\nPhiên bản: {app_version}\nCổng: {AGENT_PORT}",
                    "Thông tin phiên bản",
                    0x40,  # MB_ICONINFORMATION
                )
            except Exception as e:
                log.warning("Không hiển thị được hộp thoại phiên bản (%s)", e)

        def _on_exit(icon, item):
            icon.stop()
            os._exit(0)

        app_version = _get_app_version()
        img  = _make_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem(f"AMnote GDT  ·  :{AGENT_PORT}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Phiên bản: {app_version}", _show_version),
            pystray.MenuItem("Xem file log", _open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Thoát", _on_exit),
        )
        icon = pystray.Icon("AMnote GDT", img, f"AMnote GDT v{app_version} :{AGENT_PORT}", menu)
        log.info("System tray icon đã khởi động")
        icon.run()          # block until _on_exit

    except Exception as e:
        # Fallback nếu pystray không có (chạy .py trực tiếp không install pystray)
        log.warning("Không hiện được system tray (%s) — dùng Ctrl+C để dừng", e)
        threading.Event().wait()   # block mãi


# ══════════════════════════════════════════════════════════════════════════════
# Async server (chạy trong background thread)
# ══════════════════════════════════════════════════════════════════════════════

async def _amain() -> None:
    global _gdt_sem, _http_sem
    _gdt_sem = asyncio.Semaphore(MAX_CONCURRENT)
    _http_sem = asyncio.Semaphore(MAX_HTTP_CONCURRENT)

    log.info("=" * 55)
    log.info("AMnote GDT")
    log.info("  Port          : %d", AGENT_PORT)
    log.info("  Max concurrent: %d", MAX_CONCURRENT)
    log.info("  Detail workers: %d", min(MAX_CONCURRENT, DETAIL_WORKERS))
    log.info("  Detail retries: %d attempts, %.1fs timeout", DETAIL_MAX_ATTEMPTS, DETAIL_REQUEST_TIMEOUT)
    log.info("  GDT page size : %d", GDT_PAGE_SIZE)
    log.info("  Max HTTP      : %d", MAX_HTTP_CONCURRENT)
    log.info("  Log file      : %s", _log_file)
    log.info("=" * 55)

    app = web.Application(
        middlewares=[security_middleware],
        client_max_size=MAX_REQUEST_BODY_MB * 1024 * 1024,
    )
    app.router.add_get( "/health",              handle_health)
    app.router.add_post("/login",               handle_login)
    app.router.add_post("/download-invoice-xml", handle_download_xml)
    app.router.add_get( "/progress",            handle_progress)
    app.router.add_get( "/progress/invoice-list", handle_progress_invoice_list)
    app.router.add_get( "/progress/invoice-details", handle_progress_invoice_details)
    app.router.add_post("/fetch-all-invoice",   handle_fetch_all_invoice)
    app.router.add_post("/fetch-invoice-list",  handle_fetch_invoice_list)
    app.router.add_post("/fetch-invoice-details", handle_fetch_invoice_details)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, AGENT_HOST, AGENT_PORT)
    await site.start()

    log.info("Agent sẵn sàng tại http://%s:%d", AGENT_HOST, AGENT_PORT)
    await asyncio.Event().wait()   # chạy mãi


def _server_thread() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    # Server chạy trong daemon thread riêng
    t = threading.Thread(target=_server_thread, daemon=True, name="agent-server")
    t.start()

    # System tray chạy trên main thread (blocking)
    _run_tray()
