import os
import pyperclip

def copy_files_contents():
    # 検索を開始するフォルダのパス
    folder_path = r"C:\Users\NishiharaKatsuhiko\Desktop\python\UI"
    
    # 検索対象のファイル名リスト
    target_files = [
        "style.css",
        "index.html",
        "minutes_list.html",
        "result.html",
        "app.py",
        "requirements.txt"
    ]
    
    # 見つかったファイルの中身を連結するためのリスト
    combined_content = []
    # 見つかったファイル名を記録するためのセット
    found_file_names = set()
    
    # os.walk を使ってサブフォルダを含む再帰的な検索を行う
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            # ファイル名が検索対象のリストに含まれるか判定
            if file in target_files:
                file_path = os.path.join(root, file)
                
                # ファイルの中身を読み込み
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                # 見つかったファイルを記録
                found_file_names.add(file)
                
                # 見出しとしてファイル名を追加し、内容を追記
                combined_content.append(f"===== {file} =====\n{content}\n")
    
    # 見つからなかったファイル名に対するメッセージを追加
    for file in target_files:
        if file not in found_file_names:
            combined_content.append(f"{file} は存在しません。\n")
    
    # 全ファイルの内容をひとつの文字列にまとめる
    final_text = "\n".join(combined_content)
    # クリップボードへコピー
    pyperclip.copy(final_text)
    
    # 完了メッセージ
    print("コピーできたら、完了しました。")

if __name__ == "__main__":
    copy_files_contents()
