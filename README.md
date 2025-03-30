# Discord録音ボット

Discordの音声チャンネルでの会話を自動的に録音し、MP3ファイルとして保存するボットです。10分ごとに録音ファイルを区切るため、長時間の録音でも管理しやすくなっています。

## 機能

- Discordの音声チャンネルの録音
- 10分ごとの自動ファイル分割
- MP3形式での保存（容量削減）
- 接続切断時の自動再接続
- 全員退出時の自動停止

## 必要条件

- Python 3.8以上
- FFmpeg（MP3変換に必要）
- Discordボットトークン

## インストール方法

1. リポジトリをクローン
```bash
git clone https://github.com/yourusername/discord-recorder.git
cd discord-recorder