import json
import os
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import asyncio
import shutil
from pathlib import Path
from typing import Optional
from curl_cffi.requests import  AsyncSession
import re

from src.helpers import sanitize_name


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
VIEWER_URL = "https://muasamcong.mpi.gov.vn/egp/contractorfe/viewer"
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

TARGET_CODES = {"BD.DT.02.0792", "BD.CG.02.0116", "BD.CG.02.0104"}

FORM_FILE_KEY_MAP = {
    "BD.CG.02.0104": "fileName2",
    "BD.CG.02.0116": "file",
    "BD.DT.02.0792": "fileTech",
}

CODE_RENAME_MAP = {
    "BD.CG.02.0104": "TCDG",
    "BD.CG.02.0116": "YCKT",
    "BD.DT.02.0792": "YCKT",
}

STORAGE_DIR = Path("storage")


headers = {
    "Origin": "https://muasamcong.mpi.gov.vn",
    "Referer": "https://muasamcong.mpi.gov.vn/",
}


async def download_file(
    file_id: str,
    ma_tbmt: Optional[str] = None,
    file_name: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> Optional[str]:
    params = {"fileId": file_id}



    target_dir = STORAGE_DIR / sanitize_name(str(ma_tbmt))
    os.makedirs(target_dir, exist_ok=True)

    safe_file_name = sanitize_name(file_name or f"{file_id}.pdf")
    file_path = os.path.join(target_dir, safe_file_name)

    async def _do_download(client: AsyncSession) -> Optional[str]:
        try:
            resp = await client.get(
                url="http://localhost:1234/api/download/file/browser/public",
                params=params,
                stream=True,
                impersonate="chrome120",
                headers=headers,
                timeout=60,
            )
            if resp.status_code == 200:
                with open(file_path, "wb") as f:
                    async for chunk in resp.aiter_content(chunk_size=16384):
                        if chunk:
                            f.write(chunk)
                return file_path
        except Exception:
            return None

    if session:
        return await _do_download(session)

    async with AsyncSession(impersonate="chrome120") as local_session:
        return await _do_download(local_session)


async def download_hsmt(item_id: str, ma_tbmt: str) -> tuple[bool, int]:
    target_dir = STORAGE_DIR / sanitize_name(str(ma_tbmt))
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0

    try:
        # 1. TẢI FILE HSMT CHÍNH QUA PLAYWRIGHT
        async with async_playwright() as p:
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
                user_agent=DEFAULT_HEADERS.get("User-Agent", ""),
                viewport={"width": 1920, "height": 1080},
            )

            storage_json = json.dumps({"param_hsmt": {"formCode": "ALL", "id": str(item_id)}})
            init_script = f"""
                (() => {{
                    const storage = {storage_json};
                    for (const [key, value] of Object.entries(storage)) {{
                        window.localStorage.setItem(key, typeof value === 'string' ? value : JSON.stringify(value));
                    }}
                }})();
            """
            await context.add_init_script(init_script)

            page = await context.new_page()
            await stealth.apply_stealth_async(page)

            try:
                async with page.expect_response(
                    lambda res: res.url.startswith("blob:https://muasamcong.mpi.gov.vn/")
                    and res.status == 200
                    and "application/pdf" in res.headers.get("content-type", ""),
                    timeout=45000,
                ) as response_info:
                    await page.goto(VIEWER_URL, wait_until="domcontentloaded", timeout=60000)

                response = await response_info.value
                pdf_bytes = await response.body()

                if pdf_bytes:
                    hsmt_file_path = target_dir / f"{ma_tbmt}_HSMT.pdf"
                    with open(hsmt_file_path, "wb") as f:
                        f.write(pdf_bytes)
                    downloaded_count += 1
            except Exception :
                pass
            finally:
                await context.close()
                await browser.close()

        # 2. LẤY METADATA VÀ TẢI FILE ĐÍNH KÈM QUA API
        token = await get_recaptcha_token()  
        payload = {"id": item_id, "processApply": "LDT"}

        async with AsyncSession(impersonate="chrome120") as session:
            resp = await session.post(
                url="https://muasamcong.mpi.gov.vn/o/egp-portal-contractor-selection-v2/services/lcnt_tbmt_hsmt",
                params={"token": token},
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()  
            result = resp.json()
            bidding_list = result.get("bidoInvBiddingDTO") or []
            downloaded_file_ids = set()

            for bid in bidding_list:
                form_code = bid.get("formCode")
                raw_val = bid.get("formValue")
                if form_code not in TARGET_CODES or not raw_val:
                    continue

                try:
                    val_obj = json.loads(raw_val)
                except Exception:
                    continue

                file_map = {
                    v["fileId"]: v.get("fileName")
                    for v in val_obj.values()
                    if isinstance(v, dict) and v.get("fileId")
                }
                


                # 2.1 File chính
                target_key = FORM_FILE_KEY_MAP.get(form_code)
                main_file_obj = val_obj.get(target_key)
               
                if isinstance(main_file_obj, dict):
                    main_file_id = main_file_obj.get("fileId")
                    original_name = main_file_obj.get("fileName", "")

                    if main_file_id and main_file_id not in downloaded_file_ids:
                        ext = os.path.splitext(original_name)[1] if original_name else ""
                        prefix = CODE_RENAME_MAP.get(form_code, "FILE")
                        new_file_name = f"{ma_tbmt}_{prefix}{ext}"

                        saved_path = await download_file(
                            file_id=main_file_id,
                            ma_tbmt=ma_tbmt,
                            file_name=new_file_name,
                            session=session,
                        )
                        if saved_path:
                            downloaded_file_ids.add(main_file_id)
                            downloaded_count += 1
                            await asyncio.sleep(1)

                       

                # 2.2 File phụ trong sharedFiles
                for sf_id in val_obj.get("sharedFiles") or []:
                    if not sf_id or sf_id in downloaded_file_ids:
                        continue
                    
                    raw_file_name = file_map.get(sf_id)
                    original_name = raw_file_name if raw_file_name else f"{sf_id}.pdf"
                    keep_file_name = f"{ma_tbmt}_{original_name}"
                    
                 
                    saved_path = await download_file(
                        file_id=sf_id,
                        ma_tbmt=ma_tbmt,
                        file_name=keep_file_name,
                        session=session,
                    )
                    if saved_path:
                        downloaded_file_ids.add(sf_id)
                        downloaded_count += 1
                        await asyncio.sleep(1)

                    print(saved_path)

        # 3. KIỂM TRA TỔNG KẾT
        if downloaded_count == 0:
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            return False, 0

        return True, downloaded_count

    except Exception:
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        return False, 0