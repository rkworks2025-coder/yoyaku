# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (プランA: 厳格・最速版)
# 目標: 2.0s/件 前後で「確実なデータ」のみを収集。異常時は即停止。
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
# II. データ収集 (超高速・厳格バリデーション)
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

        # 内容まで含めた高速検知ループ (最大7秒)
        found = False
        start_wait = time()
        while time() - start_wait < 7:
            if driver.find_elements(By.ID, "cardNo1"):
                print("   [!] セッション切れ検知。再ログイン。")
                perform_login()
                driver.get(target_url)
                start_wait = time()
                continue
            
            # 外枠の存在を確認
            car_boxes = driver.find_elements(By.CLASS_NAME, "car-list-box")
            if car_boxes:
                # ★厳格判定: 最初の車両のテーブル内にデータ(クラス名)が描画されているか
                try:
                    first_table = car_boxes[0].find_element(By.CLASS_NAME, "timetable")
                    # vacant, impossible, booked などのいずれかが存在すれば描画済みとみなす
                    cells = first_table.find_elements(By.TAG_NAME, "td")
                    if any(c.get_attribute("class") for c in cells):
                        found = True
                        break
                except: pass
            sleep(0.1)
        
        if not found:
            print(f"!! データ未描画または要素未検出(即時停止): {station_name}")
            sys.exit(1)

        # 時刻取得 (失敗時は即停止)
        try:
            timeline_cell = driver.find_element(By.CLASS_NAME, "timeline")
            h = timeline_cell.text.strip()
            if not h: raise ValueError("時刻が空です")
            start_time_str = f"{h}:00" if h.isdigit() else h
        except Exception as e:
            print(f"!! 時刻取得失敗(即時停止): {station_name} - {e}")
            sys.exit(1)

        # 車両データ解析
        for box in car_boxes:
            try:
                title_text = box.find_element(By.CLASS_NAME, "car-list-title-area").text.strip()
                parts = title_text.split(" / ") if " / " in title_text else [title_text, ""]
                plate, model = parts[0].strip(), parts[1].strip()

                # ステータス行(3行目)のtdを確実に取得
                # TMAの構造に合わせ、明示的にtr[3]または特定のクラスを狙う
                status_list = []
                # trの3番目、またはtimetable内のデータ行を特定
                rows = box.find_elements(By.XPATH, ".//table[@class='timetable']//tr")
                if len(rows) < 3: raise ValueError("予約テーブルの行不足")
                
                cells = rows[2].find_all_sub_elements if False else rows[2].find_elements(By.TAG_NAME, "td")
                
                for cell in cells:
                    cls = cell.get_attribute("class") or ""
                    sym = "s" if "impossible" in cls else ("○" if "vacant" in cls else "×")
                    colspan = int(cell.get_attribute("colspan") or 1)
                    status_list.extend([sym] * colspan)
                
                if len(status_list) < 288:
                    status_list.extend(["×"] * (288 - len(status_list)))
                
                # 全てが「×」かつ描画が怪しい場合は異常とみなす
                if all(s == "×" for s in status_list):
                     # 本当に全時間帯予約不可か、取得失敗かの再チェック(必要ならここで停止)
                     pass

                collected_data.append([area, station_name, plate, model, start_time_str, "".join(status_list)])
            except Exception as e:
                print(f"!! 車両データ解析失敗(即時停止): {station_name} - {e}")
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
            ws_work = sh_prod.worksheet(work_sheet_name)
            ws_work.clear()
            ws_work.update([df_to_write.columns.values.tolist()] + df_to_write.values.tolist(), range_name='A1')
    
except Exception as e:
    print(f"\n!! 重大なエラー発生: {e}")
    sys.exit(1)

finally:
    if 'driver' in locals():
        driver.quit()
    try:
        sh_prod_fin = gc.open_by_key(PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0])
        ws_status_fin = sh_prod_fin.worksheet("SystemStatus")
        ws_status_fin.clear()
    except: pass
