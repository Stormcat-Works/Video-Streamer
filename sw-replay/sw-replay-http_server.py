import http.server
import socketserver
import urllib.parse
import threading
import logging
import asyncio
import time # timeモジュールを追加
from typing import cast

# ロガーの設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ReplayDataHandler(http.server.BaseHTTPRequestHandler):
    """
    Stormworksからのリプレイデータを受信するHTTPリクエストハンドラ
    """
    def do_GET(self):
        """
        GETリクエストを処理し、URLからデータを受信します。
        """
        # /replay パスのみを処理
        if not self.path.split('?')[0].startswith('/replay'):
            self.send_response(404)
            self.end_headers()
            return

        query = urllib.parse.urlparse(self.path).query
        
        server_instance = cast("ReplayHTTPServer", self.server)

        # リクエスト数をカウントし、タイムスタンプを記録
        with server_instance.request_lock:
            now = time.time()
            server_instance.request_timestamps.append(now)
            # 直近1秒以内のリクエストのみを保持
            server_instance.request_timestamps = [ts for ts in server_instance.request_timestamps if now - ts <= 1.0]
            requests_in_last_second = len(server_instance.request_timestamps)

        # データマネージャーにデータを渡すためのコールバック関数を呼び出す
        if hasattr(server_instance, 'data_callback') and server_instance.data_callback:
            # 非同期でデータを処理するために別スレッドでコールバックを実行
            # (HTTPサーバーの応答をブロックしないため)
            threading.Thread(target=server_instance.data_callback, args=(query,)).start()
            
        # logger.info(f"Received GET request. Requests in last second: {requests_in_last_second}") # この行は削除

        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"OK")

    def log_request(self, code='-', size='-'):
        """
        デフォルトのログ出力をオーバーライドし、リクエスト数を追加します。
        """
        server_instance = cast("ReplayHTTPServer", self.server)
        with server_instance.request_lock:
            requests_in_last_second = len(server_instance.request_timestamps)

        # デフォルトのログフォーマットを再現し、リクエスト数を追加
        self.log_message('"%s" %s %s (Requests/s: %d)',
                         self.requestline, str(code), str(size), requests_in_last_second)

class ReplayHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """
    複数リクエストを処理できるスレッドベースのHTTPサーバー
    """
    def __init__(self, server_address, RequestHandlerClass, data_callback):
        super().__init__(server_address, RequestHandlerClass)
        self.data_callback = data_callback
        self.request_timestamps = [] # リクエストのタイムスタンプを記録するリスト
        self.request_lock = threading.Lock() # リクエストカウント用のロック
        logger.info(f"HTTPサーバーをポート {server_address[1]} で開始します。")

def start_http_server(port: int, data_callback):
    """
    HTTPサーバーを起動します。

    Args:
        port (int): サーバーがリッスンするポート番号。
        data_callback (callable): 受信したデータを処理するためのコールバック関数。
    """
    server_address = ('', port)
    httpd = ReplayHTTPServer(server_address, ReplayDataHandler, data_callback)
    
    # サーバーをバックグラウンドスレッドで実行
    # mainスレッドはPanda3Dのメインループのために必要
    server_thread = threading.Thread(target=httpd.serve_forever)
    server_thread.daemon = True # メインスレッド終了時に一緒に終了させる
    server_thread.start()
    logger.info("HTTPサーバーのスレッドが起動しました。")
    return httpd # 必要であれば停止するために返す
    
if __name__ == '__main__':
    # テスト用のコールバック関数
    def test_data_callback(data):
        logger.info(f"Received data from Stormworks: {data[:150]}...") # 長すぎるので一部だけ表示

    # ポート8000でサーバーを起動
    server = start_http_server(8000, test_data_callback)
    try:
        # サーバーを動かし続けるためにメインスレッドを待機させる
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("サーバーをシャットダウンします。")
        server.shutdown()
        server.server_close()
