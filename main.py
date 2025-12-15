# ==========================================================
# IV. Google Sheetsへの書き込み
# ==========================================================
print(f"\n[IV. Google Sheetsへの書き込み]")

# スプレッドシート接続
try:
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(WORK_SS_ID)
except Exception as e:
    print(f"エラー: スプレッドシートに接続できませんでした: {e}")
    sys.exit()

# 各エリアごとに更新用シートへ書き込む
for area_info in AREAS:
    sheet_name_update = area_info['update']  # 例: 大和_更新用
    target_city       = area_info['display'] # 例: 大和

    # そのエリアのデータだけを抽出
    df_area = df_active[df_active['city'] == target_city].copy()

    # 更新用シートへ書き込み（ここだけ行う）
    try:
        ws_update = sh.worksheet(sheet_name_update)
        ws_update.clear() # 前回のデータを消去
        set_with_dataframe(ws_update, df_area) # 新しいデータを書き込み
        print(f"-> '{sheet_name_update}' : {len(df_area)} 件 更新完了")
        
    except gspread.exceptions.WorksheetNotFound:
        print(f"-> 警告: シート '{sheet_name_update}' が見つかりません。スキップします。")
    except Exception as e:
        print(f"-> エラー: '{sheet_name_update}' 書き込み中に問題発生: {e}")

print("\n✅ Python側の処理完了。あとはGASのトリガーを待ちます。")
