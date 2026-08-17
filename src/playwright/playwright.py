import json
from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

stealth = Stealth()

BASE_URL = "https://muasamcong.mpi.gov.vn"
MSC_URL = f"{BASE_URL}/web/guest/contractor-selection?render=index"

# Headers bắt buộc để tránh bị WAF reset connection
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": MSC_URL,
}

# =========================================================
# RECAPTCHA TOKEN
# =========================================================

async def get_recaptcha_token():
    async with async_playwright() as p:
        # Bỏ --single-process và --no-zygote để tránh crash
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=DEFAULT_HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        await stealth.apply_stealth_async(page)

        async with page.expect_response(
            lambda response: "/recaptcha/api2/reload" in response.url,
            timeout=30000,
        ) as response_info:
            await page.goto(MSC_URL, wait_until="domcontentloaded", timeout=60000)

        response = await response_info.value
        body = await response.text()
        await browser.close()
        
        data = json.loads(body.replace(")]}'", "").strip())
        return data[1]


# =========================================================
# API CALLS (Sử dụng AsyncSession & impersonate chrome124)
# =========================================================

async def get_server_time():
    url = f"{BASE_URL}/o/egp-portal-personal-page/services/get-time-now"
    async with AsyncSession(impersonate="chrome124") as session:
        response = await session.post(url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        return response.json().get("body")


async def get_list_tbmt(token: str, query: dict):
    # Đưa token trực tiếp vào URL query string như bên JS
    url = f"{BASE_URL}/o/egp-portal-contractor-selection-v2/services/smart/search?token={token}"
    async with AsyncSession(impersonate="chrome124") as session:
        response = await session.post(
            url,
            json=query,
            headers=DEFAULT_HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


async def get_detail_tbmt(token: str, notify_id: str):
    url = f"{BASE_URL}/o/egp-portal-contractor-selection-v2/services/lcnt_tbmt_ttc_ldt?token={token}"
    async with AsyncSession(impersonate="chrome124") as session:
        response = await session.post(
            url,
            json={"id": notify_id},
            headers=DEFAULT_HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


async def get_detail_khlcnt(token: str, plan_id: str):
    url = f"{BASE_URL}/o/egp-portal-contractor-selection-v2/services/expose/lcnt/bid-po-bidp-plan-project-view/get-by-id?token={token}"
    async with AsyncSession(impersonate="chrome124") as session:
        response = await session.post(
            url,
            json={"id": plan_id},
            headers=DEFAULT_HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

