# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (API・完全安定高速版)
# 目標: 1.5s ~ 2.0s/件 (全エリア 4分前後完了)
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
# II. セッション確立
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
session = requests.Session()

def sync_session():
    """Cookieとヘッダーを完全に同期。クッションページを挟むことで安定化。"""
    print("-> ログインとセッション同期を開始します...")
    driver.get(LOGIN_URL)
    sleep(3)
    try:
        driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
        driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
        sleep(5)
        
        # ★安定化の鍵: 一覧ページを完全に読み込み、セッションを確立させる
        driver.get("https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html")
        sleep(4) 
        
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])
        
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'X-Requested-With': 'XMLHttpRequest'
        })
        
        # クッションリクエスト: API側にも一度トップページを叩かせておく
        session.get("https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html", timeout=10)
        
        print("   -> セッション同期成功")
    except Exception as e:
        print(f"!! セッション確立失敗: {e}")
        sys.exit(1)

# ==========================================================
# III. データ収集 (超高速バリデーション)
# ==========================================================
try:
    sh_prod = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
    sync_session()
    collected_data = []

    for i, item in enumerate(target_stations):
        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        area = str(item.get('city', 'other')).strip()

        # BOT対策の揺らぎ (0.4〜0.9秒)
        sleep(random.uniform(0.4, 0.9))

        target_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        
        valid_soup = None
        # 最大4回試行 (初回 + リトライ3回)
        for attempt in range(4):
            res = session.get(target_url, timeout=10)
            if "tawLogin.html" in res.url:
                sync_session()
                res = session.get(target_url, timeout=10)
            
            temp_soup = BeautifulSoup(res.text, 'lxml')
            
            # 厳格判定: 車両枠と時刻セルが「中身を持って」存在するか
            car_boxes = temp_soup.find_all("div", class_="car-list-box")
            time_cell = temp_soup.find("td", class_="timeline")
            
            if car_boxes and time_cell and time_cell.get_text(strip=True):
                valid_soup = temp_soup
                break
            
            if attempt < 3:
                # 待機時間を段階的に増やす(バックオフ)
                wait_time = 0.5 + (attempt * 0.3)
                print(f"   [!] {station_name}: データ未完成。{wait_time}秒後にリトライ({attempt+1}/3)...")
                sleep(wait_time)
        
        if not valid_soup:
            print(f"!! 深刻なエラー: リトライを尽くしてもデータが取得できません(即時停止): {station_name}")
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
                if len(rows) < 3: raise ValueError("予約データの行が不足しています")
                
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
            print(f"!! 解析工程でのエラー(即時停止): {station_name} - {e}")
            sys.exit(1)
        
        print(f"[{i+1}/{len(target_stations)}] {station_name} OK")

    # ==========================================================
    # IV. 本格書き込み
    # ==========================================================
    if collected_data:
        print("\n[IV.データ保存] シートへ書き込み中...")
        df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])
        for area in df_output['city'].unique():
            df_area = df_output[df_output['city'] == area].copy()
            work_sheet_name = f"{str(area).replace('市', '').strip()}_更新用"
            df_to_write = df_area.drop(columns=['city'])
            try: ws_work = sh_prod.worksheet(work_sheet_name)
            except gspread.WorksheetNotFound: ws_work = sh_prod.add_worksheet(title=work_sheet_name, rows=len(df_area)+10, cols=10)
            ws_work.clear()
            ws_work.update([df_to_write.columns.values.tolist()] + df_to_write.values.tolist(), range_name='A1')
            print(f"   -> {work_sheet_name} 更新完了")

except Exception as e:
    print(f"\n!! 重大な実行エラー: {e}")
    sys.exit(1)
finally:
    if 'driver' in locals(): driver.quit()
