# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (プランA: 超高速・安定版)
# 目標: 2.0s/件 前後 (全エリア 4分台完了)
# ==========================================================
import sys
import os
import pandas as pd
import gspread
from time import sleep, time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# 1. ログイン情報設定
LOGIN_URL = "https://dailycheck.tc-extsys.jp/tcrappsweb/web/login/tawLogin.html"
USER_ID_1 = "0030"
USER_ID_2 = "927583"
PASSWORD = "Ccj-222223"

# 2. 設定
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/13cQngK_Xx38VU67yLS-iTHyOZgsACZdxM34l-Jq_U9A/edit"
CSV_FILE_NAME = "station_code_map.csv"

# 3. Google認証
SERVICE_ACCOUNT_KEY_FILE = "service_account.json"
if not os.path.exists(SERVICE_ACCOUNT_KEY_FILE):
    print("!! エラー: 認証キーファイルが見つかりません。")
    sys.exit(1)

gc = gspread.service_account(filename=SERVICE_ACCOUNT_KEY_FILE)

# ==========================================================
# I. リスト読み込み
# ==========================================================
print(f"\n[I.リスト読み込み] '{CSV_FILE_NAME}' を読み込みます...")
df_map = pd.read_csv(CSV_FILE_NAME)
df_map.columns = df_map.columns.str.strip()
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})

target_stations = df_map.drop_duplicates(subset=['stationCd']).to_dict('records')
print(f"-> 巡回対象: {len(target_stations)} カ所")

# ==========================================================
# ドライバ設定
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--window-size=1920,1080')
options.add_experimental_option("prefs", {
    "profile.managed_default_content_settings.images": 2,
    "profile.managed_default_content_settings.stylesheets": 2
})
options.page_load_strategy = 'eager'

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
collected_data = []

def perform_login():
    """元の確実な手順を再現"""
    print("-> ログインを開始します...")
    driver.get(LOGIN_URL)
    sleep(5)
    try:
        driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
        driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
        sleep(7)
        driver.get("https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html")
        sleep(3)
        print("   -> ログイン成功")
    except Exception as e:
        print(f"!! ログイン失敗(即時停止): {e}")
        sys.exit(1)

# ==========================================================
# II. データ収集 (超高速要素検知)
# ==========================================================
try:
    sh_prod = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
    perform_login()

    for i, item in enumerate(target_stations):
        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        area = str(item.get('city', 'other')).strip()

        target_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        driver.get(target_url)

        # 高速要素検知ループ (最大5秒)
        found = False
        start_wait = time()
        while time() - start_wait < 5:
            # ログイン画面へ戻されたか
            if driver.find_elements(By.ID, "cardNo1"):
                print("   [!] セッション切れ検知。再ログイン。")
                perform_login()
                driver.get(target_url)
                start_wait = time()
                continue
            
            # 車両リストがあるか
            car_boxes = driver.find_elements(By.CLASS_NAME, "car-list-box")
            if car_boxes:
                found = True
                break
        
        if not found:
            print(f"!! 要素未検出(即時停止): {station_name}")
            sys.exit(1)

        # 時刻取得 (Selenium経由で直接)
        start_time_str = "00:00"
        try:
            timeline_cell = driver.find_element(By.CLASS_NAME, "timeline")
            h = timeline_cell.text.strip()
            start_time_str = f"{h}:00" if h.isdigit() else h
        except: pass

        # 車両データ解析 (BeautifulSoupを使わず直接属性を取得)
        for box in car_boxes:
            title_text = box.find_element(By.CLASS_NAME, "car-list-title-area").text.strip()
            parts = title_text.split(" / ") if " / " in title_text else [title_text, ""]
            plate, model = parts[0].strip(), parts[1].strip()

            # ステータス行のセルを一括取得
            try:
                # 3行目のtdを直接指定
                cells = box.find_elements(By.XPATH, ".//table[@class='timetable']//tr[3]/td")
                status_list = []
                for cell in cells:
                    cls = cell.get_attribute("class")
                    sym = "s" if "impossible" in cls else ("○" if "vacant" in cls else "×")
                    colspan = int(cell.get_attribute("colspan") or 1)
                    status_list.extend([sym] * colspan)
                
                if len(status_list) < 288:
                    status_list.extend(["×"] * (288 - len(status_list)))
                
                collected_data.append([area, station_name, plate, model, start_time_str, "".join(status_list)])
            except:
                print(f"!! 解析エラー(即時停止): {plate}")
                sys.exit(1)
        
        print(f"[{i+1}/{len(target_stations)}] {station_name} OK")

    # ==========================================================
    # III. 保存
    # ==========================================================
    if collected_data:
        print("\n[III.データ保存] シートへ書き込み中...")
        df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])
        for area in df_output['city'].unique():
            df_area = df_output[df_output['city'] == area].copy()
            work_sheet_name = f"{str(area).replace('市', '').strip()}_更新用"
            df_to_write = df_area.drop(columns=['city'])
            try: ws_work = sh_prod.worksheet(work_sheet_name)
            except gspread.WorksheetNotFound: ws_work = sh_prod.add_worksheet(title=work_sheet_name, rows=len(df_area)+10, cols=10)
            ws_work.clear()
            ws_work.update([df_to_write.columns.values.tolist()] + df_to_write.values.tolist(), range_name='A1')
    
except Exception as e:
    print(f"\n!! エラー発生: {e}")
    sys.exit(1)

finally:
    if 'driver' in locals():
        driver.quit()
