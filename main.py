# ==========================================================
# 【GitHub Actions用】3エリア巡回システム (高速化＆Discord通知＆動的除外版)
# 改修内容: force_allモード対応（全件取得ロジック追加）
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
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req) as res:
            pass
    except Exception as e:
        print(f"Discord通知失敗: {e}")

# --- 環境変数から実行対象エリアを取得 ---
TARGET_AREA = os.environ.get("TARGET_AREA", "all")

# ==========================================================
# I. 設定・スプレッドシート準備
# ==========================================================
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/13cQngK_Xx38VU67yLS-iTHyOZgsACZdxM34l-Jq_U9A/edit"
WORK_SHEET_ID = "11XglLANtnG7bCxYjLRMGoZY25wspjHsGR3IG2ZyRITs" # 業務用SS (inspectionlog)
STATION_CSV_URL = "https://raw.githubusercontent.com/rkworks2025-coder/yoyaku/main/target_stations.csv"

# GCP認証設定
SCOPE = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
gc = gspread.service_account(filename='service_account.json')

# スプレッドシートを開く
sh_prod = gc.open_by_url(PRODUCTION_SHEET_URL)
sh_work = gc.open_by_key(WORK_SHEET_ID)

# ステータス更新用シート (SystemStatus)
ws_status = sh_prod.get_worksheet(0)

def update_status(current, total):
    ws_status.update_acell('B1', current)
    ws_status.update_acell('C1', total)

# --- 1. inspectionlogの読み込み (除外リスト作成) ---
ws_log = sh_work.sheet1 # inspectionlog
log_data = ws_log.get_all_values()
df_log = pd.DataFrame(log_data[1:], columns=log_data[0])

# 除外対象のステータス
EXCLUDE_STATUSES = ["checked", "unnecessary", "7days_rule"]
excluded_plates = set(df_log[df_log.iloc[:, 12].isin(EXCLUDE_STATUSES)].iloc[:, 0].tolist())

# --- 2. 巡回対象ステーションCSVの読み込み ---
df_stations = pd.read_csv(STATION_CSV_URL)

# --- 3. 動的な巡回リストの作成 ---
target_stations = []

# エリアフィルタリングの定義
if TARGET_AREA == "kanagawa":
    df_filtered = df_stations[df_stations['area'].isin(['大和', '海老名'])]
elif TARGET_AREA == "tama":
    df_filtered = df_stations[df_stations['area'] == '多摩']
else:
    # all または force_all の場合は全エリアを対象にする
    df_filtered = df_stations

# 車両ごとの除外判定
for idx, row in df_filtered.iterrows():
    plate = str(row['plate_number']).strip()
    
    # 【核心部分】force_all モード時は、除外判定をスキップして全て追加
    if TARGET_AREA == "force_all":
        target_stations.append(row.to_dict())
    else:
        # 通常モード（all, kanagawa, tama）時は、除外リストにある車両をスキップ
        if plate not in excluded_plates:
            target_stations.append(row.to_dict())

total_count = len(target_stations)
print(f"実効モード: {TARGET_AREA}")
print(f"最終巡回対象数: {total_count}")
update_status(0, total_count)

if total_count == 0:
    msg = f"ℹ️ 【スキップ】 {TARGET_AREA.upper()} に該当する車両はありませんでした。"
    print(msg)
    send_discord_notification(msg)
    sys.exit(0)

# ==========================================================
# II. スクレイピング (Selenium)
# ==========================================================
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

results = []

try:
    # ログイン処理
    driver.get("https://share.timescar.jp/view/member/mypage.jsp")
    sleep(1)
    driver.find_element(By.NAME, "cardNo").send_keys("0010118843")
    driver.find_element(By.NAME, "tpPass").send_keys("8843")
    driver.find_element(By.ID, "login-button").click()
    sleep(1)

    # 巡回
    for i, station in enumerate(target_stations):
        current_num = i + 1
        url = f"https://share.timescar.jp/view/reserve/step1.jsp?stationCardNo={station['station_code']}&carCardNo={station['car_code']}"
        driver.get(url)
        
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        slots = []
        table = soup.find('table', class_='com-table-res-status')
        if table:
            tds = table.find_all('td')
            for td in tds:
                classes = td.get('class', [])
                if 'res-ok' in classes: slots.append('○')
                elif 'res-ng' in classes: slots.append('×')
                else: slots.append('-')

        while len(slots) < 72: slots.append('-')
        
        results.append([
            station['station_name'],
            station['plate_number'],
            station['car_model'],
            station['area']
        ] + slots[:72])
        
        if current_num % 10 == 0 or current_num == total_count:
            update_status(current_num, total_count)
            print(f"Progress: {current_num}/{total_count}")

    # ==========================================================
    # III. 書き込み
    # ==========================================================
    if results:
        df_res = pd.DataFrame(results)
        areas = ['大和', '海老名', '多摩']
        
        for area in areas:
            df_area = df_res[df_res[3] == area]
            if df_area.empty: continue
            
            sheet_name = f"{area}_更新用"
            try:
                ws_work = sh_prod.worksheet(sheet_name)
            except:
                ws_work = sh_prod.add_worksheet(title=sheet_name, rows=len(df_area)+10, cols=80)

            cols = ["ステーション", "ナンバー", "車種", "エリア"] + [str(i) for i in range(72)]
            df_to_write = df_area.copy()
            df_to_write.columns = cols
            
            upload_data = [df_to_write.columns.values.tolist()] + df_to_write.values.tolist()
            ws_work.clear()
            ws_work.update(upload_data, range_name='A1')

        status_prefix = "【全件強制更新】" if TARGET_AREA == "force_all" else "【更新完了】"
        success_msg = f"✅ {status_prefix} {TARGET_AREA} のデータ更新が完了しました ({total_count}台)"
        send_discord_notification(success_msg)

except Exception as e:
    error_msg = f"❌ 【実行エラー】 {TARGET_AREA} モードでエラーが発生しました:\n{e}"
    print(error_msg)
    send_discord_notification(error_msg)

finally:
    if 'driver' in locals():
        driver.quit()
    update_status(total_count, total_count)
