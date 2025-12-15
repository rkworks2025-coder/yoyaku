# ==========================================================
# 【GitHub Actions用】3エリア巡回システム
# 機能: 3色判定 + 進捗書き込み機能付き
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
    print("!! エラー: 認証キーファイルが見つかりません。Secretsの設定を確認してください。")
    sys.exit(1)

gc = gspread.service_account(filename=SERVICE_ACCOUNT_KEY_FILE)

# ==========================================================
# I. リスト読み込み
# ==========================================================
print(f"\n[I.リスト読み込み] '{CSV_FILE_NAME}' を読み込みます...")
if not os.path.exists(CSV_FILE_NAME):
    raise FileNotFoundError(f"エラー: '{CSV_FILE_NAME}' が見つかりません。")

df_map = pd.read_csv(CSV_FILE_NAME)
df_map.columns = df_map.columns.str.strip()

if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})

if 'status' not in df_map.columns: df_map['status'] = ""
filter_mask = df_map['status'].astype(str).str.lower().isin(['checked', 'unnecessary'])
df_active = df_map[~filter_mask].copy()

target_stations = df_active.drop_duplicates(subset=['stationCd']).to_dict('records')
print(f"-> 巡回対象: {len(target_stations)} カ所")
if len(target_stations) == 0: sys.exit()

# ==========================================================
# II. データ収集
# ==========================================================
print("\n[II.データ収集] 巡回を開始します...")

# シート準備（進捗書き込み用）
prod_sh_key = PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
sh_prod = gc.open_by_key(prod_sh_key)

options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--window-size=1920,1080')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
collected_data = []

try:
    for i, item in enumerate(target_stations):
        # --- 進捗保存機能 (20件ごと) ---
        if (i > 0) and (i % 20 == 0):
            try:
                try: ws_status = sh_prod.worksheet("SystemStatus")
                except: ws_status = sh_prod.add_worksheet(title="SystemStatus", rows=5, cols=5)
                # B1セルに現在の完了数、C1セルに全件数を書き込み
                ws_status.update([["progress", i, len(target_stations)]], "A1")
                print(f"--- 進捗保存: {i}/{len(target_stations)} ---")
            except Exception as e:
                print(f"進捗保存エラー(無視します): {e}")
        # -----------------------------

        raw_city = str(item.get('city', 'other')).strip()
        area = raw_city if raw_city and raw_city != 'nan' else 'other'
        station_name = item.get('station', '不明なステーション')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')

        # ログイン処理
        if i == 0 or (i % 20 == 0):
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
            except Exception as e:
                print(f"ログイン続行: {e}")

        print(f"[{i+1}/{len(target_stations)}] {station_name} ({area})...")
        driver.get(f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}")
        sleep(4)

        soup = BeautifulSoup(driver.page_source, "lxml")
        car_boxes = soup.find_all("div", class_="car-list-box")

        start_time_str = "00:00"
        try:
            table = soup.find("table", class_="timetable")
            rows = table.find_all("tr")
            if len(rows) >= 2:
                hour_row = rows[1]
                first_hour_cell = hour_row.find("td", class_="timeline")
                if first_hour_cell:
                    raw_hour = first_hour_cell.get_text(strip=True)
                    if raw_hour.isdigit(): start_time_str = f"{raw_hour}:00"
                    else: start_time_str = raw_hour
        except: pass

        for box in car_boxes:
            try:
                raw_car_text = box.find("div", class_="car-list-title-area").get_text(strip=True)
                if " / " in raw_car_text:
                    parts = raw_car_text.split(" / ")
                    plate = parts[0].strip()
                    model = parts[1].strip() if len(parts) > 1 else ""
                else:
                    plate = raw_car_text
                    model = ""

                table = box.find("table", class_="timetable")
                rows = table.find_all("tr")
                status_list = []
                
                if len(rows) >= 3:
                    data_cells = rows[2].find_all("td")
                    for cell in data_cells:
                        classes = cell.get("class", [])
                        if "impossible" in classes: status_list.append("s")
                        elif "vacant" in classes: status_list.append("○")
                        else: status_list.append("×")
                            
                if len(status_list) < 288:
                    status_list += ["×"] * (288 - len(status_list))

                collected_data.append([area, station_name, plate, model, start_time_str, "".join(status_list)])
            except Exception as e:
                print(f"警告: 解析エラー {raw_car_text}: {e}")
        sleep(2)

except Exception as e:
    print(f"\n!! エラー発生: {e}")

finally:
    driver.quit()

# ==========================================================
# III. 本番シートへの書き込み
# ==========================================================
if collected_data:
    print("\n[III.データ保存] シートへ書き込みます...")
    columns = ['city', 'station', 'plate', 'model', 'getTime', 'rsvData']
    df_output = pd.DataFrame(collected_data, columns=columns)

    unique_areas = df_output['city'].unique()
    for area in unique_areas:
        df_area = df_output[df_output['city'] == area].copy()
        if df_area.empty: continue
        
        area_name = str(area).replace('市', '').strip()
        work_sheet_name = f"{area_name}_更新用"
