# main.py
import yaml
import os
import logging
from http_server import start_http_server
from data_manager import ReplayDataManager
from viewer import ReplayViewer

# ロガーの設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Panda3Dのログレベル設定 (デバッグ情報が多すぎる場合はWARN以上にする)
# import direct.showbase.ShowBase
# direct.showbase.ShowBase.ShowBase().notify.setInfo(False) # or setWarning, setError

def load_config(config_path="config.yml"):
    """
    設定ファイルをロードします。
    """
    if not os.path.exists(config_path):
        logger.error(f"設定ファイルが見つかりません: {config_path}")
        # デフォルト値を返すか、エラーで終了
        return {
            'server': {'port': 8000},
            'model_paths': {'base_dir': 'models'}
        }
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    logger.info(f"設定ファイルをロードしました: {config_path}")
    return config

def main():
    config = load_config()

    # モデルディレクトリの存在確認と作成
    model_base_dir = config['model_paths']['base_dir']
    if not os.path.exists(model_base_dir):
        os.makedirs(model_base_dir)
        logger.info(f"モデルディレクトリを作成しました: {model_base_dir}")

    # DataManagerの初期化
    data_manager = ReplayDataManager()

    # HTTPサーバーの起動
    # DataManagerのパースメソッドをコールバックとして渡す
    http_server_instance = start_http_server(config['server']['port'], data_manager.parse_and_store_data)

    # Viewerの初期化とPanda3Dアプリケーションの実行
    app = ReplayViewer(data_manager, model_base_dir=model_base_dir)

    # Panda3Dのタスクマネージャーに追加
    # UIイベントや追加機能のバインド
    app.accept("space", data_manager.toggle_play_pause)
    app.accept("arrow_left", data_manager.prev_frame) # 1フレーム戻る
    app.accept("arrow_right", data_manager.next_frame) # 1フレーム進む
    app.accept("page_up", lambda: data_manager.fast_forward(60)) # 60フレーム早送り
    app.accept("page_down", lambda: data_manager.rewind(60)) # 60フレーム巻き戻し
    
    # 保存/ロードのキーバインド (例: Ctrl+S, Ctrl+L)
    app.accept("control-s", lambda: data_manager.save_replay("replay.msgpack"))
    app.accept("control-l", lambda: data_manager.load_replay("replay.msgpack"))
    app.accept("c", data_manager.clear_replay_data) # 'c'キーでデータをクリア

    # アプリケーション開始時に自動的に再生を開始
    data_manager.play()

    # アプリケーションの実行
    logger.info("Panda3Dアプリケーションを開始します。")
    app.run()

    # アプリケーション終了時にHTTPサーバーをシャットダウン
    if http_server_instance:
        logger.info("HTTPサーバーをシャットダウン中...")
        http_server_instance.shutdown()
        http_server_instance.server_close()
        logger.info("HTTPサーバーがシャットダウンされました。")

if __name__ == '__main__':
    main()
