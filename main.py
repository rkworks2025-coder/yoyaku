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
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req) as res:
            pass
    except Exception as e:
        print(f"Discord通知失敗: {e}")

# --- 環境変数から実行対象エリアを取得 ---
# 'all', 'kanagawa', 'tama', 'force_all' のいずれか
TARGET_AREA = os.environ.get("TARGET_AREA", "all")

# ==========================================================
# I. 設定・スプレッドシート準備
# ==========================================================
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/1Bf4hP5q9G78KOf8xV1lV_S0T8mOqG3V3jF4H_tT1Y-Y/edit"
WORK_SHEET_ID = "11XglLANtnG7bCxYjLRMGoZY25wspjHsGR3IG2ZyRITs" # 業務用SS (inspectionlog)
STATION_CSV_URL = "https://raw.githubusercontent.com/rkworks2025-coder/yoyaku/main/target_stations.csv"

# GCP認証設定
SCOPE = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
gc = gspread.service_account(filename='service_account.json')

# スプレッドシートを開く
sh_prod = gc.open_by_url(PRODUCTION_SHEET_URL)
sh_work = gc.open_by_key(WORK_SHEET_ID)

# ステータス更新用シート (SystemStatus)
ws_status = sh_prod.get_worksheet(0) # 1番目のシート

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

# エリアフィルタリング
# force_all の場合は全エリアを対象にする
if TARGET_AREA == "kanagawa":
    df_stations = df_stations[df_stations['area'].isin(['大和', '海老名'])]
elif TARGET_AREA == "tama":
    df_stations = df_stations[df_stations['area'] == '多摩']
# 'all' または 'force_all' の場合は全エリア対象 (フィルタなし)

# --- 3. 動的除外ロジックの適用 ---
target_stations = []
for idx, row in df_stations.iterrows():
    plate = str(row['plate_number']).strip()
    
    # force_all モードなら、除外リスト(excluded_plates)に含まれていても無視して追加する
    if TARGET_AREA == "force_all":
        target_stations.append(row.to_dict())
    else:
        # 通常モードなら、除外リストにある車両はスキップ
        if plate not in excluded_plates:
            target_stations.append(row.to_dict())

total_count = len(target_stations)
print(f"モード: {TARGET_AREA}")
print(f"巡回対象車両数: {total_count}")
update_status(0, total_count)

if total_count == 0:
    msg = f"ℹ️ 【スキップ】 {TARGET_AREA.upper()} エリアに巡回対象の車両はありませんでした。"
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

# 結果格納用
results = []

try:
    # タイムズカー ログイン画面
    driver.get("https://share.timescar.jp/view/member/mypage.jsp")
    sleep(1)
    
    # ログイン (ID/PASSはSecretsから取得推奨だが一旦直書き/適宜変更)
    driver.find_element(By.NAME, "cardNo").send_keys("0010118843")
    driver.find_element(By.NAME, "tpPass").send_keys("8843")
    driver.find_element(By.ID, "login-button").click()
    sleep(1)

    # 巡回開始
    for i, station in enumerate(target_stations):
        current_num = i + 1
        print(f"[{current_num}/{total_count}] {station['station_name']} ({station['plate_number']}) を確認中...")
        
        # 車両予約ページへ直接遷移 (高速化)
        url = f"https://share.timescar.jp/view/reserve/step1.jsp?stationCardNo={station['station_code']}&carCardNo={station['car_code']}"
        driver.get(url)
        
        # HTML解析
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        
        # 予約状況テーブル (3日間・72スロット分)
        # 実際の実装ではここでHTMLから「○」「×」を抽出してリスト化する
        # ここでは枠組みのみ。実際のパースロジックを既存コードから維持
        
        slots = []
        # --- [パースロジック開始] ---
        # 既存の mai.py にある <td> クラス判定ロジックをここに適用
        table = soup.find('table', class_='com-table-res-status')
        if table:
            tds = table.find_all('td')
            for td in tds:
                if 'res-ok' in td.get('class', []):
                    slots.append('○')
                elif 'res-ng' in td.get('class', []):
                    slots.append('×')
                else:
                    slots.append('-')
        # --- [パースロジック終了] ---

        # 72個に満たない、または取得失敗時の補完
        while len(slots) < 72:
            slots.append('-')
        
        results.append([
            station['station_name'],
            station['plate_number'],
            station['car_model'],
            station['area']
        ] + slots[:72])
        
        # 10件ごとにスプレッドシートのステータスを更新 (GAS側のバーに反映)
        if current_num % 5 == 0 or current_num == total_count:
            update_status(current_num, total_count)

    # ==========================================================
    # III. 結果の書き込み
    # ==========================================================
    if results:
        df_res = pd.DataFrame(results)
        # エリアごとにシートを分ける
        areas_to_process = ['大和', '海老名', '多摩']
        
        for area in areas_to_process:
            df_area = df_res[df_res[3] == area]
            if df_area.empty: continue
            
            # シート名決定
            work_sheet_name = f"{area}_更新用"
            try:
                ws_work = sh_prod.worksheet(work_sheet_name)
            except:
                ws_work = sh_prod.add_worksheet(title=work_sheet_name, rows=len(df_area)+10, cols=80)

            # データ整形
            # カラム名: ステーション, ナンバー, 車種, エリア, 0, 1, 2, ...
            cols = ["ステーション", "ナンバー", "車種", "エリア"] + [str(i) for i in range(72)]
            df_to_write = df_area.copy()
            df_to_write.columns = cols
            
            data_to_upload = [df_to_write.columns.values.tolist()] + df_to_write.values.tolist()
            
            ws_work.clear()
            ws_work.update(data_to_upload, range_name='A1')
            print(f"   -> '{work_sheet_name}' シート更新完了")
            
        # 正常完了時のDiscord通知
        success_msg = f"✅ 【更新完了】 {TARGET_AREA.upper()} モードのデータ更新が完了しました！ ({total_count}台)"
        print(f"\n{success_msg}")
        send_discord_notification(success_msg)

except Exception as e:
    error_msg = f"❌ 【重大なエラー】 {TARGET_AREA.upper()} モードでエラーが発生しました:\n```{e}```"
    print(f"\n{error_msg}")
    send_discord_notification(error_msg)

finally:
    if 'driver' in locals():
        driver.quit()
    
    print("\n[終了処理] ステータスをリセットします...")
    try:
        update_status(total_count, total_count) # 完了状態へ
    except:
        pass
    print("完了。")
