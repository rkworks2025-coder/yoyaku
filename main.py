# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (ハイブリッド最終形態)
# 目標: 1件目はブラウザで確実に。2件目以降はAPIで超高速(2s/件)。
# ==========================================================
import sys
import os
import pandas as pd
import gspread
import requests
import random
from time import sleep, time
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
# I. リスト読み込み
# ==========================================================
df_map = pd.read_csv(CSV_FILE_NAME)
df_map.columns = df_map.columns.str.strip()
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})
target_stations = df_map.drop_duplicates(subset=['stationCd']).to_dict('records')
print(f"-> 巡回対象: {len(target_stations)} カ所")

# ==========================================================
# II. 1件目の実演 & セッション同期
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
session = requests.Session()
collected_data = []

def run_hybrid_init(first_item):
    """1件目のみブラウザで抽出し、同時にAPIセッションを確立する"""
    print(f"-> 1件目({first_item['station']})をブラウザで実演巡回します...")
    driver.get(LOGIN_URL)
    sleep(5)
    try:
        driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
        driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
        sleep(8)
        
        # 1件目詳細URL
        station_cd = str(first_item.get('stationCd', '')).replace('.0', '')
        detail_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        driver.get(detail_url)
        
        # 時刻セル(timeline)が中身を持って描画されるのを待つ
        WebDriverWait(driver, 15).until(lambda d: d.find_element(By.CLASS_NAME, "timeline").text.strip() != "")
        sleep(2)
        
        # ★1件目のデータはブラウザのDOMから直接抜く(確実性100%)
        soup = BeautifulSoup(driver.page_source, 'lxml')
        h = soup.find("td", class_="timeline").get_text(strip=True)
        start_time_str = f"{h}:00" if h.isdigit() else h
        
        car_boxes = soup.find_all("div", class_="car-list-box")
        if not car_boxes: raise ValueError("1件目の車両枠が見つかりません")
        
        for box in car_boxes:
            title = box.find("div", class_="car-list-title-area").get_text(strip=True)
            parts = title.split(" / ") if " / " in title else [title, ""]
            rows = box.select("table.timetable tr")
            status_list = []
            for cell in rows[2].find_all("td"):
                cls = cell.get("class", [])
                sym = "s" if "impossible" in cls else ("○" if "vacant" in cls else "×")
                status_list.extend([sym] * int(cell.get("colspan", 1)))
            status_list.extend(["×"] * (288 - len(status_list)))
            collected_data.append([first_item['city'], first_item['station'], parts[0], parts[1], start_time_str, "".join(status_list)])
        
        # Cookie同期
        for cookie in driver.get_cookies(): session.cookies.set(cookie['name'], cookie['value'])
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': detail_url,
            'X-Requested-With': 'XMLHttpRequest'
        })
        print(f"   -> 1件目成功。セッションをAPIへ引き継ぎます。")
    except Exception as e:
        print(f"!! 1件目の実演で失敗(即時停止): {e}")
        sys.exit(1)

# ==========================================================
# III. 2件目以降のAPI高速巡回
# ==========================================================
try:
    sh_prod = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
    run_hybrid_init(target_stations[0])

    for i, item in enumerate(target_stations[1:], 1): # 2件目から開始
        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        area = str(item.get('city', 'other')).strip()

        sleep(random.uniform(0.4, 0.8)) # 揺らぎ
        target_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        
        valid_soup = None
        for attempt in range(3):
            res = session.get(target_url, timeout=10)
            if "tawLogin.html" in res.url:
                 run_hybrid_init(item) # 再ログイン
                 res = session.get(target_url, timeout=10)
            
            temp_soup = BeautifulSoup(res.text, 'lxml')
            time_cell = temp_soup.find("td", class_="timeline")
            if time_cell and time_cell.get_text(strip=True):
                valid_soup = temp_soup
                break
            sleep(0.8)
        
        if not valid_soup:
            print(f"!! API取得失敗(即時停止): {station_name}")
            sys.exit(1)

        # APIデータ解析
        try:
            h = valid_soup.find("td", class_="timeline").get_text(strip=True)
            start_time_str = f"{h}:00" if h.isdigit() else h
            for box in valid_soup.find_all("div", class_="car-list-box"):
                title = box.find("div", class_="car-list-title-area").get_text(strip=True)
                parts = title.split(" / ") if " / " in title else [title, ""]
                rows = box.select("table.timetable tr")
                status_list = []
                for cell in rows[2].find_all("td"):
                    cls = cell.get("class", [])
                    sym = "s" if "impossible" in cls else ("○" if "vacant" in cls else "×")
                    status_list.extend([sym] * int(cell.get("colspan", 1)))
                status_list.extend(["×"] * (288 - len(status_list)))
                collected_data.append([area, station_name, parts[0], parts[1], start_time_str, "".join(status_list)])
        except Exception as e:
            print(f"!! 解析異常(即時停止): {station_name} - {e}")
            sys.exit(1)
        
        print(f"[{i+1}/{len(target_stations)}] {station_name} OK")

    # ==========================================================
    # IV. 保存
    # ==========================================================
    if collected_data:
        print("\n[IV.データ保存] シートへ書き込み中...")
        df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])
        for city_name in df_output['city'].unique():
            df_area = df_output[df_output['city'] == city_name].copy()
            work_sheet_name = f"{str(city_name).replace('市', '').strip()}_更新用"
            df_to_write = df_area.drop(columns=['city'])
            ws_work = sh_prod.worksheet(work_sheet_name)
            ws_work.clear()
            ws_work.update([df_to_write.columns.values.tolist()] + df_to_write.values.tolist(), range_name='A1')

except Exception as e:
    print(f"\n!! 致命的エラー: {e}")
    sys.exit(1)
finally:
    if 'driver' in locals(): driver.quit()
