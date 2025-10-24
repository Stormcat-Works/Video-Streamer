# main.py
# coding: utf-8
import http.server
import socketserver
import numpy as np
import base64
from urllib.parse import urlparse, parse_qs
import uuid
import time
from collections import OrderedDict
from typing import Optional, Dict, Any, List, Tuple, Iterable, Set
import random
import os      # [追加] ファイルパスの操作に必要
import cv2     # [追加] 動画の読み込みと処理に必要

# ==============================================================================
# 定数セクション
# ==============================================================================
# サーバー設定
PORT = 8000
CHUNK_SIZE_LIMIT = 4000

# 画像設定
IMG_WIDTH = 200
IMG_HEIGHT = 150

# [追加] 動画ファイルへのパス
VIDEO_PATH = 'video.mp4'

# パレット管理設定
MAX_PALETTES_TO_KEEP = 500
MAX_PALETTE_COLORS = 256

# ==============================================================================
# (BouncingShapesSimulator, Shapeクラスは変更なし)
# ==============================================================================
class Shape:
    """単一の図形の状態（位置、速度、色など）を保持するクラス。"""
    def __init__(self, bounds_x, bounds_y):
        self.w = random.randint(10, 20)
        self.h = random.randint(10, 20)
        self.x = random.uniform(0, bounds_x - self.w)
        self.y = random.uniform(0, bounds_y - self.h)
        self.dx = random.choice([-2, -1.5, -1, 1, 1.5, 2])
        self.dy = random.choice([-2, -1.5, -1, 1, 1.5, 2])
        self.color = [random.randint(100, 255), random.randint(100, 255), random.randint(100, 255)]

class BouncingShapesSimulator:
    """複数の図形を管理し、フレームごとに状態を更新・描画するクラス。"""
    def __init__(self, count: int, width: int, height: int):
        self.width = width
        self.height = height
        self.shapes = [Shape(width, height) for _ in range(count)]

    def update_and_draw_frame(self) -> np.ndarray:
        canvas = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        for shape in self.shapes:
            shape.x += shape.dx
            shape.y += shape.dy
            if shape.x < 0: shape.x = 0; shape.dx *= -1
            elif shape.x + shape.w > self.width: shape.x = self.width - shape.w; shape.dx *= -1
            if shape.y < 0: shape.y = 0; shape.dy *= -1
            elif shape.y + shape.h > self.height: shape.y = self.height - self.h; shape.dy *= -1
            x, y, w, h = int(shape.x), int(shape.y), int(shape.w), int(shape.h)
            canvas[y:y+h, x:x+w] = shape.color
        return canvas

# ==============================================================================
# グローバル変数
# ==============================================================================
IMAGE_FRAMES = OrderedDict()
MAX_FRAMES_TO_KEEP = 10
bouncing_shapes_sim = BouncingShapesSimulator(count=4, width=IMG_WIDTH, height=IMG_HEIGHT)

# [修正] video_streamingモードをリストの先頭に追加
MODES = ['video_streaming', 'bouncing_shapes', 'random_color_noise', 'random_gray_noise', 'random_bw_noise']
current_mode_index = 0
last_mode_switch_time = time.time()

# [追加] 動画キャプチャ用のグローバル変数
video_capture: Optional[cv2.VideoCapture] = None

# ==============================================================================
# パレット管理クラス (変更なし)
# ==============================================================================
class PaletteManager:
    def __init__(self, max_size: int):
        self.palettes: OrderedDict[int, Dict[str, Any]] = OrderedDict()
        self.max_size = max_size
        self.next_palette_id = 0
    def get_or_create_palette(self, colors: np.ndarray) -> tuple[int, bool, list]:
        colors_tuple = tuple(map(tuple, colors))
        for pid, data in self.palettes.items():
            if data['colors'] == colors_tuple:
                self.palettes.move_to_end(pid)
                return pid, False, data['colors_list']
        palette_id = self.next_palette_id
        self.next_palette_id += 1
        self.palettes[palette_id] = {'colors': colors_tuple, 'colors_list': [list(c) for c in colors]}
        if len(self.palettes) > self.max_size: self.palettes.popitem(last=False)
        return palette_id, True, self.palettes[palette_id]['colors_list']

palette_manager = PaletteManager(MAX_PALETTES_TO_KEEP)

# ==============================================================================
# HTTPリクエストハンドラ
# ==============================================================================
class ImageChunkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            query = urlparse(self.path).query
            params = parse_qs(query)
            action = params.get('action', [None])[0]
            if action == 'new_frame': self._handle_new_frame(params)
            elif action == 'get_chunk': self._handle_get_chunk(params)
            else: self.send_error(400, "Invalid action.")
        except Exception as e:
            #print(f"Error: {e}")
            self.send_error(500, "Internal Server Error")

    def _handle_new_frame(self, params: dict):
        global current_mode_index, last_mode_switch_time
        # video_streamingモード以外の場合は5秒で切り替え
        if MODES[current_mode_index] != 'video_streaming' and time.time() - last_mode_switch_time > 5.0:
            current_mode_index = (current_mode_index + 1) % len(MODES)
            last_mode_switch_time = time.time()
            #print(f"\nSwitching to mode: {MODES[current_mode_index]}")
        
        cached_pids_str = params.get('cached_pids', [None])[0]
        client_cached_pids = set()
        if cached_pids_str:
            try:
                client_cached_pids = {int(p) for p in cached_pids_str.split(',')}
            except ValueError:
                pass
        
        prev_frame = IMAGE_FRAMES[next(reversed(IMAGE_FRAMES))] if IMAGE_FRAMES else {}
        prev_frame_data = prev_frame.get('image_data')
        image_data = self._generate_image(MODES[current_mode_index], prev_frame_data)

        candidates = {
            'FULL': self._create_full_update(image_data),
            'DIFF': self._create_diff_update(image_data, prev_frame_data),
            'FR': self._create_full_rle_update(image_data),
            'IDX': self._create_indexed_update(image_data, client_cached_pids),
            'D_IDX': self._create_diff_indexed_update(image_data, prev_frame_data, client_cached_pids),
            'IR': self._create_indexed_rle_update(image_data, client_cached_pids),
        }
        
        valid_candidates = {k: v for k, v in candidates.items() if v is not None}
        if not valid_candidates:
            self.send_response(204); self.end_headers(); return

        best_format, data_to_send = min(valid_candidates.items(), key=lambda item: len(item[1]))
        #print(f"Frame Sent. Format: {best_format}, Size: {len(data_to_send)} \r", end="") # [修正] 見やすくするため改行を削除
        
        chunks = [data_to_send[i:i + CHUNK_SIZE_LIMIT] for i in range(0, len(data_to_send), CHUNK_SIZE_LIMIT)]
        frame_id = uuid.uuid4().hex
        IMAGE_FRAMES[frame_id] = {'chunks': chunks, 'timestamp': time.time(), 'image_data': image_data}
        if len(IMAGE_FRAMES) > MAX_FRAMES_TO_KEEP: IMAGE_FRAMES.popitem(last=False)

        total_chunks = len(chunks)
        response_body = f"{frame_id};{total_chunks}"
        if chunks and len(response_body) + 1 + len(chunks[0]) < CHUNK_SIZE_LIMIT: response_body += f";{chunks[0]}"

        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(response_body.encode('utf-8'))

    def _handle_get_chunk(self, params: dict):
        frame_id = params.get('frame_id', [None])[0]
        chunk_index = int(params.get('chunk', [0])[0])
        if frame_id and frame_id in IMAGE_FRAMES and 0 <= chunk_index < len(IMAGE_FRAMES[frame_id]['chunks']):
            response_body = f"{frame_id};{chunk_index};{IMAGE_FRAMES[frame_id]['chunks'][chunk_index]}"
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(response_body.encode('utf-8'))
        else:
            self.send_error(404, "Frame or Chunk not found.")
            
    def _generate_image(self, mode: str, prev_frame_data: Optional[np.ndarray]) -> np.ndarray:
        # [追加] video_streamingモードの処理
        if mode == 'video_streaming':
            if video_capture and video_capture.isOpened():
                ret, frame = video_capture.read()
                if not ret:
                    # 動画の最後に到達したら、再生位置を最初に戻す
                    video_capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = video_capture.read()
                
                if ret:
                    # 目的の解像度にリサイズ
                    # INTER_AREAは縮小に適した補間方法
                    resized_frame = cv2.resize(frame, (IMG_WIDTH, IMG_HEIGHT), interpolation=cv2.INTER_AREA)
                    # OpenCVはBGR形式なので、RGB形式に変換
                    rgb_frame = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2RGB)
                    return rgb_frame
            # 動画ファイルがない、または開けない場合は黒い画像を表示
            return np.zeros((IMG_HEIGHT, IMG_WIDTH, 3), dtype=np.uint8)

        elif mode == 'bouncing_shapes': return bouncing_shapes_sim.update_and_draw_frame()
        elif mode == 'random_color_noise': return np.random.randint(0, 256, (IMG_HEIGHT, IMG_WIDTH, 3), dtype=np.uint8)
        elif mode == 'random_gray_noise': return np.repeat(np.random.randint(0, 256, (IMG_HEIGHT, IMG_WIDTH, 1), dtype=np.uint8), 3, axis=2)
        elif mode == 'random_bw_noise': return np.repeat(np.random.randint(0, 2, (IMG_HEIGHT, IMG_WIDTH, 1), dtype=np.uint8) * 255, 3, axis=2)
        
        return np.zeros((IMG_HEIGHT, IMG_WIDTH, 3), dtype=np.uint8)

    # --- データ形式生成ヘルパー (以降の関数は変更なし) ---
    def _rle_encode(self, data_stream: Iterable[Any]) -> List[Tuple[Any, int]]:
        it = iter(data_stream)
        try:
            prev_val = next(it)
        except StopIteration:
            return []
        count = 1
        encoded = []
        for val in it:
            if val == prev_val:
                count += 1
            else:
                encoded.append((prev_val, count))
                prev_val = val
                count = 1
        encoded.append((prev_val, count))
        return encoded

    def _create_full_update(self, image_data: np.ndarray) -> str:
        return "F|" + base64.b64encode(image_data.tobytes()).decode('ascii')

    def _create_full_rle_update(self, image_data: np.ndarray) -> Optional[str]:
        hex_colors = [f"{p[0]:02x}{p[1]:02x}{p[2]:02x}" for p in image_data.reshape(-1, 3)]
        rle_data = self._rle_encode(hex_colors)
        if not rle_data: return None
        rle_payload = "|".join(f"{color_hex},{count:x}" for color_hex, count in rle_data)
        return f"FR|{rle_payload}"

    def _create_diff_update(self, image_data: np.ndarray, prev_data: Optional[np.ndarray]) -> Optional[str]:
        if prev_data is None: return None
        diff_mask = np.any(image_data != prev_data, axis=2); coords = np.where(diff_mask)
        if coords[0].size == 0: return None
        parts = [f"{y * IMG_WIDTH + x:x}:{image_data[y, x][0]:02x}{image_data[y, x][1]:02x}{image_data[y, x][2]:02x}" for y, x in zip(*coords)]
        return "D|" + "|".join(parts)

    def _create_indexed_update(self, image_data: np.ndarray, client_cached_pids: Set[int]) -> Optional[str]:
        unique_colors = np.unique(image_data.reshape(-1, 3), axis=0); num_colors = len(unique_colors)
        if not (1 < num_colors <= MAX_PALETTE_COLORS): return None
        pid, is_new_to_server, palette = palette_manager.get_or_create_palette(unique_colors)
        
        send_palette_data = is_new_to_server or (pid not in client_cached_pids)
        palette_payload = ",".join(f"{c[0]:02x}{c[1]:02x}{c[2]:02x}" for c in palette) if send_palette_data else ""
        
        color_to_idx = {tuple(color): i for i, color in enumerate(palette)}
        indices = [color_to_idx[tuple(p)] for p in image_data.reshape(-1, 3)]
        hex_format = "{:x}" if num_colors <= 16 else "{:02x}"
        indices_payload = "".join(map(hex_format.format, indices))
        return f"I|{pid}|{palette_payload}|{indices_payload}"

    def _create_indexed_rle_update(self, image_data: np.ndarray, client_cached_pids: Set[int]) -> Optional[str]:
        unique_colors = np.unique(image_data.reshape(-1, 3), axis=0); num_colors = len(unique_colors)
        if not (1 < num_colors <= MAX_PALETTE_COLORS): return None
        pid, is_new_to_server, palette = palette_manager.get_or_create_palette(unique_colors)

        send_palette_data = is_new_to_server or (pid not in client_cached_pids)
        palette_payload = ",".join(f"{c[0]:02x}{c[1]:02x}{c[2]:02x}" for c in palette) if send_palette_data else ""
        
        color_to_idx = {tuple(color): i for i, color in enumerate(palette)}
        indices = [color_to_idx[tuple(p)] for p in image_data.reshape(-1, 3)]
        rle_data = self._rle_encode(indices)
        if not rle_data: return None
        hex_format = "{:x}" if num_colors <= 16 else "{:02x}"
        rle_payload = "|".join(f"{hex_format.format(idx)},{count:x}" for idx, count in rle_data)
        return f"IR|{pid}|{palette_payload}|{rle_payload}"

    def _create_diff_indexed_update(self, image_data: np.ndarray, prev_data: Optional[np.ndarray], client_cached_pids: Set[int]) -> Optional[str]:
        if prev_data is None: return None
        unique_colors = np.unique(image_data.reshape(-1, 3), axis=0); num_colors = len(unique_colors)
        if not (1 < num_colors <= MAX_PALETTE_COLORS): return None
        diff_mask = np.any(image_data != prev_data, axis=2); coords = np.where(diff_mask)
        if coords[0].size == 0: return None
        pid, is_new_to_server, palette = palette_manager.get_or_create_palette(unique_colors)

        send_palette_data = is_new_to_server or (pid not in client_cached_pids)
        palette_payload = ",".join(f"{c[0]:02x}{c[1]:02x}{c[2]:02x}" for c in palette) if send_palette_data else ""
        
        color_to_idx = {tuple(color): i for i, color in enumerate(palette)}
        hex_format = "{:x}" if num_colors <= 16 else "{:02x}"
        parts = [f"{y * IMG_WIDTH + x:x}:{hex_format.format(color_to_idx[tuple(image_data[y, x])])}" for y, x in zip(*coords)]
        return f"DI|{pid}|{palette_payload}|{'|'.join(parts)}"

# ==============================================================================
# [追加] ビデオキャプチャのセットアップ
# ==============================================================================
def setup_video_capture():
    """動画ファイルを読み込み、グローバル変数を初期化する。"""
    global video_capture
    if os.path.exists(VIDEO_PATH):
        video_capture = cv2.VideoCapture(VIDEO_PATH)
        if not video_capture.isOpened():
            #print(f"エラー: 動画ファイル '{VIDEO_PATH}' を開けませんでした。video_streamingモードは無効になります。")
            video_capture = None
        else:
            # 動画のプロパティを取得して表示
            fps = video_capture.get(cv2.CAP_PROP_FPS)
            width = int(video_capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(video_capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            #print(f"動画ファイル '{VIDEO_PATH}' を正常に読み込みました。({width}x{height} @ {fps:.2f} FPS)")
    else:
        #print(f"警告: 動画ファイル '{VIDEO_PATH}' が見つかりません。video_streamingモードは無効になります。")
        video_capture = None
        # video_streamingモードをリストから削除
        if 'video_streaming' in MODES:
            MODES.remove('video_streaming')

# ==============================================================================
# サーバー実行
# ==============================================================================
def run_server():
    # [追加] サーバー起動前にビデオをセットアップ
    setup_video_capture()
    with socketserver.TCPServer(("", PORT), ImageChunkHandler) as httpd:
        #print(f"Python HTTPサーバーを起動しました (最適化・同期モード)。")
        #print(f"ポート: {PORT}")
        #print("利用可能なモード:", MODES)
        #print("Ctrl+Cでサーバーを停止します。")
        httpd.serve_forever()

if __name__ == "__main__":
    run_server()