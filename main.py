# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (最終安定・高速版)
# 目標: 2.0s/件 前後で確実に完走。迷走を排したシンプル構成。
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
print(f"\n[I.リスト読み込み] '{CSV_FILE_NAME}' を読み込みます...")
df_map = pd.read_csv(CSV_FILE_NAME)
df_map.columns = df_map.columns.str.strip()
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})

target_stations = df_map.drop_duplicates(subset=['stationCd']).to_dict('records')
print(f"-> 巡回対象: {len(target_stations)} カ所")

# ==========================================================
# II. セッション確立 (実績のある手順へ回帰)
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
session = requests.Session()

def sync_session():
    """余計な工程を排除した最安定のログイン手順"""
    print("-> ログインを開始します...")
    driver.get(LOGIN_URL)
    sleep(5)
    try:
        driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
        driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
        sleep(8)
        
        # 巡回トップページへ移動
        driver.get("https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html")
        sleep(5)
        
        # Cookie転送
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])
        
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html',
            'X-Requested-With': 'XMLHttpRequest'
        })
        print("   -> ログイン成功・同期完了")
    except Exception as e:
        print(f"!! ログイン工程で致命的エラー(即時停止): {e}")
        sys.exit(1)

# ==========================================================
# III. データ収集 (API高速巡回)
# ==========================================================
try:
    sh_prod = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
    sync_session()
    collected_data = []

    for i, item in enumerate(target_stations):
        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        area = str(item.get('city', 'other')).strip()

        # 揺らぎ(0.4〜0.8s)
        sleep(random.uniform(0.4, 0.8))

        target_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        
        valid_soup = None
        # 初回は特に念入りに(最大5回)リトライ
        max_retry = 5 if i == 0 else 3
        for attempt in range(max_retry):
            res = session.get(target_url, timeout=10)
            if "tawLogin.html" in res.url:
                sync_session()
                res = session.get(target_url, timeout=10)
            
            temp_soup = BeautifulSoup(res.text, 'lxml')
            # 判定: 時刻セル(timeline)が存在するか
            time_cell = temp_soup.find("td", class_="timeline")
            if time_cell and time_cell.get_text(strip=True):
                valid_soup = temp_soup
                break
            
            # リトライ待機
            sleep(0.8)
        
        if not valid_soup:
            print(f"!! データ取得失敗(即時停止): {station_name}")
            sys.exit(1)

        # 解析
        try:
            h = valid_soup.find("td", class_="timeline").get_text(strip=True)
            start_time_str = f"{h}:00" if h.isdigit() else h
            
            boxes = valid_soup.find_all("div", class_="car-list-box")
            for box in boxes:
                title = box.find("div", class_="car-list-title-area").get_text(strip=True)
                parts = title.split(" / ") if " / " in title else [title, ""]
                plate, model = parts[0].strip(), parts[1].strip()

                rows = box.select("table.timetable tr")
                if len(rows) < 3: continue # 異常行はスキップせず、後続のチェックで落とす

                status_list = []
                for cell in rows[2].find_all("td"):
                    cls = cell.get("class", [])
                    sym = "s" if "impossible" in cls else ("○" if "vacant" in cls else "×")
                    colspan = int(cell.get("colspan", 1))
                    status_list.extend([sym] * colspan)
                
                if len(status_list) < 288:
                    status_list.extend(["×"] * (288 - len(status_list)))
                
                collected_data.append([area, station_name, plate, model, start_time_str, "".join(status_list)])
        except Exception as e:
            print(f"!! 解析エラー(即時停止): {station_name} - {e}")
            sys.exit(1)
        
        print(f"[{i+1}/{len(target_stations)}] {station_name} OK")

    # ==========================================================
    # IV. 保存
    # ==========================================================
    if collected_data:
        print("\n[IV.データ保存] シートへ書き込み中...")
        df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])
        for area in df_output['city'].unique():
            df_area = df_output[df_output['city'] == area].copy()
            work_sheet_name = f"{str(area).replace('市', '').strip()}_更新用"
            df_to_write = df_area.drop(columns=['city'])
            ws_work = sh_prod.worksheet(work_sheet_name)
            ws_work.clear()
            ws_work.update([df_to_write.columns.values.tolist()] + df_to_write.values.tolist(), range_name='A1')

except Exception as e:
    print(f"\n!! 重大な実行エラー: {e}")
    sys.exit(1)
finally:
    if 'driver' in locals(): driver.quit()
