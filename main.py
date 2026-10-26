# ==========================================================
# 【GitHub Actions用】多摩・府中エリア巡回システム (JKS本体同時書き込み版)
# 改修内容:
# 1. 予約管理メイン(旧CarData_Ryu) と JKS本体(16HYziQ...) への同時同期機能
# 2. 新設ステーション対応(inspectionlogにない場合は未登録として送信)
# 3. エリア抽象化(多摩・府中のみ対象)
# 4. ★プレート正規化追加: TMAページのタイトル表記(伝統的なスペース区切り
#    ナンバー表示)をそのまま取り込まないよう、スクレイピング入口で
#    全角/半角スペース・ゼロ幅相当の空白類を除去する正規化を追加
# 5. ★スクレイピング/書き込み 2段ジョブ分割対応:
#    STAGE=scrape でスクレイピングのみ実行しcollected_data.jsonへ保存
#    STAGE=write  でJSON読込→シート書き込みのみ実行（リトライ対象はこちらのみ）
#    STAGE未指定(all)は従来通り一括実行（ローカル動作確認用）
# 6. ★書き込みリトライ発生時のDiscord通知を追加（label付き）。
#    5回とも失敗した場合は専用の「上限到達」通知のみ送信し、
#    従来の「重大なエラー」通知とは重複させない
# 7. ★TMAサーバー遅延対策（2026-10-26）:
#    - 固定sleepをやめ、ローディング画面消滅・車両BOX出現をWebDriverWaitで待機
#    - ページ読込タイムアウトを設定（1ページの無限待ちで全体が止まらないように）
#    - ログイン成否を確認し、失敗時は1回だけ再ログイン
#    - ステーション単位で最大3回試行（予約表の未描画・セッション切れ・タイムアウト）
#    - それでも失敗したステーションは最後にまとめてもう1周
#    - 取得できなかったステーション/車両はJSON経由でwrite jobへ渡し、完了通知に添える
#    - 再試行で順序が崩れないよう、結果は元のステーション順で組み立てる
# ==========================================================
import sys
import os
import re
import json
import pandas as pd
import gspread
import unicodedata
import urllib.request
from time import sleep
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

# エリアフィルタリング設定
TARGET_AREA = os.environ.get('TARGET_AREA', 'all').lower()
print(f"\n[エリア指定] {TARGET_AREA}")

# STAGE: 'scrape'=収集のみ, 'write'=書き込みのみ, 'all'=一括（未指定時のデフォルト、ローカル確認用）
STAGE = os.environ.get('STAGE', 'all').lower()
NEEDS_SCRAPE = STAGE in ('scrape', 'all')
NEEDS_WRITE = STAGE in ('write', 'all')
COLLECTED_DATA_FILE = "collected_data.json"

# Seleniumはscrape段階でのみ必要
if NEEDS_SCRAPE:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    # ★遅延対策: 待機・例外判定用
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException

# --- Discord通知用設定 ---
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL: return
    data = {"content": message}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(data).encode(), headers=headers)
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Discord通知エラー: {e}")

# ==========================================================
# ★プレート正規化関数
# TMAページの表示テキストは伝統的な「地名 分類番号 かな 一連番号」の
# スペース区切り表記のことがあるため、取得した瞬間にここで正規化する。
# 全角/半角スペース・タブ・改行などの空白類を位置を問わず全除去する。
# GAS側のnormalizePlate()と同じ考え方に合わせている。
# ==========================================================
def normalize_plate(s):
    if not s:
        return ""
    return re.sub(r'[\s　]', '', str(s)).strip()

# ==========================================================
# ★取得失敗一覧をDiscord通知用の文字列にまとめる
# 長くなりすぎないよう先頭20件まで表示し、残りは件数のみ
# ==========================================================
def format_failures(failed_stations, failed_vehicles):
    lines = []
    if failed_stations:
        shown = "、".join(failed_stations[:20])
        more = f" 他{len(failed_stations) - 20}件" if len(failed_stations) > 20 else ""
        lines.append(f"取得失敗ステーション {len(failed_stations)}件: {shown}{more}")
    if failed_vehicles:
        shown = "、".join(failed_vehicles[:20])
        more = f" 他{len(failed_vehicles) - 20}件" if len(failed_vehicles) > 20 else ""
        lines.append(f"取得失敗車両 {len(failed_vehicles)}台: {shown}{more}")
    return "\n".join(lines)

# 1. ログイン情報設定
LOGIN_URL = "https://dailycheck.tc-extsys.jp/tcrappsweb/web/login/tawLogin.html"
USER_ID_1 = os.environ.get("TMA_ID_1", "0030")
USER_ID_2 = os.environ.get("TMA_ID_2", "")
# PW_MODE環境変数（mode1/mode2）に応じてPWを切り替える（ポータルのPWモード切替と連携）
PW_MODE = os.environ.get('PW_MODE', 'mode1').lower()
PASSWORD_MAP = {
    "mode1": os.environ.get("TMA_PW_MODE1", ""),
    "mode2": os.environ.get("TMA_PW_MODE2", "")
}
PASSWORD = PASSWORD_MAP.get(PW_MODE, os.environ.get("TMA_PW_MODE1", ""))
print(f"[PWモード] {PW_MODE} を使用")

# 2. 設定
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/1LCyj16nsRYBk5cTpx2Sb75qmtm3YGKNEIdeyUvZzQQI/edit"
JKS_SHEET_ID = "16HYziQ5now1IATZJU3wZhTE08S_3B8xVP9MbfceHONE"

CSV_FILE_NAME = "station_code_map.csv"
INSPECTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/11XglLANtnG7bCxYjLRMGoZY25wspjHsGR3IG2ZyRITs/edit"

# ★遅延対策: 待機・再試行の設定
PAGE_LOAD_TIMEOUT = 60       # 1ページの読込上限(秒)
LOADING_WAIT_TIMEOUT = 30    # ローディング画面が消えるまでの上限(秒)
BOX_WAIT_TIMEOUT = 10        # ローディング後、車両BOXが出るまでの上限(秒)
TIMETABLE_WAIT_TIMEOUT = 90  # 車両BOX出現後、予約表が描画されるまでの上限(秒)
                             # ★描画が遅い車両はリロードせず同じページのまま待つ（リロードすると描画が最初からやり直しになるため）
STATION_MAX_ATTEMPTS = 3     # 1ステーションあたりの試行回数
EMPTY_MAX_ATTEMPTS = 2       # 車両0台だった場合の試行回数（本当に0台のステーションで時間を浪費しないため少なめ）
RETRY_BASE_WAIT = 5          # 再試行前の待機(秒)。試行ごとに倍
SECOND_PASS_WAIT = 30        # 失敗分をもう1周する前の待機(秒)
RETRY_ROUNDS_MAX = 5         # ★再巡回の最大周回数（全部取れるまで繰り返すが、永久に止まらないよう上限を設ける）
RETRY_TIME_BUDGET = 30 * 60  # ★再巡回の合計時間の上限(秒)。超えたら取れた分だけで書き込みへ進む

# 3. Google認証
SERVICE_ACCOUNT_KEY_FILE = "service_account.json"
if not os.path.exists(SERVICE_ACCOUNT_KEY_FILE):
    print("!! エラー: 認証キーファイルが見つかりません。")
    sys.exit(1)

gc = gspread.service_account(filename=SERVICE_ACCOUNT_KEY_FILE)

# ==========================================================
# ★上限到達済みリトライを示す専用例外
# with_retryが5回とも失敗した場合にこれを送出する。
# この時点で「上限到達」通知は送信済みのため、外側の
# 「重大なエラー」通知と重複させないための目印として使う。
# ==========================================================
class RetryExhaustedError(Exception):
    pass

# ★遅延対策用の例外
class StationNotReady(Exception):
    """ページが描画しきっていない（再試行対象）"""
    pass

class SessionExpired(Exception):
    """ログイン画面へ戻された（再ログイン後に再試行）"""
    pass

# ==========================================================
# リトライ付きAPI呼び出しラッパー（書き込み専用）
# Google Sheets APIの一時的なエラー(5xx, 429)のみリトライする
# 5回, 1→2→4→8→16秒の指数バックオフ
# ★label: どの書き込み処理か判別するための表示名（Discord通知用）
# ★リトライ試行のたびにDiscord通知。5回とも失敗した場合は
#   「上限到達」専用通知のみ送り、以後は外側で重複通知しない
# ==========================================================
def with_retry(func, *args, label="不明な処理", **kwargs):
    max_retries = 5
    delay = 1
    last_exception = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status_code = None
            try:
                status_code = e.response.status_code
            except Exception:
                pass
            is_retryable = (status_code == 429) or (status_code is not None and 500 <= status_code < 600)
            if not is_retryable:
                raise
            if attempt == max_retries:
                send_discord_notification(f"<@1474004343207366839> ⛔ {label} への書き込みに{max_retries}回失敗。時間をおいて再実行してください。")
                raise RetryExhaustedError(f"{label}: 上限{max_retries}回リトライしても失敗 ({e})") from e
            print(f"   !! [リトライ {attempt}/{max_retries}] APIエラー(status={status_code})。{delay}秒後に再試行します...")
            send_discord_notification(f"⚠️ [リトライ {attempt}/{max_retries}] {label} への書き込みを再試行中...")
            last_exception = e
            sleep(delay)
            delay *= 2
    if last_exception:
        raise last_exception

collected_data = []
failed_stations = []   # ★最終的に取得できなかったステーション名
failed_vehicles = []   # ★ステーションは取れたが予約表が取れなかった車両（station/plate）

if NEEDS_SCRAPE:
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
            sh_inspection = with_retry(gc.open_by_key, inspection_sh_key, label="inspectionlog取得(open)")
            ws_inspection = with_retry(sh_inspection.worksheet, "inspectionlog", label="inspectionlog取得(worksheet)")
            inspection_values = with_retry(ws_inspection.get_all_values, label="inspectionlog取得(values)")
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

            if norm_station not in inspection_status_map:
                print(f"   -> [未登録(新設)検知] 巡回対象に追加: {item.get('station')}")
                final_target_stations.append(item)
                continue

            if not all((s in skip_statuses) for s in inspection_status_map[norm_station]):
                final_target_stations.append(item)

        target_stations = final_target_stations

    if len(target_stations) == 0:
        send_discord_notification(f"<@1474004343207366839> ⚠️ 【データなし】 {TARGET_AREA.upper()} 更新対象データがありません。")
        if STAGE == 'scrape':
            with open(COLLECTED_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"target_area": TARGET_AREA, "collected_data": [], "failed_stations": [], "failed_vehicles": []}, f, ensure_ascii=False)
        sys.exit()

    # ==========================================================
    # ドライバ設定
    # ★遅延対策: ページ読込タイムアウトを設定し、落ちた時に作り直せるよう関数化
    # ==========================================================
    def create_driver():
        options = Options()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        d = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        d.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        return d

    # ==========================================================
    # ★ログイン（成否確認つき・失敗時は1回だけ再試行）
    # ログイン成功後はURLからloginが消えることで判定する（yoyakuLongと同じ判定）
    # ==========================================================
    def login(d):
        last_err = None
        for attempt in range(1, 3):
            try:
                d.get(LOGIN_URL)
                WebDriverWait(d, LOADING_WAIT_TIMEOUT).until(EC.presence_of_element_located((By.ID, "cardNo1")))
                d.find_element(By.ID, "cardNo1").clear()
                d.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
                d.find_element(By.ID, "cardNo2").clear()
                d.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
                d.find_element(By.ID, "password").clear()
                d.find_element(By.ID, "password").send_keys(PASSWORD)
                d.find_element(By.ID, "password").send_keys(Keys.RETURN)
                WebDriverWait(d, LOADING_WAIT_TIMEOUT).until(lambda x: "login" not in x.current_url.lower())
                d.get("https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStation.html")
                sleep(2)
                print("[ログイン] 成功")
                return
            except Exception as e:
                last_err = e
                print(f"   !! [ログイン失敗 {attempt}/2] {type(e).__name__}: {e}")
                if attempt < 2:
                    sleep(10)
        raise Exception(f"TMAへのログインに失敗しました（PWモード={PW_MODE}・2回試行）: {type(last_err).__name__}: {last_err}")

    def driver_alive(d):
        try:
            _ = d.current_url
            return True
        except Exception:
            return False

    # ==========================================================
    # ★1ステーション分を1回だけ取得する
    # 戻り値: (rows, bad_plates)
    #   rows       = collected_dataに入れる行のリスト
    #   bad_plates = 予約表が描画されていなかった車両のプレート
    # 描画途中などで判断できない場合は StationNotReady / SessionExpired を送出
    # ==========================================================
    def scrape_station_once(d, item):
        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        city = str(item.get('city', 'other')).strip()

        d.get(f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}")
        if "login" in d.current_url.lower():
            raise SessionExpired("ログイン画面へ戻されました")

        # ローディング画面が消えるまで待つ
        try:
            WebDriverWait(d, LOADING_WAIT_TIMEOUT).until(EC.invisibility_of_element_located((By.CLASS_NAME, "loading-view")))
        except TimeoutException:
            raise StationNotReady(f"ローディング画面が{LOADING_WAIT_TIMEOUT}秒で消えませんでした")

        # 車両BOXが出るまで待つ（出なければ車両0台の可能性）
        try:
            WebDriverWait(d, BOX_WAIT_TIMEOUT).until(EC.presence_of_element_located((By.CLASS_NAME, "car-list-box")))
        except TimeoutException:
            if "login" in d.current_url.lower():
                raise SessionExpired("ログイン画面へ戻されました")
            return [], []

        # ★予約表の描画待ち（車両BOXの出現後に予約表が遅れて描画されるため）
        # 全車両BOXで予約データ行が揃うまで、リロードせず同じページのまま待つ。
        # 時間内に揃わなければその時点の内容で判定し、未描画の車両は最後の再巡回に回す
        def boxes_ready(x):
            s = BeautifulSoup(x.page_source, "lxml")
            boxes = s.find_all("div", class_="car-list-box")
            if not boxes:
                return False
            for box in boxes:
                if box.find("div", class_="car-list-title-area") is None:
                    return False
                tt = box.find("table", class_="timetable")
                if tt is None:
                    return False
                has_data = False
                for r in tt.find_all("tr"):
                    cells = r.find_all("td")
                    if cells and any(x in (cells[0].get("class", [])) for x in ["vacant", "full", "impossible", "others"]):
                        has_data = True
                        break
                if not has_data:
                    return False
            return ("ready", s)
        try:
            _, soup = WebDriverWait(d, TIMETABLE_WAIT_TIMEOUT, poll_frequency=0.5).until(boxes_ready)
        except TimeoutException:
            print(f"   !! 予約表が{TIMETABLE_WAIT_TIMEOUT}秒待っても揃いませんでした（描画済みの車両のみ採用）")
            soup = BeautifulSoup(d.page_source, "lxml")
        car_boxes = soup.find_all("div", class_="car-list-box")

        # タイムライン開始時刻取得 (DOMに依存せず現在時刻のHH:00を使用)
        now_jst = datetime.now(timezone(timedelta(hours=+9)))
        start_time_str = now_jst.strftime('%Y-%m-%d %H:00')

        rows = []
        bad_plates = []
        for box in car_boxes:
            title_el = box.find("div", class_="car-list-title-area")
            if title_el is None:
                raise StationNotReady("車両タイトルが未描画です")
            title = title_el.get_text(strip=True)
            plate_raw, model = title.split(" / ") if " / " in title else (title, "")
            # ★ここでTMA表示由来の空白（表示フォーマットのスペース区切り）を正規化して除去
            plate = normalize_plate(plate_raw)

            try:
                status_list = []
                data_cells = []
                timetable = box.find("table", class_="timetable")
                if timetable is not None:
                    for r in timetable.find_all("tr"):
                        cells = r.find_all("td")
                        if cells and any(x in (cells[0].get("class", [])) for x in ["vacant", "full", "impossible", "others"]):
                            data_cells = cells
                            break

                # ★予約データ行が無い＝描画途中。全「×」で書き込まないよう不良扱いにする
                if not data_cells:
                    bad_plates.append(plate)
                    continue

                for cell in data_cells:
                    sym = "○" if "vacant" in cell.get("class", []) else ("s" if "impossible" in cell.get("class", []) else "×")
                    for _ in range(int(cell.get("colspan", 1))): status_list.append(sym)

                if len(status_list) < 288: status_list += ["×"] * (288 - len(status_list))
                rows.append([city, station_name, plate, model.strip(), start_time_str, "".join(status_list)])
            except Exception as ex:
                print(f"  !! 車両解析エラー [{station_name}]: {ex}")
                bad_plates.append(plate)

        return rows, bad_plates

    # ==========================================================
    # ★1ステーション分を再試行つきで取得する
    # - 描画未完了・タイムアウト → 待ってから再試行
    # - セッション切れ → 再ログインして再試行
    # - ブラウザが落ちた → ドライバを作り直して再ログイン
    # - 一部車両だけ予約表が取れない → ★リロードによる再試行はせず、取れた分だけ採用
    #   （同じページで最大TIMETABLE_WAIT_TIMEOUT秒待ち済み。未描画の車両は最後の再巡回で再取得）
    # - 車両0台 → EMPTY_MAX_ATTEMPTS回まで確認し、それでも0台なら0台として確定
    # 全試行失敗時は例外を送出（呼び出し側で失敗ステーションとして扱う）
    # ==========================================================
    def scrape_station(state, item):
        last_err = None
        for attempt in range(1, STATION_MAX_ATTEMPTS + 1):
            is_last = (attempt == STATION_MAX_ATTEMPTS)
            try:
                rows, bad_plates = scrape_station_once(state["driver"], item)

                if not rows and not bad_plates:
                    if attempt < EMPTY_MAX_ATTEMPTS:
                        raise StationNotReady("車両が0台でした（念のため再確認）")
                    print(f"   -> 車両0台として確定")
                    return rows, bad_plates

                if bad_plates:
                    print(f"   !! 予約表が未描画の車両あり（再巡回で再取得）: {', '.join(bad_plates)}")

                return rows, bad_plates

            except SessionExpired as e:
                last_err = e
                print(f"   !! [試行 {attempt}/{STATION_MAX_ATTEMPTS}] {e} → 再ログインします")
                try:
                    login(state["driver"])
                except Exception as le:
                    last_err = le
            except (StationNotReady, TimeoutException) as e:
                last_err = e
                print(f"   !! [試行 {attempt}/{STATION_MAX_ATTEMPTS}] {type(e).__name__}: {e}")
            except WebDriverException as e:
                last_err = e
                print(f"   !! [試行 {attempt}/{STATION_MAX_ATTEMPTS}] ブラウザエラー: {str(e).splitlines()[0] if str(e) else type(e).__name__}")
                if not driver_alive(state["driver"]):
                    print("   !! ブラウザが応答しないため作り直します")
                    try: state["driver"].quit()
                    except Exception: pass
                    state["driver"] = create_driver()
                    login(state["driver"])

            if not is_last:
                sleep(RETRY_BASE_WAIT * (2 ** (attempt - 1)))

        raise Exception(f"{STATION_MAX_ATTEMPTS}回試行しても取得できませんでした（最後のエラー: {type(last_err).__name__}: {last_err}）")

    state = {"driver": create_driver()}

    try:
        # ==========================================================
        # II. データ収集
        # ==========================================================
        prod_sh_key = PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
        sh_prod = gc.open_by_key(prod_sh_key)
        sh_jks = gc.open_by_key(JKS_SHEET_ID)

        login(state["driver"])

        # ★再試行で順序が崩れないよう、結果はステーションの元の順番(index)で保持する
        results_by_index = {}
        bad_by_index = {}
        failed_indexes = []

        for i, item in enumerate(target_stations):
            if (i > 0) and (i % 20 == 0):
                try:
                    ws_status = sh_prod.worksheet("SystemStatus")
                    ws_status.update([["progress", i, len(target_stations)]], "A1")
                except: pass

            station_name = item.get('station', '不明')
            print(f"[{i+1}/{len(target_stations)}] {station_name}...")
            try:
                rows, bad_plates = scrape_station(state, item)
                results_by_index[i] = rows
                bad_by_index[i] = bad_plates
            except Exception as ex:
                print(f"  !! [後回し] {station_name}: {ex}")
                failed_indexes.append(i)

        # ★失敗分の再巡回（サーバーが一時的に重かった分の回収）
        # 未描画の車両が残ったステーションも対象にし、再取得で未描画が減った場合のみ結果を差し替える
        # 全部取れるまで周回を繰り返すが、RETRY_ROUNDS_MAX周 または RETRY_TIME_BUDGET秒 で打ち切り、
        # 取れた分だけで書き込みへ進む（1台のために全体が永久に止まり、何も書き込まれない事態を防ぐ）
        retry_started = datetime.now().timestamp()
        for round_no in range(1, RETRY_ROUNDS_MAX + 1):
            partial_indexes = [i for i in range(len(target_stations)) if bad_by_index.get(i)]
            retry_indexes = sorted(set(failed_indexes) | set(partial_indexes))
            if not retry_indexes:
                break
            if datetime.now().timestamp() - retry_started > RETRY_TIME_BUDGET:
                print(f"\n[再巡回] 合計{RETRY_TIME_BUDGET // 60}分を超えたため打ち切ります。")
                break
            print(f"\n[再巡回 {round_no}周目/{RETRY_ROUNDS_MAX}] 取得失敗 {len(failed_indexes)}件・未描画車両ありのステーション {len(partial_indexes)}件を{SECOND_PASS_WAIT}秒後に再取得します...")
            sleep(SECOND_PASS_WAIT)
            still_failed = []
            for n, i in enumerate(retry_indexes):
                if datetime.now().timestamp() - retry_started > RETRY_TIME_BUDGET:
                    print(f"   !! 再巡回が合計{RETRY_TIME_BUDGET // 60}分を超えたため、残りは打ち切ります。")
                    still_failed.extend(j for j in retry_indexes[n:] if j not in results_by_index)
                    break
                item = target_stations[i]
                station_name = item.get('station', '不明')
                print(f"[再巡回{round_no} {n+1}/{len(retry_indexes)}] {station_name}...")
                try:
                    rows, bad_plates = scrape_station(state, item)
                    if i not in results_by_index or len(bad_plates) < len(bad_by_index.get(i, [])):
                        results_by_index[i] = rows
                        bad_by_index[i] = bad_plates
                except Exception as ex:
                    print(f"  !! [取得失敗] {station_name}: {ex}")
                    if i not in results_by_index:
                        still_failed.append(i)
            failed_indexes = still_failed

        # 元の順番で組み立て
        for i in range(len(target_stations)):
            if i in results_by_index:
                collected_data.extend(results_by_index[i])
            for plate in bad_by_index.get(i, []):
                failed_vehicles.append(f"{target_stations[i].get('station', '不明')}/{plate}")
        failed_stations = [target_stations[i].get('station', '不明') for i in failed_indexes]

        if failed_stations or failed_vehicles:
            print(f"\n[取得失敗まとめ]\n{format_failures(failed_stations, failed_vehicles)}")

        if STAGE == 'scrape':
            # ★scrape専用: 収集結果をJSONに保存。書き込みはwrite jobに任せる
            with open(COLLECTED_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "target_area": TARGET_AREA,
                    "collected_data": collected_data,
                    "failed_stations": failed_stations,
                    "failed_vehicles": failed_vehicles
                }, f, ensure_ascii=False)
            print(f"\n[scrape完了] {len(collected_data)}件を{COLLECTED_DATA_FILE}に保存しました。")

    except Exception as e:
        print(f"\nエラー発生のため停止: {e}")
        if not isinstance(e, RetryExhaustedError):
            send_discord_notification(f"<@1474004343207366839> ❌ 【重大なエラー】 {TARGET_AREA.upper()} スクレイピング停止:\n```{e}```")
        sys.exit(1)

    finally:
        try: state["driver"].quit()
        except Exception: pass
        try:
            sh_prod_fin = gc.open_by_key(prod_sh_key)
            ws_status_fin = sh_prod_fin.worksheet("SystemStatus")
            ws_status_fin.clear()
        except: pass

if NEEDS_WRITE:
    if STAGE == 'write':
        # ★write専用: scrape jobがartifactとして保存したJSONを読み込む
        if not os.path.exists(COLLECTED_DATA_FILE):
            print(f"!! エラー: {COLLECTED_DATA_FILE} が見つかりません（scrape jobのartifactを確認してください）。")
            sys.exit(1)
        with open(COLLECTED_DATA_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        collected_data = payload.get("collected_data", [])
        TARGET_AREA = payload.get("target_area", TARGET_AREA)
        failed_stations = payload.get("failed_stations", [])
        failed_vehicles = payload.get("failed_vehicles", [])

    failure_text = format_failures(failed_stations, failed_vehicles)

    try:
        # ==========================================================
        # III. 二重書き込み (予約管理メイン & JKS本体)
        # ==========================================================
        if collected_data:
            print("\n[III.データ保存] 両シートへ書き込みます...")
            prod_sh_key = PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
            sh_prod = gc.open_by_key(prod_sh_key)
            sh_jks = gc.open_by_key(JKS_SHEET_ID)

            df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])

            for area in df_output['city'].unique():
                df_area = df_output[df_output['city'] == area].copy()
                area_name = str(area).replace('市', '').strip()
                work_sheet_name = f"{area_name}_更新用"
                df_to_write = df_area.drop(columns=['city'])
                data_to_upload = [df_to_write.columns.values.tolist()] + df_to_write.values.tolist()

                # 1. 予約管理メイン（旧CarData_Ryu）への書き込み
                try:
                    try: ws_prod = with_retry(sh_prod.worksheet, work_sheet_name, label=f"予約管理メイン/{work_sheet_name}")
                    except gspread.WorksheetNotFound: ws_prod = with_retry(sh_prod.add_worksheet, title=work_sheet_name, rows=len(df_area)+10, cols=10, label=f"予約管理メイン/{work_sheet_name}")
                    with_retry(ws_prod.clear, label=f"予約管理メイン/{work_sheet_name}")
                    with_retry(ws_prod.update, data_to_upload, range_name='A1', label=f"予約管理メイン/{work_sheet_name}")
                    print(f"   -> 予約管理メイン: '{work_sheet_name}' 更新完了")
                except Exception as e:
                    if isinstance(e, RetryExhaustedError): raise
                    raise Exception(f"予約管理メインへの書き込みに失敗しました: {e}")

                # 2. JKS本体 への書き込み
                try:
                    try: ws_jks = with_retry(sh_jks.worksheet, work_sheet_name, label=f"JKS本体/{work_sheet_name}")
                    except gspread.WorksheetNotFound:
                        raise Exception(f"JKS本体側に '{work_sheet_name}' タブが見つかりません。")
                    with_retry(ws_jks.clear, label=f"JKS本体/{work_sheet_name}")
                    with_retry(ws_jks.update, data_to_upload, range_name='A1', label=f"JKS本体/{work_sheet_name}")
                    print(f"   -> JKS本体: '{work_sheet_name}' 更新完了")
                except Exception as e:
                    if isinstance(e, RetryExhaustedError): raise
                    raise Exception(f"JKS本体への同時書き込みに失敗しました: {e}")

            # force_all完了後: GASのforceAllAbsenceCheckを呼び出してTMA不在シートを上書き更新
            if TARGET_AREA == 'force_all':
                GAS_URL = "https://script.google.com/macros/s/AKfycbwpNRM_753x0gG5sl5_LTwxn5afUUQqezpmPb874-Stsl5aVUJBLTBk70nW5RE_mdU0/exec"
                try:
                    req = urllib.request.Request(
                        f"{GAS_URL}?action=forceAllAbsenceCheck",
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    with urllib.request.urlopen(req, timeout=60) as res:
                        gas_result = json.loads(res.read().decode())
                        print(f"[forceAllAbsenceCheck] {gas_result}")
                except Exception as e:
                    print(f"!! [警告] forceAllAbsenceCheck呼び出し失敗: {e}")
                    send_discord_notification(f"<@1474004343207366839> ⚠️ 【警告】 force_all完了後のTMA不在チェック呼び出しに失敗しました:\n```{e}```")

            status_prefix = "【全件強制更新】" if TARGET_AREA == 'force_all' else "【更新完了】"
            # ★取得失敗があれば完了通知に添える
            if failure_text:
                send_discord_notification(f"<@1474004343207366839> ✅ {status_prefix} {TARGET_AREA.upper()} 両シートの更新が完了しました（一部取得失敗あり）\n{failure_text}")
            else:
                send_discord_notification(f"<@1474004343207366839> ✅ {status_prefix} {TARGET_AREA.upper()} 両シートの更新が完了しました！")
        else:
            # データが0件の場合はDiscordに警告を送る
            print("!! [警告] スクレイピング完了しましたがデータが0件でした")
            extra = f"\n{failure_text}" if failure_text else ""
            send_discord_notification(f"<@1474004343207366839> ⚠️ 【警告】 {TARGET_AREA.upper()} スクレイピング完了しましたがデータが0件でした。ログインや構造を確認してください。{extra}")

    except Exception as e:
        print(f"\nエラー発生のため停止: {e}")
        if not isinstance(e, RetryExhaustedError):
            send_discord_notification(f"<@1474004343207366839> ❌ 【重大なエラー】 {TARGET_AREA.upper()} スクレイピング停止:\n```{e}```")
        sys.exit(1)
