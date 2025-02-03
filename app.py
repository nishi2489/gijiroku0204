#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import time
import logging
import threading
import webbrowser
import ctypes
from datetime import datetime, timedelta
import os.path
import re
from pathlib import Path
from docx import Document
from docx.shared import Pt

from flask import Flask, request, render_template, session, redirect, url_for, flash, jsonify, g
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_session import Session

# 変更点: openai ではなく OpenAI/RateLimitError/APIError を直接 import
from openai import OpenAI, APIError, RateLimitError

from win32com.client import Dispatch

# === python-dotenv で .env を読み取る
from dotenv import load_dotenv

# === Supabase を使う場合 ===
from supabase import create_client, Client

# 音声関係
import speech_recognition as sr
from pydub import AudioSegment

# 文字コード自動判定
import chardet

# 追加: tkinter を使って「保存先」をダイアログで選択
import tkinter
from tkinter import filedialog

import json
from queue import Queue
from flask import Response

SECRET_KEY = "dummy_secret_key_for_session"
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Flask-Sessionの設定
app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)  # セッション有効期限を1日に設定
app.config['SESSION_COOKIE_SECURE'] = True  # HTTPSのみでクッキーを送信
app.config['SESSION_COOKIE_HTTPONLY'] = True  # JavaScriptからのクッキーアクセスを防止

# セッション初期化の前にシークレットキーを設定
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'your-secret-key-here')  # 環境変数から取得するように変更

Session(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'  # ログインが必要な場合にリダイレクトする先
login_manager.session_protection = "strong"  # セッション保護を強化

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

############################################
# 1) .env から環境変数を読み込み
############################################
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logging.warning("【警告】OPENAI_API_KEY が .env で設定されていません。要約は動作しない可能性があります。")
else:
    logging.info("OpenAI APIキーが正常に読み込まれました。")

############################################
# OpenAIクライアントの初期化
############################################
client = OpenAI(api_key=OPENAI_API_KEY)

############################################
# 2) Supabase を使いたい場合の初期化
############################################
supabase: Client = None
if SUPABASE_URL and SUPABASE_ANON_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        logging.info("Supabase クライアントを初期化しました。")
    except Exception as e:
        logging.error(f"Supabase の初期化に失敗しました: {e}")
else:
    logging.info("Supabase URL または KEY が設定されていないため、Supabase は使用しません。")


############################################
# 3) ユーティリティ関連
############################################

def get_jp_weekday(d: datetime) -> str:
    """Pythonのdatetimeから日本語の曜日文字を返す (月, 火, 水, 木, 金, 土, 日)。"""
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    return weekdays[d.weekday()]

def convert_to_wav(input_path, output_path):
    """任意のフォーマット音声ファイルをWAVへ変換する。"""
    try:
        audio = AudioSegment.from_file(input_path)
        audio.export(output_path, format="wav")
        return True
    except Exception as e:
        logging.error(f"WAV変換エラー: {e}")
        return False

def transcribe_long_audio(file_path, language="ja-JP"):
    """
    Google API (SpeechRecognition) で文字起こし。
    長ければ 30秒ごとに分割し、オフセット＆リトライしながら認識する。
    """
    r = sr.Recognizer()
    r.dynamic_energy_threshold = True
    r.energy_threshold = 300
    max_retries = 3
    chunk_duration = 30.0

    try:
        audio_segment = AudioSegment.from_wav(file_path)
        total_duration_sec = len(audio_segment) / 1000.0
        logging.info(f"音声ファイルを読み込み: 全体 {total_duration_sec:.1f}秒")

        full_text = []
        current_offset = 0.0

        while current_offset < total_duration_sec:
            remaining = total_duration_sec - current_offset
            this_chunk = min(chunk_duration, remaining)
            logging.info(f"音声オフセット {current_offset:.1f}s から {this_chunk:.1f}s を認識")

            with sr.AudioFile(file_path) as source:
                audio_data = r.record(source, offset=current_offset, duration=this_chunk)

            recognized_chunk = ""
            for attempt in range(max_retries):
                try:
                    recognized_chunk = r.recognize_google(
                        audio_data,
                        language=language,
                        show_all=False
                    )
                    logging.info(f"チャンク認識成功: offset={current_offset:.1f}s => {len(recognized_chunk)}文字")
                    break
                except sr.RequestError as e:
                    logging.warning(f"【音声認識エラー:Request】 {e} / リトライ {attempt+1}/{max_retries}")
                    time.sleep(2)
                except sr.UnknownValueError:
                    logging.warning(f"【音声認識エラー:UnknownValue】offset={current_offset:.1f}s")
                    time.sleep(2)

            full_text.append(recognized_chunk)
            current_offset += this_chunk

        result_text = " ".join(t.strip() for t in full_text if t.strip())
        if result_text:
            logging.info(f"【音声認識完了】 取得文字数: {len(result_text)}")
            return result_text
        else:
            logging.info("【音声認識結果】すべて空でした。")
            return ""
    except Exception as e:
        logging.error(f"【音声認識エラー】予期せぬエラー: {e}")
        return ""

############################################
# 4) OpenAI APIを使った要約ユーティリティ
############################################

def chunk_text(text, max_chars=3000):
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append(text[start:end])
        start = end
    return chunks

def call_gpt_api(prompt_text, system_msg="", model="gpt-4o-mini", temperature=0.7, max_retries=5):
    if not client.api_key:
        logging.error("【GPT要約エラー】OpenAI APIキーが未設定です。")
        return ""

    messages = []
    if system_msg:
        messages.append({"role": "system", "content": system_msg})
    messages.append({"role": "user", "content": prompt_text})

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                timeout=60,
                max_tokens=1500,
            )
            result = response.choices[0].message.content.strip()
            logging.info(f"【call_gpt_api】要約結果の先頭100文字: {result[:100]!r}")
            return result

        except RateLimitError:
            wait_time = (attempt + 1) * 10
            logging.warning(f"【GPT要約】レートリミット到達。{wait_time}秒待機:リトライ({attempt+1}/{max_retries})")
            time.sleep(wait_time)
            continue
        except APIError as e:
            wait_time = (attempt + 1) * 3
            logging.warning(f"【GPT要約】APIエラー: {e}。{wait_time}秒待機:リトライ({attempt+1}/{max_retries})")
            time.sleep(wait_time)
            continue
        except Exception as e:
            logging.error(f"【GPT要約エラー】予期せぬエラー: {e}")
            if attempt < max_retries - 1:
                time.sleep(3)
                continue
            return ""

    logging.error(f"【GPT要約エラー】{max_retries}回リトライ失敗。")
    return ""

def summarize_text_in_chunks(entire_text):
    """
    議事録作成に向けたテキスト要約処理。
    修正点: 追加の議事録作成指示を埋め込み、要約時に考慮させる。
    """
    chunks = chunk_text(entire_text)
    summaries = []
    total_chunks = len(chunks)

    # === 議事録作成時の追加指示例 ===
    additional_instructions = """
【議事録作成時の指示】
1. 「以下の文字起こし文章を議事録風に作成してください。話し言葉を整理し、適宜箇条書きを用いてわかりやすくまとめること」
2. 可能な限り発言内容の重複を削除し、構成を整理する
3. 英語で考えて日本語で返答する
4. 社内共有文書のため、過度にくだけた表現や専門用語の省略は避ける
5. 話し手個人名がある場合、必要に応じて適宜省略または仮名化
6. 「以上が本日の議事録です」などの結びの言葉は不要
7. 「議事録」「日時」「出席者」などの見出しは不要
8. 箇条書きの記号は「・」を使用
"""

    for i, chunk in enumerate(chunks):
        logging.info(f"[詳細要約] チャンク {i+1}/{total_chunks} (文字数: {len(chunk)})")
        
        # 進捗状況を計算（パーセンテージ）
        progress = int((i / total_chunks) * 98)  # 98%まで
        
        # Server-Sent Events経由で進捗状況を送信
        if hasattr(g, 'progress_queue'):
            g.progress_queue.put({
                'progress': progress,
                'current': i + 1,
                'total': total_chunks,
                'estimated_time': (total_chunks - i - 1) * 30
            })

        # 基本プロンプト
        base_prompt = (
            "以下のテキストを議事録形式でまとめてください。"
            "発言順序を必要に応じて整理し、重複や冗長表現を削除してください。\n"
            "また、各項目が何に関するコメントかを明確にし、箇条書きを活用してください。\n\n"
        )

        # ここに追加指示を差し込んでマージ
        prompt = f"{additional_instructions}\n\n{base_prompt}\n\n【対象テキスト】\n{chunk}"

        summary = call_gpt_api(
            prompt_text=prompt,
            # system_msgを修正し、「議事録作成者としての詳細指示」を付加
            system_msg=(
                "あなたはプロの議事録作成者です。以下のガイドラインに厳密に従って回答してください。\n"
                "1) 発言の前後関係がわかるよう整理する\n"
                "2) 重複・冗長表現はなるべくまとめる\n"
                "3) 『英語で考えて日本語で返答』し、丁寧かつ簡潔な文章を心がける\n"
                "4) 上記additional_instructions内の指示も考慮する\n"
            ),
            model="gpt-4o-mini",
            temperature=0.5
        )
        summaries.append(summary)

    # 処理完了時に100%を送信
    if hasattr(g, 'progress_queue'):
        g.progress_queue.put({
            'progress': 100,
            'current': total_chunks,
            'total': total_chunks,
            'estimated_time': 0
        })

    return "\n".join(summaries)

############################################
# 5) Word への貼り付け & 保存
############################################
def paste_to_word_and_save(minutes_text: str, meeting_name: str = None, custom_path=None):
    word = None
    doc = None
    try:
        if not minutes_text.strip():
            logging.info("【Word保存】要約が空なのでスキップ。")
            return None

        # 保存先のパスを設定
        if not custom_path:
            download_dir = os.path.expanduser("~/Downloads")
            date_str = datetime.now().strftime("%Y%m%d")
            safe_meeting_name = (meeting_name or "議事録").replace(" ", "_").replace("/", "_")
            default_filename = f"{safe_meeting_name}_{date_str}.docx"
            save_path = os.path.join(download_dir, default_filename)
        else:
            save_path = custom_path

        logging.info(f"【Word保存】保存先: {save_path}")

        word = Dispatch("Word.Application")
        word.Visible = False  # バックグラウンドで実行

        doc = word.Documents.Add()
        # ページ設定（余白を狭く）
        doc.PageSetup.TopMargin = 20   # 上余白
        doc.PageSetup.BottomMargin = 20  # 下余白
        doc.PageSetup.LeftMargin = 20   # 左余白
        doc.PageSetup.RightMargin = 20  # 右余白

        sel = doc.ActiveWindow.Selection
        sel.Font.Name = "MS明朝"
        sel.Font.Size = 10.5  # フォントサイズを少し小さく

        # シンプルなヘッダーのみ追加
        sel.TypeText(f"会議名：{meeting_name or '[会議名]'}\n")
        sel.TypeText(f"作成日：{datetime.now().strftime('%Y年%m月%d日')}\n\n")

        # 不要な文字や見出しを削除
        cleaned_text = (minutes_text
            .replace("###", "")
            .replace("#", "")
            .replace("以上が本日の議事録です。", "")
            .replace("各自、提起された事項についての対応をよろしくお願いいたします。", "")
            .replace("各自、今後の対応についてご留意ください。", "")
            .replace("議事録", "")
            .replace("日時: [日付を挿入]", "")
            .replace("出席者: [出席者名を挿入]", "")
            .strip())

        sel.TypeText(cleaned_text)

        # ファイルを保存
        doc.SaveAs2(save_path)
        doc.Close()
        
        logging.info(f"【Word保存完了】ファイルパス: {save_path}")
        return save_path

    except Exception as e:
        logging.error(f"【Word貼り付けエラー】 {e}")
        return None
        
    finally:
        if doc:
            try:
                doc.Close(SaveChanges=False)
            except:
                pass
        if word:
            try:
                word.Quit()
            except:
                pass

def open_save_dialog() -> str:
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.asksaveasfilename(
            defaultextension=".docx",
            filetypes=[("Word Documents", "*.docx"), ("All Files", "*.*")]
        )
        root.destroy()
        logging.info(f"【ダイアログ】保存先: {path}")
        return path
    except Exception as e:
        logging.error(f"【ダイアログエラー】 {e}")
        return ""

############################################
# 6) Flask ルート & ログイン周り
############################################

class User(UserMixin):
    """
    Supabase Authのユーザーは「UID（例: UUID）」が一意のIDになります。
    ここではそれを self.id に設定します。
    """
    def __init__(self, user_id, email):
        self.id = user_id   # UserMixinが参照する「ユーザーID」
        self.email = email  # 参考: ユーザーメールアドレス

@login_manager.user_loader
def load_user(user_id):
    """セッションからユーザー情報を取得"""
    if not user_id:
        return None
    
    # まずセッションから確認
    if 'user_id' in session and str(session['user_id']) == str(user_id):
        return User(user_id, session.get('email'))
    
    try:
        # Supabaseからユーザー情報を取得
        if supabase:
            response = supabase.auth.get_user()
            logging.info(f"Supabase user response: {response}")
            
            if response and hasattr(response, 'user'):
                user_data = response.user
                if str(user_data.id) == str(user_id):
                    return User(user_id, user_data.email)
    except Exception as e:
        logging.error(f"ユーザー情報取得エラー: {e}")
    
    return None

@app.route('/login', methods=['GET', 'POST'])
def login():
    # すでにログインしている場合はリダイレクト
    if current_user.is_authenticated:
        return redirect(url_for('minutes_list'))

    if request.method == 'POST':
        email = request.form['username']
        password = request.form['password']

        if not supabase:
            flash("認証システムに接続できません。")
            return render_template('login.html')

        try:
            result = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })
            
            logging.info(f"Login response: {result}")
            
            user_data = result.user
            user_id = user_data.id
            user_email = user_data.email

            user_obj = User(user_id, user_email)
            login_user(user_obj, remember=True)

            session.permanent = True
            session['user_id'] = user_id
            session['email'] = user_email
            
            session.modified = True

            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('minutes_list'))

        except Exception as e:
            logging.error(f"【ログインエラー】: {e}")
            flash("ログインできません。メールアドレスとパスワードを確認してください。")
            return render_template('login.html')

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
def index():
    return render_template('index.html')

def format_time(seconds):
    if seconds < 60:
        return f"{seconds:.1f}秒"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}分"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}時間"

@app.route('/submit_audio', methods=['POST'])
@login_required
def submit_audio():
    try:
        # 進捗状況をリセット
        if hasattr(g, 'progress_queue'):
            g.progress_queue.put({
                'progress': 0,
                'current': 0,
                'total': 1,
                'estimated_time': 0
            })

        start_time = time.time()
        meeting_name = request.form.get('meetingName', '未入力')
        create_name = request.form.get('createName', '未入力')

        session['meeting_name'] = meeting_name
        session['create_name'] = create_name
        now_dt = datetime.now()
        session['timestamp'] = now_dt.strftime("%Y-%m-%d")

        audio_file = request.files.get('audioFile')
        if not audio_file:
            return "音声ファイルがありません。"

        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        audio_path = os.path.join(UPLOAD_FOLDER, f"{stamp}_{audio_file.filename}")
        audio_file.save(audio_path)

        wav_path = os.path.join(UPLOAD_FOLDER, f"{stamp}.wav")
        success = convert_to_wav(audio_path, wav_path)
        if not success:
            return "音声ファイルの変換に失敗。"

        st_trans = time.time()
        text_result = transcribe_long_audio(wav_path, language="ja-JP")
        ed_trans = time.time()
        transcribe_time = ed_trans - st_trans

        session['processing'] = True
        
        st_sum = time.time()
        minutes_result = summarize_text_in_chunks(text_result)
        ed_sum = time.time()
        summary_time = ed_sum - st_sum

        time.sleep(2)

        total_time = time.time() - start_time

        session['text_result'] = text_result
        session['minutes_result'] = minutes_result
        session['transcribe_time_str'] = format_time(transcribe_time)
        session['summary_time_str'] = format_time(summary_time)
        session['total_time_str'] = format_time(total_time)

        # Supabaseへの保存
        try:
            supabase.table("minutes_log").insert({
                "text_content": text_result,
                "minutes_result": minutes_result,
                "meeting_date": session['timestamp'],
                "timestamp": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "meeting_name": session['meeting_name'],
                "create_name": session['create_name']
            }).execute()
            logging.info("Supabase: minutes_log に音声議事録を保存しました。")
        except Exception as e:
            logging.warning(f"Supabaseへの書き込み失敗: {e}")

        return redirect(url_for('result'))

    except Exception as e:
        session['processing'] = False
        logging.error(f"【音声処理エラー】 {e}")
        if hasattr(g, 'progress_queue'):
            g.progress_queue.put({'error': True})
        flash(f'エラーが発生しました：{str(e)}')
        return redirect(url_for('index'))

@app.route('/submit_text', methods=['POST'])
@login_required
def submit_text():
    try:
        if hasattr(g, 'progress_queue'):
            g.progress_queue.put({
                'progress': 0,
                'current': 0,
                'total': 1,
                'estimated_time': 0
            })

        start_time = time.time()
        meeting_name = request.form.get('meetingName', '未入力')
        create_name = request.form.get('createName', '未入力')

        now_dt = datetime.now()
        session['meeting_name'] = meeting_name
        session['create_name'] = create_name
        session['timestamp'] = now_dt.strftime("%Y-%m-%d")

        text_file = request.files.get('textFile')
        if not text_file:
            return "テキストファイルがありません。"

        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        text_path = os.path.join(UPLOAD_FOLDER, f"{stamp}_{text_file.filename}")
        text_file.save(text_path)

        with open(text_path, 'rb') as f:
            raw_data = f.read()
        detected = chardet.detect(raw_data)
        guessed_enc = detected["encoding"] or "cp932"

        try:
            with open(text_path, 'r', encoding=guessed_enc, errors='replace') as f:
                text = f.read()
        except:
            with open(text_path, 'r', encoding='cp932', errors='replace') as f:
                text = f.read()

        session['transcribe_time_str'] = "N/A"
        session['processing'] = True

        st_sum = time.time()
        minutes_result = summarize_text_in_chunks(text)
        ed_sum = time.time()
        summary_time = ed_sum - st_sum

        time.sleep(2)

        total_time = time.time() - start_time

        session['text_result'] = text
        session['minutes_result'] = minutes_result
        session['summary_time_str'] = format_time(summary_time)
        session['total_time_str'] = format_time(total_time)

        try:
            supabase.table("minutes_log").insert({
                "text_content": text,
                "minutes_result": minutes_result,
                "meeting_date": session['timestamp'],
                "timestamp": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "meeting_name": session['meeting_name'],
                "create_name": session['create_name']
            }).execute()
            logging.info("Supabase: minutes_log にテキスト議事録を保存しました。")
        except Exception as e:
            logging.warning(f"Supabaseへの書き込み失敗: {e}")

        return redirect(url_for('result'))

    except Exception as e:
        session['processing'] = False
        logging.error(f"【テキスト処理エラー】 {e}")
        if hasattr(g, 'progress_queue'):
            g.progress_queue.put({'error': True})
        flash(f'エラーが発生しました：{str(e)}')
        return redirect(url_for('index'))

@app.route('/custom_save', methods=['POST'])
def custom_save():
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                'success': False,
                'error': 'データが送信されていません'
            }), 400

        text = data.get('text', '').strip()
        meeting_name = data.get('meeting_name', '').strip()
        
        if not text:
            return jsonify({
                'success': False,
                'error': '保存するテキストが空です'
            }), 400

        # Word文書の作成
        doc = Document()
        
        # フォント設定
        style = doc.styles['Normal']
        style.font.name = 'MS明朝'
        style.font.size = Pt(10.5)
        
        # ページ設定（A4サイズ、余白を狭く）
        section = doc.sections[0]
        section.page_width = Pt(595.3)  # A4幅
        section.page_height = Pt(841.9)  # A4高さ
        section.left_margin = Pt(20)
        section.right_margin = Pt(20)
        section.top_margin = Pt(20)
        section.bottom_margin = Pt(20)

        # ヘッダー情報の追加
        doc.add_paragraph(f"会議名：{meeting_name}")
        doc.add_paragraph(f"作成日：{datetime.now().strftime('%Y年%m月%d日')}\n")

        # 本文の追加
        doc.add_paragraph(text)
        
        # 保存先のパスを設定
        downloads_path = os.path.join(os.path.expanduser('~'), 'Downloads')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_meeting_name = re.sub(r'[<>:"/\\|?*]', '', meeting_name) or '議事録'
        filename = f"{safe_meeting_name}_{timestamp}.docx"
        save_path = os.path.join(downloads_path, filename)
        
        # フォルダが存在しない場合は作成
        os.makedirs(downloads_path, exist_ok=True)
        
        # 保存
        doc.save(save_path)
        
        return jsonify({
            'success': True,
            'path': save_path,
            'message': 'ファイルを保存しました'
        })
            
    except Exception as e:
        logging.error(f"Word保存エラー: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/check_auth')
def check_auth():
    """認証状態をチェックするエンドポイント"""
    try:
        is_authenticated = current_user.is_authenticated
        if is_authenticated and hasattr(current_user, 'email'):
            return jsonify({
                'authenticated': True,
                'user_email': current_user.email
            })
        return jsonify({'authenticated': False})
    except Exception as e:
        logging.error(f"認証チェックエラー: {e}")
        return jsonify({'authenticated': False})

@app.route('/minutes_list')
@login_required
def minutes_list():
    if not current_user.is_authenticated:
        return redirect(url_for('login', next=url_for('minutes_list')))
        
    if not supabase:
        return "Supabaseが初期化されていません。"

    try:
        response = supabase.table("minutes_log").select("*").execute()
        data = response.data
        return render_template("minutes_list.html", minutes_data=data)
    except Exception as e:
        logging.error(f"【minutes_list エラー】 {e}")
        return f"エラー: {str(e)}"

@app.route('/minutes_detail/<id>')
def minutes_detail(id):
    if not supabase:
        return {"error": "Supabaseが初期化されていません。"}, 500

    try:
        response = supabase.table("minutes_log").select("*").eq("id", id).execute()
        if not response.data:
            return {"error": "指定された議事録が見つかりません。"}, 404
        return response.data[0]
    except Exception as e:
        logging.error(f"【minutes_detail エラー】 {e}")
        return {"error": str(e)}, 500

@app.route('/result')
@login_required
def result():
    text_result = session.get('text_result', '')
    minutes_result = session.get('minutes_result', '')
    transcribe_time_str = session.get('transcribe_time_str', 'N/A')
    summary_time_str = session.get('summary_time_str', 'N/A')
    total_time_str = session.get('total_time_str', 'N/A')

    if not session.get('timestamp'):
        session['timestamp'] = datetime.now().strftime("%Y-%m-%d")

    return render_template('result.html',
                         text_result=text_result,
                         minutes_result=minutes_result,
                         transcribe_time_str=transcribe_time_str,
                         summary_time_str=summary_time_str,
                         total_time_str=total_time_str)

@app.route('/enter_meeting_name', methods=['GET', 'POST'])
def enter_meeting_name():
    if request.method == 'POST':
        meeting_name = request.form.get('meetingName', '未入力')
        session['meeting_name'] = meeting_name
        return redirect(url_for('result'))
    return render_template('enter_meeting_name.html')

def open_browser():
    time.sleep(1)
    webbrowser.open_new("http://127.0.0.1:5000")
    time.sleep(1)
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    except:
        pass

@app.route('/clear_session')
def clear_session():
    session.pop('minutes_result', None)
    session.pop('text_result', None)
    session.pop('transcribe_time_str', None)
    session.pop('summary_time_str', None)
    session.pop('total_time_str', None)
    return redirect(url_for('index'))

@app.route('/progress')
def progress():
    def generate():
        progress_queue = Queue()
        g.progress_queue = progress_queue
        
        try:
            while True:
                progress_data = progress_queue.get()
                if 'error' in progress_data:
                    yield f"data: {json.dumps({'error': progress_data['error']})}\n\n"
                    break
                yield f"data: {json.dumps(progress_data)}\n\n"
        except GeneratorExit:
            if hasattr(g, 'progress_queue'):
                del g.progress_queue
    
    return Response(generate(), mimetype='text/event-stream')

def cleanup_uploads(max_age_hours=24):
    """
    uploadsフォルダ内の古いファイルを削除する
    max_age_hours: 保持する最大時間（デフォルト24時間）
    """
    try:
        current_time = datetime.now()
        upload_dir = os.path.join(os.getcwd(), UPLOAD_FOLDER)
        
        for filename in os.listdir(upload_dir):
            file_path = os.path.join(upload_dir, filename)
            # ファイルの最終更新時刻を取得
            file_modified = datetime.fromtimestamp(os.path.getmtime(file_path))
            age_hours = (current_time - file_modified).total_seconds() / 3600
            
            # 指定時間より古いファイルを削除
            if age_hours > max_age_hours:
                try:
                    os.remove(file_path)
                    logging.info(f"古いファイルを削除: {filename}")
                except Exception as e:
                    logging.error(f"ファイル削除エラー {filename}: {e}")
    except Exception as e:
        logging.error(f"クリーンアップエラー: {e}")

def init_app(app):
    """アプリケーションの初期化処理"""
    with app.app_context():
        cleanup_uploads()

if __name__ == '__main__':
    logging.info("Flaskサーバ起動。")
    init_app(app)  # 初期化処理を実行
    t = threading.Thread(target=open_browser)
    t.start()

    app.run(debug=True, use_reloader=False, host='127.0.0.1', port=5000, threaded=True)
