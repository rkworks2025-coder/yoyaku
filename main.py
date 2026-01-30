# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (最終安定・高速化版)
# 戦略: Selenium正規遷移 + 出現即抽出 (目標: 2.5s ~ 3.0s /件)
# ==========================================================
import sys
import os
import pandas as pd
import gspread
import random
from time import sleep, time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from bs4 import BeautifulSoup

# 1. ログイン情報設定
LOGIN_URL = "https://dailycheck.tc-extsys.jp/tcrappsweb/web/login/tawLogin.html"
USER_ID_1 = "0030"
USER_ID_2 = "927583"
PASSWORD = "Ccj-222223"

# 2. シート・リスト設定
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/13cQngK_Xx38VU67yLS-iTHyOZgsACZdxM34l-Jq_U9A/edit"
CSV_FILE_NAME = "station_code_map.csv"

# 3. Google認証
SERVICE_ACCOUNT_KEY_FILE = "service_account.json"
if not os.path.exists(SERVICE_ACCOUNT_KEY_FILE):
    print("!! 認証キーが見つかりません。")
    sys.exit(1)
gc = gspread.service_account(filename=SERVICE_ACCOUNT_KEY_FILE)

# ==========================================================
# I. リスト準備
# ==========================================================
df_map = pd.read_csv(CSV_FILE_NAME)
df_map.columns = df_map.columns.str.strip()
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})
target_stations = df_map.drop_duplicates(subset=['stationCd']).to_dict('records')
print(f"-> 巡回対象: {len(target_stations)} カ所")

# ==========================================================
# II. ブラウザ起動・ログイン
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
options.page_load_strategy = 'eager' # DOMが構築されたら解析へ(画像等を待たない)

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
wait = WebDriverWait(driver, 10)

def login():
    print("-> ログインを開始します...")
    driver.get(LOGIN_URL)
    sleep(3)
    try:
        driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
        driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
        sleep(5) # 安定化のための最小待機
    except Exception as e:
        print(f"!! ログイン失敗: {e}")
        sys.exit(1)

# ==========================================================
# III. 巡回実行 (出現即抽出)
# ==========================================================
collected_data = []
try:
    sh_prod = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
    login()

    for i, item in enumerate(target_stations):
        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        city = str(item.get('city', 'other')).strip()

        detail_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        driver.get(detail_url)
        
        # ★コアロジック: 時刻データが描き込まれた瞬間を捕捉
        try:
            wait.until(lambda d: d.find_element(By.CLASS_NAME, "timeline").text.strip() != "")
        except:
            print(f"!! データ欠落(即時停止): {station_name}")
            sys.exit(1)

        # 高速解析 (BeautifulSoupへ丸投げ)
        soup = BeautifulSoup(driver.page_source, 'lxml')
        h = soup.find("td", class_="timeline").get_text(strip=True)
        start_time_str = f"{h}:00" if h.isdigit() else h
        
        car_boxes = soup.find_all("div", class_="car-list-box")
        if not car_boxes:
            print(f"!! 車両枠なし(即時停止): {station_name}")
            sys.exit(1)

        for box in car_boxes:
            title = box.find("div", class_="car-list-title-area").get_text(strip=True)
            parts = title.split(" / ") if " / " in title else [title, ""]
            rows = box.select("table.timetable tr")
            if len(rows) < 3:
                print(f"!! 予約行不足(即時停止): {station_name}")
                sys.exit(1)
            
            status_list = []
            for cell in rows[2].find_all("td"):
                cls = cell.get("class", [])
                sym = "s" if "impossible" in cls else ("○" if "vacant" in cls else "×")
                status_list.extend([sym] * int(cell.get("colspan", 1)))
            
            status_list.extend(["×"] * (288 - len(status_list)))
            collected_data.append([city, station_name, parts[0].strip(), parts[1].strip(), start_time_str, "".join(status_list)])
        
        print(f"[{i+1}/{len(target_stations)}] {station_name} OK")

    # ==========================================================
    # IV. 保存
    # ==========================================================
    if collected_data:
        print("\n[IV.データ保存] シート書き込み中...")
        df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])
        for city_name in df_output['city'].unique():
            df_area = df_output[df_output['city'] == city_name].copy()
            ws_name = f"{str(city_name).replace('市', '').strip()}_更新用"
            df_to_write = df_area.drop(columns=['city'])
            try:
                ws = sh_prod.worksheet(ws_name)
                ws.clear()
                ws.update([df_to_write.columns.values.tolist()] + df_to_write.values.tolist(), range_name='A1')
            except Exception as e:
                print(f"   [WARN] シート {ws_name} の書き込みに失敗: {e}")

except Exception as e:
    print(f"\n!! 致命的エラー: {e}")
    sys.exit(1)
finally:
    if 'driver' in locals():
        driver.quit()
