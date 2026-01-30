# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (プランA: 安全・厳格版)
# 機能: 必要時のみログイン + ログイン処理の完全復元 + エラー即停止
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
    print("!! エラー: 認証キーファイルが見つかりません。")
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

filter_mask = df_map['status'].astype(str).str.lower().isin(['checked', 'unnecessary', '7days_rule'])
df_active = df_map[~filter_mask].copy()

target_stations = df_active.drop_duplicates(subset=['stationCd']).to_dict('records')
print(f"-> 巡回対象: {len(target_stations)} カ所")
if len(target_stations) == 0: sys.exit()

# ==========================================================
# ドライバ設定
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--window-size=1920,1080')
options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
collected_data = []

def perform_login():
    """以前のコード(main20260110.py)から完全に復元したログイン手順"""
    print("-> ログインを開始します...")
    driver.get(LOGIN_URL)
    sleep(5)
    try:
        driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
        driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
        sleep(7)
        # ログイン後にこのURLへ飛ぶ手順も確実に再現
        driver.get("https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html")
        sleep(3)
        print("   -> ログイン成功")
    except Exception as e:
        print(f"!! ログインに失敗しました。スクリプトを停止します: {e}")
        sys.exit(1) # ログイン失敗時は即座に終了

# ==========================================================
# II. データ収集
# ==========================================================
try:
    print("\n[II.データ収集] 巡回を開始します...")
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
                print(f"--- 進捗保存: {i}/{len(target_stations)} ---")
            except Exception as e:
                print(f"進捗保存エラー(無視): {e}")

        station_name = item.get('station', '不明なステーション')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        area = str(item.get('city', 'other')).strip()

        print(f"[{i+1}/{len(target_stations)}] {station_name}...")
        
        target_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        driver.get(target_url)
        sleep(4)

        # ログイン画面に飛ばされたか判定 (cardNo1があるか)
        login_check = driver.find_elements(By.ID, "cardNo1")
        if len(login_check) > 0:
            print("   [!] セッション切れを検知しました。再ログインします。")
            perform_login()
            driver.get(target_url)
            sleep(4)

        soup = BeautifulSoup(driver.page_source, "lxml")
        car_boxes = soup.find_all("div", class_="car-list-box")

        # ページ構造が不正(データが取れない)場合は警告せず停止
        if not car_boxes:
            print(f"!! エラー: {station_name} の車両リスト(car-list-box)が見つかりません。")
            sys.exit(1)

        start_time_str = "00:00"
        table_time = soup.find("table", class_="timetable")
        if table_time:
            rows_time = table_time.find_all("tr")
            if len(rows_time) >= 2:
                first_hour_cell = rows_time[1].find("td", class_="timeline")
                if first_hour_cell:
                    raw_hour = first_hour_cell.get_text(strip=True)
                    start_time_str = f"{raw_hour}:00" if raw_hour.isdigit() else raw_hour

        for box in car_boxes:
            # 各車両データの解析 (エラー時は即停止)
            raw_car_text = box.find("div", class_="car-list-title-area").get_text(strip=True)
            if " / " in raw_car_text:
                parts = raw_car_text.split(" / ")
                plate, model = parts[0].strip(), parts[1].strip()
            else:
                plate, model = raw_car_text, ""

            table = box.find("table", class_="timetable")
            rows = table.find_all("tr")
            status_list = []
            
            if len(rows) >= 3:
                data_cells = rows[2].find_all("td")
                for cell in data_cells:
                    classes = cell.get("class", [])
                    symbol = "s" if "impossible" in classes else ("○" if "vacant" in classes else "×")
                    colspan = int(cell.get("colspan", 1))
                    for _ in range(colspan):
                        status_list.append(symbol)
            else:
                print(f"!! エラー: {plate} のステータス行が見つかりません。")
                sys.exit(1)
                        
            if len(status_list) < 288:
                status_list += ["×"] * (288 - len(status_list))

            collected_data.append([area, station_name, plate, model, start_time_str, "".join(status_list)])
        sleep(2)

    # ==========================================================
    # III. 本番シートへの書き込み
    # ==========================================================
    if collected_data:
        print("\n[III.データ保存] シートへ書き込みます...")
        df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])

        for area in df_output['city'].unique():
            df_area = df_output[df_output['city'] == area].copy()
            area_name = str(area).replace('市', '').strip()
            work_sheet_name = f"{area_name}_更新用"
            
            df_to_write = df_area.drop(columns=['city']) 
            try: ws_work = sh_prod.worksheet(work_sheet_name)
            except gspread.WorksheetNotFound: ws_work = sh_prod.add_worksheet(title=work_sheet_name, rows=len(df_area)+10, cols=10)
            
            ws_work.clear()
            ws_work.update([df_to_write.columns.values.tolist()] + df_to_write.values.tolist(), range_name='A1')
            print(f"   -> '{work_sheet_name}' 更新完了")
    else:
        print("!! データなし。終了します。")
        sys.exit(1)

except Exception as e:
    print(f"\n!! 重大なエラーが発生しました: {e}")
    sys.exit(1)

finally:
    if 'driver' in locals():
        driver.quit()
    
    # ステータスリセット (ここだけは安全に最後まで実行)
    try:
        sh_prod_fin = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
        try: ws_status_fin = sh_prod_fin.worksheet("SystemStatus")
        except: ws_status_fin = sh_prod_fin.add_worksheet(title="SystemStatus", rows=5, cols=5)
        ws_status_fin.clear()
        print("[終了処理] リセット完了")
    except: pass
