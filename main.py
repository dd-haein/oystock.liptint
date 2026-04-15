import asyncio
import re
import os
import requests
import json
import gspread
from datetime import datetime
from oauth2client.service_account import ServiceAccountCredentials
from playwright.async_api import async_playwright

# 시작 로그
print("🎬 [시스템 시작] 구글 시트 및 슬랙 통합 버전을 실행합니다.")

TARGET_URL = os.environ.get("TARGET_URL")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
GOOGLE_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT")

def update_google_sheet(inventory_dict):
    """구글 시트에 데이터를 기록하는 함수 (독립 실행)"""
    if not GOOGLE_JSON:
        print("⚠️ GOOGLE_SERVICE_ACCOUNT 시크릿 설정이 누락되었습니다.")
        return

    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(GOOGLE_JSON)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # 시트 URL 확인
        sheet_url = "https://docs.google.com/spreadsheets/d/1XEZj9FCbJKP5Gt9ZquK09meDgVHcbZGqvgleFOaBk2U/edit"
        doc = client.open_by_url(sheet_url)
        worksheet = doc.get_worksheet(0)

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        new_rows = []
        for opt_name, stock_val in inventory_dict.items():
            num_stock = 0
            if "재고" in stock_val:
                match = re.search(r'\d+', stock_val)
                num_stock = int(match.group()) if match else 999
            elif "품절" in stock_val:
                num_stock = 0
            else:
                num_stock = -1 # 확인 불가 상태
            
            new_rows.append([date_str, time_str, opt_name, num_stock])
        
        worksheet.append_rows(new_rows)
        print("✅ 구글 시트 업데이트 성공!")
    except Exception as e:
        print(f"❌ 구글 시트 업데이트 중 에러 발생: {e}")

async def get_inventory():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 1024},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        inventory_dict = {}
        slack_results = []
        
        try:
            print(f"🚀 {TARGET_URL} 접속 중...")
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(10)

            opt_btn_sel = 'button:has-text("선택"), button[class*="OptionSelector_btn-option"]'
            await page.wait_for_selector(opt_btn_sel, timeout=20000)
            await page.click(opt_btn_sel)
            await page.wait_for_selector('li[class*="OptionSelector_option-item"]', state="visible", timeout=15000)
            
            items_count = await page.locator('li[class*="OptionSelector_option-item"]').count()
            print(f"📦 총 {items_count}개 옵션 탐색 시작")

            for i in range(items_count):
                if not await page.locator('li[class*="OptionSelector_option-item"]').first.is_visible():
                    await page.click(opt_btn_sel)
                    await asyncio.sleep(2)

                items = await page.locator('li[class*="OptionSelector_option-item"]').all()
                target = items[i]
                opt_name = (await target.locator('span[class*="option-item-tit"]').inner_text()).strip()

                class_attr = await target.get_attribute("class") or ""
                if "is-soldout" in class_attr:
                    inventory_dict[opt_name] = "품절"
                    slack_results.append(f"{opt_name} : 품절")
                    continue

                await target.scroll_into_view_if_needed()
                await target.click(force=True)
                
                input_sel = 'input[data-qa-name="input-product-number"], input[class*="QuantityCounter_count"]'
                try:
                    await page.wait_for_selector(input_sel, timeout=5000)
                    input_field = page.locator(input_sel).first
                    await input_field.fill("999")
                    await page.keyboard.press("Enter")
                    
                    stock_res = "확인 불가"
                    try:
                        toast_sel = 'div[class*="Toast_toast-inner"]'
                        await page.wait_for_selector(toast_sel, timeout=4000)
                        t_text = await page.inner_text(toast_sel)
                        if "재고" in t_text:
                            m = re.search(r'\d+', t_text)
                            stock_res = f"재고 {m.group()}개"
                    except:
                        stock_res = "재고 999+ 예상"
                    
                    inventory_dict[opt_name] = stock_res
                    slack_results.append(f"{opt_name} : {stock_res}")
                    
                    del_btn = page.locator('button[class*="OptionSelector_btn-delete"]').first
                    if await del_btn.is_visible():
                        await del_btn.click()
                        await asyncio.sleep(1.5)
                except:
                    inventory_dict[opt_name] = "확인 불가"
                    slack_results.append(f"{opt_name} : 확인 불가")
                    await page.keyboard.press("Escape")

            return inventory_dict, slack_results

        except Exception as e:
            print(f"🚨 크롤링 중 에러: {e}")
            return inventory_dict, slack_results
        finally:
            await browser.close()

def send_slack(msg_list):
    if not msg_list or not SLACK_WEBHOOK_URL:
        print("⚠️ 슬랙 메시지 전송 스킵 (데이터 없음 혹은 URL 누락)")
        return
    report = "\n".join([f"• {m}" for m in msg_list])
    payload = {"text": f"📊 *올리브영 실시간 재고 리포트*\n{report}"}
    requests.post(SLACK_WEBHOOK_URL, json=payload)
    print("📢 슬랙 메시지 전송 완료!")

if __name__ == "__main__":
    # 1. 재고 체크 실행
    inv_data, slack_msg = asyncio.run(get_inventory())
    
    # 2. 구글 시트 업데이트 (실패해도 슬랙은 가도록 별도 처리)
    if inv_data:
        update_google_sheet(inv_data)
    
    # 3. 슬랙 전송
    send_slack(slack_msg)
