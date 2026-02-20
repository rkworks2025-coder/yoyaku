# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (高速化＆Discord通知＆動的除外版)
# 改修内容: 再ログイン削除 + sleep最適化 + エリアフィルタリング対応 + inspectionlog動的除外 + Discord通知
# ==========================================================
import sys
import os
import pandas as pd
import gspread
import unicodedata
import urllib.request
import json
from time import sleep
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from bs4 import BeautifulSoup

# --- Discord通知用設定 ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1474006170057441300/Emo5Ooe48jBUzMhzLrCBn85_3Td-ck3jYtXtVa2vdXWWyT2HxSuKghWchrG7gCsZhEqY"

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL: return
    data = {"content": message}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(data).encode(), headers=headers)
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Discord通知エラー: {e}")

# 1. ログイン情報設定
LOGIN_URL = "https://dailycheck.tc-extsys.jp/tcrappsweb/web/login/tawLogin.html"
USER_ID_1 = "0030"
USER_ID_2 = "927583"
PASSWORD = "Ccj-222223"

# 2. 設定
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/13cQngK_Xx38VU67yLS-iTHyOZgsACZdxM34l-Jq_U9A/edit"
CSV_FILE_NAME = "station_code_map.csv"
INSPECTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/11XglLANtnG7bCxYjLRMGoZY25wspjHsGR3IG2ZyRITs/edit"

# 3. Google認証
SERVICE_ACCOUNT_KEY_FILE = "service_account.json"

if not os.path.exists(SERVICE_ACCOUNT_KEY_FILE):
    print("!! エラー: 認証キーファイルが見つかりません。Secretsの設定を確認してください。")
    sys.exit(1)

gc = gspread.service_account(filename=SERVICE_ACCOUNT_KEY_FILE)

# ==========================================================
# ★新機能: エリアフィルタリング
# ==========================================================
TARGET_AREA = os.environ.get('TARGET_AREA', 'all').lower()
print(f"\n[エリア指定] {TARGET_AREA}")

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

# ★除外リストに '7days_rule' を追加
filter_mask = df_map['status'].astype(str).str.lower().isin(['checked', 'unnecessary', '7days_rule'])
df_active = df_map[~filter_mask].copy()

# ★エリアフィルタリング処理
if TARGET_AREA == 'kanagawa':
    df_active = df_active[df_active['city'].str.contains('大和|海老名', na=False)].copy()
    print(f"-> エリアフィルタ: 神奈川（大和+海老名）")
elif TARGET_AREA == 'tama':
    df_active = df_active[df_active['city'].str.contains('多摩', na=False)].copy()
    print(f"-> エリアフィルタ: 多摩")
else:
    print(f"-> エリアフィルタ: 全エリア")

target_stations_raw = df_active.drop_duplicates(subset=['stationCd']).to_dict('records')

# ==========================================================
# ★新機能: inspectionlogを用いた動的フィルタリング
# ==========================================================
print(f"\n[動的フィルタリング] 'inspectionlog' の最新ステータスを取得・突合します...")

try:
    inspection_sh_key = INSPECTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
    sh_inspection = gc.open_by_key(inspection_sh_key)
    ws_inspection = sh_inspection.worksheet("inspectionlog")
    inspection_values = ws_inspection.get_all_values()
except Exception as e:
    print(f"!! エラー: inspectionlogの取得に失敗しました。URLやシート名、権限を確認してください。")
    raise e  # エラーを隠蔽せず異常終了させる

def normalize_station_name(name):
    if pd.isna(name) or name is None:
        return ""
    name = str(name)
    name = unicodedata.normalize('NFKC', name)
    name = name.replace(' ', '').replace('　', '').lower()
    return name

inspection_status_map = {}
if len(inspection_values) > 1:
    for row in inspection_values[1:]: # ヘッダー行をスキップ
        if len(row) > 5: # B列(1)とF列(5)が存在することを確認
            raw_station = row[1] # B列: station
            raw_status = row[5]  # F列: status
            norm_station = normalize_station_name(raw_station)
            if norm_station:
                if norm_station not in inspection_status_map:
                    inspection_status_map[norm_station] = []
                norm_status = str(raw_status).strip().lower()
                inspection_status_map[norm_station].append(norm_status)

final_target_stations = []
skip_statuses = ['checked', 'unnecessary', '7days_rule']

for item in target_stations_raw:
    raw_station = item.get('station', '')
    norm_station = normalize_station_name(raw_station)
    
    if not norm_station:
        continue
        
    if norm_station not in inspection_status_map:
        # 表記揺れを吸収しても一致しない、または登録漏れの場合は厳格にエラーで止める
        raise ValueError(f"エラー: CSVのステーション '{raw_station}' が inspectionlog に存在しません。表記揺れか登録漏れの可能性があります。")
        
    statuses = inspection_status_map[norm_station]
    # ステーション内の「すべて」の車両がskip_statusesに含まれているか判定
    all_skipped = all((s in skip_statuses) for s in statuses)
    
    if all_skipped:
        print(f"   -> 全車両巡回済(または不要)のためスキップ: {raw_station}")
    else:
        final_target_stations.append(item)

target_stations = final_target_stations
print(f"-> 最終巡回対象: {len(target_stations)} カ所")

# 対象ステーションが0件の場合の処理（Discord通知付き）
if len(target_stations) == 0:
    print("-> 対象ステーションが0件のため終了します。")
    warn_msg = f"⚠️ 【データなし】 {TARGET_AREA.upper()} エリアの更新対象データがありませんでした。"
    send_discord_notification(warn_msg)
    sys.exit()

# ==========================================================
# ドライバ設定 & 変数初期化
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--window-size=1920,1080')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
collected_data = []

try:
    # ==========================================================
    # II. データ収集
    # ==========================================================
    print("\n[II.データ収集] 巡回を開始します...")

    # シート準備（進捗書き込み用）
    prod_sh_key = PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
    sh_prod = gc.open_by_key(prod_sh_key)

    # ★改修1: 初回ログイン（1回のみ）
    print("-> ログイン処理...")
    driver.get(LOGIN_URL)
    sleep(3)  # 変更なし（安定性重視）
    try:
        driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
        driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
        sleep(5)  # 7秒→5秒に短縮
        driver.get("https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html")
        sleep(2)  # 3秒→2秒に短縮
    except Exception as e:
        print(f"!! ログイン失敗: {e}")
        sys.exit(1)

    for i, item in enumerate(target_stations):
        # --- 進捗保存機能 (20件ごと) ---
        if (i > 0) and (i % 20 == 0):
            try:
                try: ws_status = sh_prod.worksheet("SystemStatus")
                except: ws_status = sh_prod.add_worksheet(title="SystemStatus", rows=5, cols=5)
                ws_status.update([["progress", i, len(target_stations)]], "A1")
                print(f"--- 進捗保存: {i}/{len(target_stations)} ---")
            except Exception as e:
                print(f"進捗保存エラー(無視します): {e}")
        # -----------------------------

        raw_city = str(item.get('city', 'other')).strip()
        area = raw_city if raw_city and raw_city != 'nan' else 'other'
        station_name = item.get('station', '不明なステーション')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')

        print(f"[{i+1}/{len(target_stations)}] {station_name} ({area})...")
        driver.get(f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}")
        sleep(2)  # ★改修2: 4秒→2秒に短縮

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
                        
                        if "impossible" in classes: symbol = "s"
                        elif "vacant" in classes: symbol = "○"
                        else: symbol = "×"
                        
                        try:
                            colspan = int(cell.get("colspan", 1))
                        except:
                            colspan = 1
                        
                        for _ in range(colspan):
                            status_list.append(symbol)
                            
                if len(status_list) < 288:
                    status_list += ["×"] * (288 - len(status_list))

                collected_data.append([area, station_name, plate, model, start_time_str, "".join(status_list)])
            except Exception as e:
                print(f"警告: 解析エラー {raw_car_text}: {e}")
        # ★改修3: sleep(2)を削除

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
            
            df_to_write = df_area.drop(columns=['city']) 
            
            try: ws_work = sh_prod.worksheet(work_sheet_name)
            except gspread.WorksheetNotFound: ws_work = sh_prod.add_worksheet(title=work_sheet_name, rows=len(df_area)+10, cols=10)
            
            data_to_upload = [df_to_write.columns.values.tolist()] + df_to_write.values.tolist()
            
            ws_work.clear()
            ws_work.update(data_to_upload, range_name='A1')
            
            print(f"   -> '{work_sheet_name}' シート更新完了")
            
        # 正常完了時のDiscord通知
        success_msg = f"✅ 【更新完了】 {TARGET_AREA.upper()} エリアの車両データ更新が完了しました！"
        print(f"\n{success_msg}")
        send_discord_notification(success_msg)

except Exception as e:
    # エラーで途中停止した時のDiscord通知
    error_msg = f"❌ 【重大なエラー】 {TARGET_AREA.upper()} エリアのスクレイピング中にエラーが発生しました:\n```{e}```"
    print(f"\n{error_msg}")
    send_discord_notification(error_msg)

finally:
    # ==========================================================
    # IV. 終了処理 (ドライバ停止 & ステータス強制クリア)
    # ==========================================================
    if 'driver' in locals():
        driver.quit()
    
    print("\n[終了処理] スプレッドシートのステータスをリセットします...")
    try:
        prod_sh_key_fin = PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
        sh_prod_fin = gc.open_by_key(prod_sh_key_fin)
        
        try: ws_status_fin = sh_prod_fin.worksheet("SystemStatus")
        except: ws_status_fin = sh_prod_fin.add_worksheet(title="SystemStatus", rows=5, cols=5)
        
        ws_status_fin.clear()
        print("-> ステータスシートのクリア完了")
        
    except Exception as e:
        print(f"!! 警告: ステータスのリセットに失敗しました: {e}")
