# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (プランA: 高速特化版)
# 目標: 1ステーションあたり 2.0s ~ 2.5s (全エリア 4~5分完了)
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
# I. リスト読み込み
# ==========================================================
print(f"\n[I.リスト読み込み] '{CSV_FILE_NAME}' を読み込みます...")
df_map = pd.read_csv(CSV_FILE_NAME)
df_map.columns = df_map.columns.str.strip()
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})

filter_mask = df_map['status'].astype(str).str.lower().isin(['checked', 'unnecessary', '7days_rule'])
df_active = df_map[~filter_mask].copy()
target_stations = df_active.drop_duplicates(subset=['stationCd']).to_dict('records')
print(f"-> 巡回対象: {len(target_stations)} カ所")
if len(target_stations) == 0: sys.exit()

# ==========================================================
# ドライバ設定 (高速化オプション)
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--window-size=1920,1080')
# ★高速化: 画像・CSS・フォントの読み込みを拒否
options.add_experimental_option("prefs", {
    "profile.managed_default_content_settings.images": 2,
    "profile.managed_default_content_settings.stylesheets": 2,
    "profile.managed_default_content_settings.fonts": 2
})
# ★高速化: ページ全体ではなくDOM構築完了(interactive)で次へ
options.page_load_strategy = 'eager'

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
wait = WebDriverWait(driver, 10) # タイムアウトは余裕を持って設定
collected_data = []

def perform_login():
    """元のログイン手順(main20260110.py)を完全に守り、確実にセッションを確立する"""
    print("-> ログインを開始します...")
    driver.get(LOGIN_URL)
    sleep(3) # 初回読み込みのみ待機
    try:
        driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
        driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
        sleep(5) # ログイン完了待ち
        driver.get("https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html")
        sleep(2)
        print("   -> ログイン成功")
    except Exception as e:
        print(f"!! ログイン失敗(即時停止): {e}")
        sys.exit(1)

# ==========================================================
# II. データ収集
# ==========================================================
try:
    sh_prod = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
    perform_login() # 最初の一回

    for i, item in enumerate(target_stations):
        # 進捗保存 (20件ごと)
        if (i > 0) and (i % 20 == 0):
            try:
                ws_status = sh_prod.worksheet("SystemStatus")
                ws_status.update([["progress", i, len(target_stations)]], "A1")
                print(f"--- 進捗: {i}/{len(target_stations)} ---")
            except: pass

        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        area = str(item.get('city', 'other')).strip()

        # ターゲットURLへ直行
        target_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        driver.get(target_url)

        # ★プランA: ログイン画面に飛ばされたか「0.5秒以内」に判定
        try:
            # 短い待機でチェック
            login_box = driver.find_elements(By.ID, "cardNo1")
            if login_box:
                print("   [!] セッション切れを検知。再ログイン。")
                perform_login()
                driver.get(target_url)
            
            # 車両情報が出るまで待機 (eagerモードと相性良)
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "car-list-box")))
        except Exception as e:
            print(f"!! ページ読み込みエラー(即時停止): {station_name}")
            sys.exit(1)

        # 解析
        soup = BeautifulSoup(driver.page_source, "lxml")
        car_boxes = soup.find_all("div", class_="car-list-box")
        
        if not car_boxes:
            print(f"!! データなし(即時停止): {station_name}")
            sys.exit(1)

        # 時刻取得 (無駄なsleepなし)
        start_time_str = "00:00"
        table_time = soup.find("table", class_="timetable")
        if table_time:
            row_time = table_time.find("tr", class_="") # 2行目を想定
            if row_time:
                cell = row_time.find("td", class_="timeline")
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
            for cell in rows[2].find_all("td"):
                cls = cell.get("class", [])
                sym = "s" if "impossible" in cls else ("○" if "vacant" in cls else "×")
                for _ in range(int(cell.get("colspan", 1))):
                    status_list.append(sym)
            
            if len(status_list) < 288:
                status_list += ["×"] * (288 - len(status_list))

            collected_data.append([area, station_name, plate, model, start_time_str, "".join(status_list)])
        
        print(f"[{i+1}/{len(target_stations)}] {station_name} OK")

    # ==========================================================
    # III. 書き込み (ここは以前のまま)
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
    else:
        print("!! データなし")
        sys.exit(1)

except Exception as e:
    print(f"\n!! エラー発生: {e}")
    sys.exit(1)

finally:
    if 'driver' in locals():
        driver.quit()
    # システムステータスのリセット
    try:
        sh_prod_fin = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
        ws_status_fin = sh_prod_fin.worksheet("SystemStatus")
        ws_status_fin.clear()
    except: pass
