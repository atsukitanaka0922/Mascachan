"""
Discord録音ボット

Discordの音声チャンネルでの会話を自動的に録音し、MP3ファイルとして保存します。
10分ごとに録音ファイルを区切るため、長時間の録音でも管理しやすくなっています。
"""

import discord
import asyncio
import os
import datetime
import wave
import logging
import subprocess
import tempfile
from discord.ext import commands, tasks
from config import TOKEN, COMMAND_PREFIX, RECORDING_LENGTH, SAMPLE_RATE, CHANNELS

# ロガーの設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("discord-recorder")

# スクリプトのディレクトリを取得（絶対パス用）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(SCRIPT_DIR, "recordings")

# FFmpegへのパスを設定（ローカルにある場合）
FFMPEG_PATH = os.path.join(SCRIPT_DIR, "ffmpeg.exe")

# ボットのインテント設定
intents = discord.Intents.all()  # すべてのインテントを有効化
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# 録音セッション情報を保持する辞書
recording_sessions = {}

@bot.event
async def on_ready():
    logger.info(f'{bot.user} としてログインしました')
    logger.info(f'インテント設定: {bot.intents}')
    check_voice_connections.start()
    logger.info('監視ループを開始しました')
    print('------')

@tasks.loop(seconds=60)
async def check_voice_connections():
    """音声接続の状態を定期的に確認"""
    for guild_id, session in list(recording_sessions.items()):
        if not session["voice_client"].is_connected():
            try:
                # 再接続を試みる
                channel = session["voice_client"].channel
                session["voice_client"] = await channel.connect()
                logger.info(f"{channel.name}に再接続しました")
            except Exception as e:
                logger.error(f"再接続に失敗しました: {e}")
                # 再接続に失敗した場合はセッションを終了
                session["running"] = False
                if guild_id in recording_sessions:
                    del recording_sessions[guild_id]

@bot.event
async def on_error(event, *args, **kwargs):
    """エラーハンドリング"""
    with open('err.log', 'a') as f:
        if event == 'on_message':
            f.write(f'Unhandled message: {args[0]}\n')
        else:
            f.write(f'Unhandled event: {event}\n')
    logger.error(f'An error occurred in {event}')

@bot.event
async def on_command_error(ctx, error):
    """コマンドエラーのハンドリング"""
    if isinstance(error, commands.CommandInvokeError):
        logger.error(f'コマンド実行エラー: {error.original}')
        await ctx.send(f"エラーが発生しました: {error.original}")
    else:
        logger.error(f'コマンドエラー: {error}')
        await ctx.send(f"コマンドエラー: {error}")

@bot.event
async def on_voice_state_update(member, before, after):
    """音声状態の変更を監視して、切断された場合に対応する"""
    if before.channel is not None and after.channel is None:
        # ユーザーが切断した場合
        for guild_id, session in list(recording_sessions.items()):
            if session["voice_client"].channel == before.channel:
                if len(before.channel.members) <= 1:  # ボットだけが残った場合
                    logger.info(f"全員が退出したため、{before.channel.name}での録音を停止します")
                    session["running"] = False
                    try:
                        session["voice_client"].stop_recording()
                        await session["voice_client"].disconnect()
                    except Exception as e:
                        logger.error(f"切断中にエラーが発生しました: {e}")
                    
                    if guild_id in recording_sessions:
                        del recording_sessions[guild_id]

@bot.command(name='record')
async def record(ctx):
    """音声チャンネルの録音を開始します"""
    try:
        # ユーザーが音声チャンネルに接続しているか確認
        if ctx.author.voice is None:
            await ctx.send("音声チャンネルに接続してから実行してください。")
            return
            
        voice_channel = ctx.author.voice.channel
        
        # 既に録音中かチェック
        if ctx.guild.id in recording_sessions:
            await ctx.send("既に録音中です。`!stop`で録音を停止できます。")
            return
        
        # 録音用ディレクトリの作成
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = os.path.join(RECORDINGS_DIR, f"{ctx.guild.id}_{timestamp}")
        os.makedirs(session_dir, exist_ok=True)
        
        # 音声チャンネルに接続
        voice_client = await voice_channel.connect()
        
        # 録音セッション情報を保存
        recording_sessions[ctx.guild.id] = {
            "voice_client": voice_client,
            "session_dir": session_dir,
            "segment": 1,
            "running": True
        }
        
        # 保存先情報も含めたメッセージを表示
        await ctx.send(f"{voice_channel.name} での録音を開始しました。\n"
                       f"10分ごとにファイルを分割して保存します。\n"
                       f"保存先: {session_dir}")
        
        logger.info(f"{ctx.guild.name}の{voice_channel.name}で録音を開始しました")
        logger.info(f"チャンネルユーザー数: {len(voice_channel.members)}")
        
        # 録音ループを開始
        asyncio.create_task(recording_loop(ctx))
    
    except Exception as e:
        logger.error(f"録音開始中にエラーが発生しました: {e}")
        await ctx.send(f"録音開始中にエラーが発生しました: {e}")

async def recording_loop(ctx):
    """10分ごとに録音を区切るループ処理"""
    try:
        guild_id = ctx.guild.id
        
        while guild_id in recording_sessions:
            session = recording_sessions[guild_id]
            
            if not session["running"]:
                break
                
            segment = session["segment"]
            # ファイル名をMP3に変更
            filename = f"{session['session_dir']}/segment_{segment}.mp3"
            
            # 録音シンクの準備
            sink = discord.sinks.WaveSink()
            
            try:
                session["voice_client"].start_recording(
                    sink,
                    finished_callback,
                    ctx
                )
                
                # Discord接続の状態を確認
                logger.info(f"音声接続状態: 接続={session['voice_client'].is_connected()}, 再生中={session['voice_client'].is_playing()}")
                logger.info(f"音声チャンネルのユーザー数: {len(session['voice_client'].channel.members)}")
                
                # 指定時間待機
                logger.info(f"{ctx.guild.name}: セグメント{segment}の録音を開始しました")
                
                # 指定時間待機しながら、一定間隔で接続状態を確認
                for _ in range(RECORDING_LENGTH // 10):
                    if not (guild_id in recording_sessions and 
                            recording_sessions[guild_id]["running"] and 
                            recording_sessions[guild_id]["voice_client"].is_connected()):
                        logger.warning(f"{ctx.guild.name}: 録音が中断されました")
                        break
                    await asyncio.sleep(10)
                
                if not (guild_id in recording_sessions and recording_sessions[guild_id]["running"]):
                    break
                    
                # 録音を停止して保存
                if guild_id in recording_sessions and recording_sessions[guild_id]["voice_client"].is_connected():
                    session["voice_client"].stop_recording()
                    success = await save_recording_as_mp3(sink, filename)
                    
                    if success:
                        session["segment"] += 1
                        await ctx.send(f"セグメント {segment} を保存しました。\n保存先: {filename}")
                        logger.info(f"{ctx.guild.name}: セグメント{segment}を保存しました")
                    else:
                        await ctx.send(f"セグメント {segment} の保存に失敗しました。ログを確認してください。")
                        logger.error(f"{ctx.guild.name}: セグメント{segment}の保存に失敗")
            
            except Exception as e:
                logger.error(f"録音ループ中にエラーが発生しました: {e}")
                if guild_id in recording_sessions:
                    try:
                        recording_sessions[guild_id]["voice_client"].stop_recording()
                    except:
                        pass
                await ctx.send(f"録音中にエラーが発生しました。再試行します。")
                await asyncio.sleep(2)  # 少し待機してから再試行
    
    except Exception as e:
        logger.error(f"録音ループ全体でエラーが発生しました: {e}")
        await ctx.send(f"録音が中断されました: {e}")

async def finished_callback(sink, ctx):
    """録音完了時のコールバック関数"""
    # この関数は自動停止時に呼ばれない（手動停止時のみ）
    logger.info(f"{ctx.guild.name}: finished_callbackが呼び出されました")
    pass

async def save_recording_as_mp3(sink, filename):
    """録音データをMP3ファイルとして保存する"""
    try:
        # 音声データの確認と詳細ログ
        logger.info(f"録音ユーザー数: {len(sink.audio_data)}")
        total_size = 0
        
        for user_id, audio in sink.audio_data.items():
            audio.file.seek(0, os.SEEK_END)
            size = audio.file.tell()
            audio.file.seek(0)
            total_size += size
            logger.info(f"ユーザーID {user_id} の録音サイズ: {size} バイト")
        
        logger.info(f"合計録音サイズ: {total_size} バイト")
        
        if not sink.audio_data:
            logger.warning("録音データが空です！")
            return False
            
        # 録音サイズが小さすぎる場合は処理しない
        if total_size < 1000:  # 1KB未満
            logger.warning(f"録音サイズが小さすぎます ({total_size} バイト)。処理をスキップします。")
            return False
        
        # デバッグディレクトリ
        debug_dir = os.path.join(RECORDINGS_DIR, "debug")
        os.makedirs(debug_dir, exist_ok=True)
        
        # 一時WAVファイル（デバッグ用に保存）
        debug_wav = os.path.join(debug_dir, f"debug_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.wav")
        
        # 一時ディレクトリを使用
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_wav = os.path.join(temp_dir, "temp_recording.wav")
            
            # WAVファイルの準備
            logger.info(f"一時WAVファイルを作成: {temp_wav}")
            
            # WAVファイルを作成
            with wave.open(temp_wav, 'wb') as wav_file:
                wav_file.setnchannels(CHANNELS)
                wav_file.setsampwidth(2)  # 16-bit PCM
                wav_file.setframerate(SAMPLE_RATE)
                
                # すべてのユーザーの音声データを処理
                combined_audio = bytearray()
                for user_id, audio in sink.audio_data.items():
                    combined_audio.extend(audio.file.read())
                    audio.file.seek(0)  # ファイルポインタをリセット
                
                wav_file.writeframes(combined_audio)
            
            # デバッグ用にもWAVファイルを保存
            with wave.open(debug_wav, 'wb') as wav_file:
                wav_file.setnchannels(CHANNELS)
                wav_file.setsampwidth(2)  # 16-bit PCM
                wav_file.setframerate(SAMPLE_RATE)
                
                # 全ユーザーの音声を再度読み込み
                combined_audio = bytearray()
                for user_id, audio in sink.audio_data.items():
                    combined_audio.extend(audio.file.read())
                    audio.file.seek(0)  # ファイルポインタをリセット
                
                wav_file.writeframes(combined_audio)
            
            logger.info(f"デバッグ用WAVファイルを保存: {debug_wav}")
            
            # ファイルが正常に作成されたか確認
            if not os.path.exists(temp_wav) or os.path.getsize(temp_wav) < 1000:
                logger.error(f"WAVファイルの作成に失敗または小さすぎます: {temp_wav}")
                return False
                
            # FFmpegコマンドの準備
            # ローカルにあればそのパスを使用、なければシステムのffmpegを使用
            ffmpeg_exec = FFMPEG_PATH if os.path.exists(FFMPEG_PATH) else 'ffmpeg'
            
            ffmpeg_cmd = [
                ffmpeg_exec, 
                '-i', temp_wav, 
                '-codec:a', 'libmp3lame', 
                '-qscale:a', '2',  # 品質設定 (0-9, 0が最高品質)
                '-y',  # 既存ファイルを上書き
                filename
            ]
            
            logger.info(f"実行するFFmpegコマンド: {' '.join(ffmpeg_cmd)}")
            
            try:
                # FFmpegを実行して詳細なエラー出力を取得
                process = subprocess.run(
                    ffmpeg_cmd, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE,
                    check=True  # エラー時に例外を発生させる
                )
                logger.info("FFmpeg変換成功")
            except subprocess.CalledProcessError as e:
                error_output = e.stderr.decode() if e.stderr else "不明なエラー"
                logger.error(f"FFmpeg変換エラー: {error_output}")
                
                # 失敗した場合はデバッグ用WAVファイルをMP3の代わりに使用
                import shutil
                mp3_dir = os.path.dirname(filename)
                wav_filename = os.path.join(mp3_dir, os.path.basename(filename).replace('.mp3', '.wav'))
                shutil.copy(debug_wav, wav_filename)
                logger.info(f"MP3変換に失敗したため、WAVファイルを保存: {wav_filename}")
                return True
            
            # ファイル確認
            if os.path.exists(filename):
                file_size = os.path.getsize(filename)
                logger.info(f"MP3ファイル {filename} を保存しました。サイズ: {file_size} バイト")
                return True
            else:
                logger.error(f"MP3ファイル {filename} が作成されませんでした")
                return False
        
    except Exception as e:
        logger.error(f"録音保存・MP3変換中にエラーが発生しました: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

@bot.command(name='stop')
async def stop_recording(ctx):
    """録音を停止します"""
    try:
        guild_id = ctx.guild.id
        
        if guild_id not in recording_sessions:
            await ctx.send("現在録音していません。")
            return
        
        session = recording_sessions[guild_id]
        session["running"] = False
        
        # 最後のセグメントを保存
        last_segment = session["segment"]
        last_filename = f"{session['session_dir']}/segment_{last_segment}.mp3"
        try:
            session["voice_client"].stop_recording()
            # 最後のセグメントも保存
            sink = session["voice_client"].sink
            if sink:
                success = await save_recording_as_mp3(sink, last_filename)
                if success:
                    await ctx.send(f"最終セグメント {last_segment} を保存しました。\n保存先: {last_filename}")
                else:
                    await ctx.send(f"最終セグメントの保存に失敗しました。ログを確認してください。")
        except Exception as e:
            logger.error(f"録音停止中にエラーが発生しました: {e}")
        
        # 切断
        try:
            await session["voice_client"].disconnect()
        except Exception as e:
            logger.error(f"切断中にエラーが発生しました: {e}")
        
        # セッション情報をクリア
        del recording_sessions[guild_id]
        
        await ctx.send("録音を停止しました。")
        logger.info(f"{ctx.guild.name}の録音を停止しました")
        
    except Exception as e:
        logger.error(f"録音停止処理中にエラーが発生しました: {e}")
        await ctx.send(f"録音停止中にエラーが発生しました: {e}")

@bot.command(name='status')
async def status(ctx):
    """現在の録音状態を表示します"""
    try:
        if ctx.guild.id in recording_sessions:
            session = recording_sessions[ctx.guild.id]
            voice_channel = session["voice_client"].channel
            segment = session["segment"]
            session_dir = session["session_dir"]
            
            # 接続中のユーザー情報を取得
            members = voice_channel.members
            member_names = [member.display_name for member in members if not member.bot]
            
            await ctx.send(f"現在の録音状況:\n"
                          f"チャンネル: {voice_channel.name}\n"
                          f"現在のセグメント: {segment}\n"
                          f"保存先: {session_dir}\n"
                          f"参加者: {', '.join(member_names)}")
        else:
            await ctx.send("現在録音していません。")
    except Exception as e:
        logger.error(f"状態確認中にエラーが発生しました: {e}")
        await ctx.send(f"状態確認中にエラーが発生しました: {e}")

@bot.command(name='test_record')
async def test_record(ctx):
    """短い録音テストを実行します（30秒）"""
    try:
        if ctx.author.voice is None:
            await ctx.send("音声チャンネルに接続してから実行してください。")
            return
            
        voice_channel = ctx.author.voice.channel
        await ctx.send(f"{voice_channel.name} でテスト録音を開始します（30秒）")
        
        # チャンネルのメンバー確認
        members = voice_channel.members
        member_names = [member.display_name for member in members if not member.bot]
        await ctx.send(f"録音対象ユーザー: {', '.join(member_names)}")
        
        # テスト用ディレクトリ
        test_dir = os.path.join(RECORDINGS_DIR, "test")
        os.makedirs(test_dir, exist_ok=True)
        test_file = os.path.join(test_dir, f"test_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3")
        
        # 接続して録音
        voice_client = await voice_channel.connect()
        sink = discord.sinks.WaveSink()
        voice_client.start_recording(sink, finished_callback, ctx)
        
        await ctx.send("録音中... 30秒お待ちください")
        await asyncio.sleep(30)
        
        # 録音停止と保存
        voice_client.stop_recording()
        success = await save_recording_as_mp3(sink, test_file)
        await voice_client.disconnect()
        
        # 結果確認
        if success and os.path.exists(test_file):
            size = os.path.getsize(test_file)
            await ctx.send(f"テスト録音完了！ファイルサイズ: {size} バイト\n保存先: {test_file}")
            
            # 録音ユーザー数の報告
            if sink.audio_data:
                user_count = len(sink.audio_data)
                await ctx.send(f"録音されたユーザー数: {user_count}")
                
                # 各ユーザーのデータサイズを報告
                for user_id, audio in sink.audio_data.items():
                    audio.file.seek(0, os.SEEK_END)
                    size = audio.file.tell()
                    audio.file.seek(0)
                    user = ctx.guild.get_member(int(user_id))
                    user_name = user.display_name if user else f"不明なユーザー({user_id})"
                    await ctx.send(f"- {user_name}: {size} バイト")
            else:
                await ctx.send("音声データが取得できませんでした。音声が出ていることを確認してください。")
        else:
            await ctx.send("テスト録音に失敗しました。ログを確認してください。")
        
    except Exception as e:
        logger.error(f"テスト録音中にエラーが発生しました: {e}")
        await ctx.send(f"テスト録音中にエラーが発生しました: {e}")

# ボットを実行
if __name__ == "__main__":
    # recordingsディレクトリの作成
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    logger.info("ボットを起動しています...")
    
    # FFmpegが利用可能か確認
    try:
        ffmpeg_command = FFMPEG_PATH if os.path.exists(FFMPEG_PATH) else 'ffmpeg'
        subprocess.run([ffmpeg_command, '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        logger.info("FFmpegが利用可能です")
    except Exception as e:
        logger.warning(f"FFmpegが見つかりません。MP3変換ができない可能性があります: {e}")
        print("警告: FFmpegが見つかりません。MP3変換には FFmpeg のインストールが必要です。")
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.critical(f"ボットの起動に失敗しました: {e}")