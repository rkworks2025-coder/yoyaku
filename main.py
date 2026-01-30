# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (プランA: 全件収集・高速版)
# 機能: 指示外のフィルタリングを全廃、全ステーションを最速で巡回
# ==========================================================
import sys
import os
import pandas as pd
import gspread
from time import sleep
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

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
# I. リスト読み込み (フィルタリングなし・全件取得)
# ==========================================================
print(f"\n[I.リスト読み込み] '{CSV_FILE_NAME}' を読み込みます...")
df_map = pd.read_csv(CSV_FILE_NAME)
df_map.columns = df_map.columns.str.strip()

# 列名の正規化のみ実施
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})

# ★修正: status列の有無に関わらず、重複を除いた全件を巡回対象とする
target_stations = df_map.drop_duplicates(subset=['stationCd']).to_dict('records')
print(f"-> 巡回対象: {len(target_stations)} カ所 (全件収集)")

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
# 高速化: DOMの構築完了で制御を戻す
options.page_load_strategy = 'eager'

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
wait = WebDriverWait(driver, 10)
collected_data = []

def perform_login():
    """元のログイン手順(main20260110.py)を忠実に再現"""
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
# II. データ収集
# ==========================================================
try:
    prod_sh_key = PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
    sh_prod = gc.open_by_key(prod_sh_key)

    # 初回ログイン
    perform_login()

    for i, item in enumerate(target_stations):
        # 進捗保存 (20件ごと)
        if (i > 0) and (i % 20 == 0):
            try:
                try: ws_status = sh_prod.worksheet("SystemStatus")
                except: ws_status = sh_prod.add_worksheet(title="SystemStatus", rows=5, cols=5)
                ws_status.update([["progress", i, len(target_stations)]], "A1")
                print(f"--- 進捗: {i}/{len(target_stations)} ---")
            except: pass

        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        area = str(item.get('city', 'other')).strip()

        target_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        driver.get(target_url)

        # ログイン画面判定 (cardNo1があるか)
        login_check = driver.find_elements(By.ID, "cardNo1")
        if login_check:
            print("   [!] セッション切れ検知。再ログイン。")
            perform_login()
            driver.get(target_url)

        # 要素の出現を待機 (固定sleep廃止による高速化)
        try:
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "car-list-box")))
        except:
            print(f"!! ページ構造エラー(即時停止): {station_name}")
            sys.exit(1)

        soup = BeautifulSoup(driver.page_source, "lxml")
        car_boxes = soup.find_all("div", class_="car-list-box")
        
        if not car_boxes:
            print(f"!! 車両リストなし(即時停止): {station_name}")
            sys.exit(1)

        start_time_str = "00:00"
        table_time = soup.find("table", class_="timetable")
        if table_time:
            rows_time = table_time.find_all("tr")
            if len(rows_time) >= 2:
                cell = rows_time[1].find("td", class_="timeline")
                if cell:
                    h = cell.get_text(strip=True)
                    start_time_str = f"{h}:00" if h.isdigit() else h

        for box in car_boxes:
            raw_text = box.find("div", class_="car-list-title-area").get_text(strip=True)
            parts = raw_text.split(" / ") if " / " in raw_text else [raw_text, ""]
            plate, model = parts[0].strip(), parts[1].strip()

            table = box.find("table", class_="timetable")
            rows = table.find_all("tr")
            if len(rows) < 3:
                print(f"!! ステータス行なし: {plate}")
                sys.exit(1)

            status_list = []
            data_cells = rows[2].find_all("td")
            for cell in data_cells:
                classes = cell.get("class", [])
                symbol = "s" if "impossible" in classes else ("○" if "vacant" in classes else "×")
                for _ in range(int(cell.get("colspan", 1))):
                    status_list.append(symbol)
            
            if len(status_list) < 288:
                status_list += ["×"] * (288 - len(status_list))

            collected_data.append([area, station_name, plate, model, start_time_str, "".join(status_list)])
        
        print(f"[{i+1}/{len(target_stations)}] {station_name} OK")

    # ==========================================================
    # III. 本番シートへの書き込み
    # ==========================================================
    if collected_data:
        print("\n[III.データ保存] シートへ書き込みます...")
        df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])

        for area in df_output['city'].unique():
            df_area = df_output[df_output['city'] == area].copy()
            work_sheet_name = f"{str(area).replace('市', '').strip()}_更新用"
            
            df_to_write = df_area.drop(columns=['city']) 
            try: ws_work = sh_prod.worksheet(work_sheet_name)
            except gspread.WorksheetNotFound: ws_work = sh_prod.add_worksheet(title=work_sheet_name, rows=len(df_area)+10, cols=10)
            
            ws_work.clear()
            ws_work.update([df_to_write.columns.values.tolist()] + df_to_write.values.tolist(), range_name='A1')
            print(f"   -> '{work_sheet_name}' 更新完了")
    else:
        print("!! データなし")
        sys.exit(1)

except Exception as e:
    print(f"\n!! 重大なエラー発生: {e}")
    sys.exit(1)

finally:
    if 'driver' in locals():
        driver.quit()
    
    # 終了処理 (SystemStatusのリセット)
    try:
        sh_prod_fin = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
        try: ws_status_fin = sh_prod_fin.worksheet("SystemStatus")
        except: ws_status_fin = sh_prod_fin.add_worksheet(title="SystemStatus", rows=5, cols=5)
        ws_status_fin.clear()
    except: pass
