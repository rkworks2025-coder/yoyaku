# ==========================================================
# 【GitHub Actions用】多摩・府中エリア巡回システム (JKS本体同時書き込み版)
# 改修内容:
# 1. CarData_Ryu と JKS本体(16HYziQ...) への同時同期機能
# 2. 新設ステーション対応(inspectionlogにない場合は未登録として送信)
# 3. エリア抽象化(多摩・府中のみ対象)
# ==========================================================
import sys
import os
import pandas as pd
import gspread
import unicodedata
import urllib.request
import json
from time import sleep
from datetime import datetime, timezone, timedelta
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
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(data).encode(), headers=headers)
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Discord通知エラー: {e}")

# 1. ログイン情報設定
LOGIN_URL = "https://dailycheck.tc-extsys.jp/tcrappsweb/web/login/tawLogin.html"
USER_ID_1 = "0030"
USER_ID_2 = "927583"
PASSWORD = "Ccj-322222"

# 2. 設定
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/13cQngK_Xx38VU67yLS-iTHyOZgsACZdxM34l-Jq_U9A/edit"
# ★JKS本体スプレッドシートID
JKS_SHEET_ID = "16HYziQ5now1IATZJU3wZhTE08S_3B8xVP9MbfceHONE"

CSV_FILE_NAME = "station_code_map.csv"
INSPECTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/11XglLANtnG7bCxYjLRMGoZY25wspjHsGR3IG2ZyRITs/edit"

# 3. Google認証
SERVICE_ACCOUNT_KEY_FILE = "service_account.json"
if not os.path.exists(SERVICE_ACCOUNT_KEY_FILE):
    print("!! エラー: 認証キーファイルが見つかりません。")
    sys.exit(1)

gc = gspread.service_account(filename=SERVICE_ACCOUNT_KEY_FILE)

# エリアフィルタリング設定
TARGET_AREA = os.environ.get('TARGET_AREA', 'all').lower()
print(f"\n[エリア指定] {TARGET_AREA}")

# ==========================================================
# I. リスト読み込み
# ==========================================================
if not os.path.exists(CSV_FILE_NAME):
    raise FileNotFoundError(f"エラー: '{CSV_FILE_NAME}' が見つかりません。")

df_map = pd.read_csv(CSV_FILE_NAME, encoding='utf-8')
df_map.columns = df_map.columns.str.strip()
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})
if 'status' not in df_map.columns: df_map['status'] = ""

if TARGET_AREA == 'force_all':
    df_active = df_map.copy()
else:
    filter_mask = df_map['status'].astype(str).str.lower().isin(['checked', 'unnecessary', '7days_rule'])
    df_active = df_map[~filter_mask].copy()

    # マッピング方式によるエリアの抽象化
    area_map = {
        'tama': '多摩',
        'fuchu': '府中'
    }
    if TARGET_AREA in area_map:
        df_active = df_active[df_active['city'].str.contains(area_map[TARGET_AREA], na=False)].copy()

target_stations_raw = df_active.drop_duplicates(subset=['stationCd']).to_dict('records')

# inspectionlogを用いた動的フィルタリング
if TARGET_AREA == 'force_all':
    target_stations = target_stations_raw
else:
    try:
        inspection_sh_key = INSPECTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
        sh_inspection = gc.open_by_key(inspection_sh_key)
        ws_inspection = sh_inspection.worksheet("inspectionlog")
        inspection_values = ws_inspection.get_all_values()
    except Exception as e:
        print(f"!! エラー: inspectionlogの取得に失敗しました。")
        raise e

    def normalize_station_name(name):
        if pd.isna(name) or name is None: return ""
        return unicodedata.normalize('NFKC', str(name)).replace(' ', '').replace('　', '').lower()

    inspection_status_map = {}
    if len(inspection_values) > 1:
        for row in inspection_values[1:]:
            if len(row) > 5:
                norm_station = normalize_station_name(row[1])
                if norm_station:
                    if norm_station not in inspection_status_map: inspection_status_map[norm_station] = []
                    inspection_status_map[norm_station].append(str(row[5]).strip().lower())

    final_target_stations = []
    skip_statuses = ['checked', 'unnecessary', '7days_rule']
    for item in target_stations_raw:
        norm_station = normalize_station_name(item.get('station', ''))
        if not norm_station: continue

        # ログに存在しない場合はエラーにせず「未登録(新設)」としてGASへ送るためリストに追加
        if norm_station not in inspection_status_map:
            print(f"   -> [未登録(新設)検知] 巡回対象に追加: {item.get('station')}")
            final_target_stations.append(item)
            continue

        # ログに存在する場合は、ステータスによる絞り込みを実行
        if not all((s in skip_statuses) for s in inspection_status_map[norm_station]):
            final_target_stations.append(item)

    target_stations = final_target_stations

if len(target_stations) == 0:
    send_discord_notification(f"<@1474004343207366839> ⚠️ 【データなし】 {TARGET_AREA.upper()} 更新対象データがありません。")
    sys.exit()

# ==========================================================
# ドライバ設定
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
collected_data = []

try:
    # ==========================================================
    # II. データ収集
    # ==========================================================
    prod_sh_key = PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
    sh_prod = gc.open_by_key(prod_sh_key)
    # ★JKS本体もオープンしておく
    sh_jks = gc.open_by_key(JKS_SHEET_ID)

    driver.get(LOGIN_URL)
    sleep(3)
    driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
    driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
    driver.find_element(By.ID, "password").send_keys(PASSWORD)
    driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
    sleep(5)
    driver.get("https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html")
    sleep(2)

    for i, item in enumerate(target_stations):
        if (i > 0) and (i % 20 == 0):
            try:
                ws_status = sh_prod.worksheet("SystemStatus")
                ws_status.update([["progress", i, len(target_stations)]], "A1")
            except: pass

        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        city = str(item.get('city', 'other')).strip()

        print(f"[{i+1}/{len(target_stations)}] {station_name}...")
        driver.get(f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}")
        sleep(2)

        soup = BeautifulSoup(driver.page_source, "lxml")
        car_boxes = soup.find_all("div", class_="car-list-box")

        # タイムライン開始時刻取得
        start_time_str = "00:00"
        try:
            table = soup.find("table", class_="timetable")
            for r in table.find_all("tr"):
                cell = r.find("td", class_="timeline")
                if cell and cell.get_text(strip=True).isdigit():
                    raw_h = int(cell.get_text(strip=True))
                    now = datetime.now(timezone(timedelta(hours=+9)))
                    target_date = now - timedelta(days=1) if raw_h > now.hour + 12 else now
                    start_time_str = f"{target_date.strftime('%Y-%m-%d')} {raw_h:02d}:00"
                    break
        except: pass

        for box in car_boxes:
            try:
                title = box.find("div", class_="car-list-title-area").get_text(strip=True)
                plate, model = title.split(" / ") if " / " in title else (title, "")

                status_list = []
                data_cells = []
                for r in box.find("table", class_="timetable").find_all("tr"):
                    cells = r.find_all("td")
                    if cells and any(x in (cells[0].get("class", [])) for x in ["vacant", "full", "impossible", "others"]):
                        data_cells = cells
                        break

                if data_cells:
                    for cell in data_cells:
                        sym = "○" if "vacant" in cell.get("class", []) else ("s" if "impossible" in cell.get("class", []) else "×")
                        for _ in range(int(cell.get("colspan", 1))): status_list.append(sym)

                if len(status_list) < 288: status_list += ["×"] * (288 - len(status_list))
                collected_data.append([city, station_name, plate.strip(), model.strip(), start_time_str, "".join(status_list)])
            except: pass

    # ==========================================================
    # III. 二重書き込み (CarData_Ryu & JKS本体)
    # ==========================================================
    if collected_data:
        print("\n[III.データ保存] 両シートへ書き込みます...")
        df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])

        for area in df_output['city'].unique():
            df_area = df_output[df_output['city'] == area].copy()
            area_name = str(area).replace('市', '').strip()
            work_sheet_name = f"{area_name}_更新用"
            df_to_write = df_area.drop(columns=['city'])
            data_to_upload = [df_to_write.columns.values.tolist()] + df_to_write.values.tolist()

            # 1. CarData_Ryu への書き込み
            try:
                try: ws_prod = sh_prod.worksheet(work_sheet_name)
                except gspread.WorksheetNotFound: ws_prod = sh_prod.add_worksheet(title=work_sheet_name, rows=len(df_area)+10, cols=10)
                ws_prod.clear()
                ws_prod.update(data_to_upload, range_name='A1')
                print(f"   -> CarData_Ryu: '{work_sheet_name}' 更新完了")
            except Exception as e:
                raise Exception(f"CarData_Ryuへの書き込みに失敗しました: {e}")

            # 2. JKS本体 への書き込み (ID: 16HYziQ...)
            try:
                try: ws_jks = sh_jks.worksheet(work_sheet_name)
                except gspread.WorksheetNotFound:
                    # 1ミリの不整合も許さないため、JKS側にシートがない場合はエラーで停止
                    raise Exception(f"JKS本体側に '{work_sheet_name}' タブが見つかりません。")
                ws_jks.clear()
                ws_jks.update(data_to_upload, range_name='A1')
                print(f"   -> JKS本体: '{work_sheet_name}' 更新完了")
            except Exception as e:
                raise Exception(f"JKS本体への同時書き込みに失敗しました: {e}")

        status_prefix = "【全件強制更新】" if TARGET_AREA == 'force_all' else "【更新完了】"
        send_discord_notification(f"<@1474004343207366839> ✅ {status_prefix} {TARGET_AREA.upper()} 両シートの更新が完了しました！")

except Exception as e:
    send_discord_notification(f"<@1474004343207366839> ❌ 【重大なエラー】 {TARGET_AREA.upper()} スクレイピング停止:\n```{e}```")
    print(f"\nエラー発生のため停止: {e}")
    sys.exit(1)

finally:
    if 'driver' in locals(): driver.quit()
    try:
        sh_prod_fin = gc.open_by_key(prod_sh_key)
        ws_status_fin = sh_prod_fin.worksheet("SystemStatus")
        ws_status_fin.clear()
    except: pass
