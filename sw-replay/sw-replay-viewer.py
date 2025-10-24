# viewer.py
from direct.showbase.ShowBase import ShowBase
from panda3d.core import Vec3, LQuaternion, NodePath, TextNode # TextNodeをインポート
from panda3d.core import PointLight, AmbientLight
import os
import threading
import time
import logging
import math # mathモジュールを追加
from direct.gui.DirectGui import * # DirectGUIをインポート

logger = logging.getLogger(__name__)

class ReplayViewer(ShowBase):
    """
    Panda3D を使ったリプレイデータ表示ビューア。
    """
    def __init__(self, data_manager, model_base_dir="models"):
        ShowBase.__init__(self) # これを最初に呼び出す
        self.camera: NodePath = self.camera # 明示的な型ヒントを追加
        self.globalClock = self.taskMgr.globalClock # globalClockをインスタンス変数として設定
        self.data_manager = data_manager
        self.model_base_dir = model_base_dir
        self.vehicles = {} # ビークルIDとPanda3D NodePathのマップ (3Dモデル用)
        self.loaded_models = {} # ロード済みのPLYモデルキャッシュ {model_path: NodePath}
        self.gui_vehicle_items = {} # GUI上のビークル項目 {vehicle_id: DirectButton}
        self.following_vehicle_id = None # 追従中のビークルID
        self.vehicle_info_label = None # 追従中のビークルの座標と姿勢を表示するラベル
        self.frame_info_label = None # フレーム情報とFPSを表示するラベル

        # カメラ操作用の状態変数
        self.mouse_x = 0
        self.mouse_y = 0
        self.keyboard_move_speed = 200.0 # キーボードによる基本移動速度
        self.mouse_pan_speed = 10000.0 # マウスによる平行移動速度 (ホイールドラッグ)
        self.camera_rotate_speed = 20.0 # 基本回転速度 (マウス感度)

        self.setup_lighting()
        self.setup_camera()
        self.setup_grid() # グリッド設定を追加
        
        # 毎フレームの更新タスクを設定
        self.taskMgr.add(self.update_vehicles_task, "update_vehicles_task")
        self.taskMgr.add(self.update_camera_task, "update_camera_task") # カメラ更新タスクを追加
        
        # UIコントロールの追加
        self.setup_ui()
        logger.info("ReplayViewer初期化済み。")

    def setup_lighting(self):
        """シーンの照明を設定します。"""
        ambientLight = AmbientLight("ambientLight")
        ambientLight.setColor((0.3, 0.3, 0.3, 1))
        self.render.setLight(self.render.attachNewNode(ambientLight))

        directionalLight = PointLight("directionalLight")
        directionalLight.setColor((0.3, 0.3, 0.3, 1)) # 強度をさらに下げる
        directionalLightNP = self.render.attachNewNode(directionalLight)
        directionalLightNP.setPos(20, -20, 20)
        self.render.setLight(directionalLightNP)
        logger.debug("照明を設定しました。")

    def setup_camera(self):
        """カメラの初期位置とコントロールを設定します。"""
        # マウスによるカメラコントロールを無効化 (独自のカメラ操作を実装するため)
        self.disableMouse() # Panda3Dのデフォルトマウスコントロールを無効化
        self.camera: NodePath # 明示的な型ヒントを追加
        self.camera.setPos(0, -50, 20) # type: ignore
        self.camera.lookAt(0, 0, 0) # type: ignore
        
        logger.debug("カメラを設定しました。")

    def update_camera_task(self, task):
        """
        毎フレームカメラを更新するタスク。
        マウスとキーボードの入力に基づいてカメラを移動・回転させます。
        """
        assert isinstance(self.camera, NodePath), "self.camera is not a NodePath"
        dt = self.globalClock.getDt() # 前のフレームからの経過時間

        # キーボード移動速度の調整
        current_keyboard_move_speed = self.keyboard_move_speed
        if self.mouseWatcherNode and self.mouseWatcherNode.isButtonDown('shift'): # type: ignore
            current_keyboard_move_speed *= 10.0 # 加速
        if self.mouseWatcherNode and self.mouseWatcherNode.isButtonDown('control'): # type: ignore
            current_keyboard_move_speed *= 0.1 # 減速

        # キーボードによる移動 (直接ポーリング)
        move_vec = Vec3(0, 0, 0)
        if self.mouseWatcherNode and self.mouseWatcherNode.isButtonDown('w'): # type: ignore
            move_vec.setY(move_vec.getY() + 1)
        if self.mouseWatcherNode and self.mouseWatcherNode.isButtonDown('s'): # type: ignore
            move_vec.setY(move_vec.getY() - 1)
        if self.mouseWatcherNode and self.mouseWatcherNode.isButtonDown('a'): # type: ignore
            move_vec.setX(move_vec.getX() - 1)
        if self.mouseWatcherNode and self.mouseWatcherNode.isButtonDown('d'): # type: ignore
            move_vec.setX(move_vec.getX() + 1)
        if self.mouseWatcherNode and self.mouseWatcherNode.isButtonDown('q'): # type: ignore
            move_vec.setZ(move_vec.getZ() - 1)
        if self.mouseWatcherNode and self.mouseWatcherNode.isButtonDown('e'): # type: ignore
            move_vec.setZ(move_vec.getZ() + 1)
        
        if move_vec.lengthSquared() > 0:
            move_vec.normalize()
            self.camera.setPos(self.camera, move_vec * current_keyboard_move_speed * dt) # type: ignore
            self.following_vehicle_id = None # キーボード移動で追従解除

        # グリッドをカメラのXZ平面に追従させる (ワールド基準のグリッドにスナップ)
        camera_pos = self.camera.getPos() # type: ignore
        
        # カメラのX, Z座標をグリッド間隔の倍数に丸める
        snapped_x = round(camera_pos.getX() / self.grid_spacing) * self.grid_spacing
        snapped_y = round(camera_pos.getY() / self.grid_spacing) * self.grid_spacing # Panda3DのYはStormworksのZに相当

        self.grid_node.setPos(snapped_x, snapped_y, 0) # Y座標は0に固定

        # ビークル追従ロジック
        if self.following_vehicle_id is not None and self.following_vehicle_id in self.vehicles:
            target_np = self.vehicles[self.following_vehicle_id]
            if target_np:
                # カメラの現在のビークルに対する相対位置と回転を維持
                # カメラをビークルの子ノードとして一時的に設定し、ワールド座標系に戻すことで相対位置を計算
                current_camera_pos_relative_to_target = self.camera.getPos(target_np) # type: ignore
                current_camera_hpr_relative_to_target = self.camera.getHpr(target_np) # type: ignore

                # ビークルの位置と姿勢に基づいてカメラを更新
                self.camera.setPos(target_np, current_camera_pos_relative_to_target) # type: ignore
                self.camera.setHpr(target_np, current_camera_hpr_relative_to_target) # type: ignore
                self.camera.lookAt(target_np) # type: ignore

        # マウスによる操作
        if self.mouseWatcherNode and self.mouseWatcherNode.hasMouse(): # type: ignore
            x, y = self.mouseWatcherNode.getMouseX(), self.mouseWatcherNode.getMouseY() # type: ignore
            dx, dy = x - self.mouse_x, y - self.mouse_y
            self.mouse_x, self.mouse_y = x, y

            if self.mouseWatcherNode.isButtonDown('mouse3'): # 右ドラッグで視点回転
                if self.following_vehicle_id is not None and self.following_vehicle_id in self.vehicles:
                    target_np = self.vehicles[self.following_vehicle_id]
                    # カメラを一時的にビークルの子ノードにする
                    self.camera.reparentTo(target_np) # type: ignore
                    hpr = self.camera.getHpr() # type: ignore
                    self.camera.setHpr(hpr.getX() + dx * self.camera_rotate_speed, # Yaw
                                       hpr.getY() - dy * self.camera_rotate_speed, # Pitch
                                       hpr.getZ()) # type: ignore
                    # カメラをワールド座標系に戻す
                    self.camera.reparentTo(self.render) # type: ignore
                else:
                    hpr = self.camera.getHpr() # type: ignore
                    self.camera.setHpr(hpr.getX() + dx * self.camera_rotate_speed, # Yaw
                                       hpr.getY() - dy * self.camera_rotate_speed, # Pitch
                                       hpr.getZ()) # type: ignore
            elif self.mouseWatcherNode.isButtonDown('mouse2'): # ホイールドラッグで画面に平行移動
                # カメラのローカル座標系で移動
                self.camera.setPos(self.camera, Vec3(-dx * self.mouse_pan_speed * dt, 0, -dy * self.mouse_pan_speed * dt)) # type: ignore
                self.following_vehicle_id = None # マウスパンで追従解除

        # UIの位置をアスペクト比に合わせて更新
        frame_width = 0.4 # setup_uiと同じ値を使用
        current_aspect_ratio = self.getAspectRatio()
        new_frame_pos_x = current_aspect_ratio - (frame_width / 2)
        self.vehicle_list_frame.setX(new_frame_pos_x)

        # 追従中のビークル情報表示の更新
        if self.following_vehicle_id is not None and self.following_vehicle_id in self.vehicles:
            target_np = self.vehicles[self.following_vehicle_id]
            if target_np:
                # Stormworksの座標系に変換 (YとZを入れ替え)
                pos_sw = Vec3(target_np.getX(), target_np.getZ(), target_np.getY())
                
                # Panda3DのHPRを度数法に変換
                h, p, r = target_np.getHpr()
                # StormworksのRoll, Pitch, Yawにマッピング
                # Panda3DのHはYaw、PはPitch、RはRollに相当
                roll_sw = r
                pitch_sw = p
                yaw_sw = h

                info_text = (
                    f"Pos X: {int(pos_sw.getX())} Y: {int(pos_sw.getY())} Z: {int(pos_sw.getZ())}\n"
                    f"Roll: {int(roll_sw)} Pitch: {int(pitch_sw)} Yaw: {int(yaw_sw)}"
                )
                if self.vehicle_info_label:
                    self.vehicle_info_label.setText(info_text)
                    self.vehicle_info_label.show()
            else:
                if self.vehicle_info_label:
                    self.vehicle_info_label.hide()
        else:
            if self.vehicle_info_label:
                self.vehicle_info_label.hide()

        # フレーム情報とFPS表示の更新 (新規追加)
        current_frame = self.data_manager.current_frame_index
        total_frames = self.data_manager.get_frame_count()
        fps = round(self.globalClock.getAverageFrameRate(), 1) # FPSを取得

        frame_text = f"Frame: {current_frame}/{total_frames} | FPS: {fps}"
        if self.frame_info_label:
            self.frame_info_label.setText(frame_text)

        return task.cont

    def load_vehicle_model(self, vehicle_id: int) -> NodePath:
        """
        ビークルIDに対応するPLYモデルをロードします。
        一度ロードしたモデルはキャッシュし、再利用します。
        """
        model_path = os.path.join(self.model_base_dir, f"vehicle_{vehicle_id}.ply")
        if not os.path.exists(model_path):
            #logger.warning(f"モデルファイルが見つかりません: {model_path}")
            return None 

        if model_path in self.loaded_models:
            # 新しいルートノードを作成
            vehicle_root_node = NodePath(f"vehicle_root_{vehicle_id}")
            vehicle_root_node.reparentTo(self.render)
            
            # キャッシュされたモデルのコピーをルートノードの子としてアタッチ
            model = self.loaded_models[model_path].copyTo(vehicle_root_node)
            
            # ここでモデルの初期姿勢を調整 (モデルのローカル変換として固定)
            model.setScale(-1, 1, 1) # X軸スケールを-1倍
            model.setP(model.getP() + 90) # ピッチを+90度オフセット
            
            return vehicle_root_node # ルートノードを返す
        
        try:
            # モデルをロード
            model: NodePath = self.loader.loadModel(model_path) # type: ignore
            if model:
                # 新しいルートノードを作成
                vehicle_root_node = NodePath(f"vehicle_root_{vehicle_id}")
                vehicle_root_node.reparentTo(self.render)
                
                # ロードしたモデルをルートノードの子としてアタッチ
                model.reparentTo(vehicle_root_node)
                
                # ここでモデルの初期姿勢を調整 (モデルのローカル変換として固定)
                model.setScale(-1, 1, 1) # X軸スケールを-1倍
                model.setP(model.getP() + 90) # ピッチを+90度オフセット
                
                self.loaded_models[model_path] = model # キャッシュにはオフセット適用済みのモデルを格納
                logger.info(f"モデル {model_path} をロードしました。")
                return vehicle_root_node # ルートノードを返す
            else:
                logger.error(f"モデル {model_path} のロードに失敗しました。")
                return None
        except Exception as e:
            logger.error(f"モデル {model_path} のロード中にエラーが発生しました: {e}")
            return None

    def update_vehicles_task(self, task):
        """
        毎フレーム、ビークルデータを更新し、Panda3Dモデルに適用します。
        """
        if self.data_manager.is_playing:
            if task.time % (1.0 / self.data_manager.frame_rate) < self.globalClock.getDt(): # type: ignore
                self.data_manager.next_frame()

        current_frame_data = self.data_manager.get_current_frame_data()

        # 現在のフレームに存在しないビークルを非表示または削除
        vehicles_to_remove = [vid for vid in self.vehicles if vid not in current_frame_data]
        for vid in vehicles_to_remove:
            self.vehicles[vid].removeNode()
            del self.vehicles[vid]

        for vehicle_id, data in current_frame_data.items():
            if vehicle_id not in self.vehicles:
                # 新しいビークルモデルをロード
                model_np = self.load_vehicle_model(vehicle_id)
                if model_np:
                    self.vehicles[vehicle_id] = model_np
                else:
                    continue # モデルがロードできなかった場合はスキップ

            # 位置と姿勢の更新
            vehicle_np = self.vehicles[vehicle_id]
            if vehicle_np is None: # Noneの場合はスキップ
                continue
            
            pos = Vec3(data['position'][0], data['position'][2], data['position'][1]) # YとZを入れ替え
            vehicle_np.setPos(pos)
            # logger.info(f"Python Viewer: Vehicle {vehicle_id} SetPos: ({pos.getX():.2f}, {pos.getY():.2f}, {pos.getZ():.2f})")
            
            yaw_deg = math.degrees(data['rotation'][0])   # LuaのYawをPanda3DのYawに
            pitch_deg = math.degrees(data['rotation'][1]) # LuaのPitchをPanda3DのPitchに
            roll_deg = math.degrees(data['rotation'][2])  # LuaのRollをPanda3DのRollに
            
            vehicle_np.setHpr(yaw_deg, pitch_deg, roll_deg) # type: ignore
            # logger.info(f"Python Viewer: Vehicle {vehicle_id} SetHpr: (Y:{yaw_deg:.2f}, P:{pitch_deg:.2f}, R:{roll_deg:.2f})")

        return task.cont

    def setup_grid(self):
        """
        高度0m、XZ座標はカメラを中心とした水平なグリッドを作成します。
        """
        from panda3d.core import LineSegs, Vec4

        grid_size = 5000 # グリッドの範囲 (m) - 10kmの半分
        self.grid_spacing = 100 # グリッドの間隔 (m) - インスタンス変数として保持
        grid_color = Vec4(0.2, 0.2, 0.2, 1) # グリッドの色 (暗い灰色)

        lines = LineSegs("grid")
        lines.setThickness(1.0) # 線の太さ
        lines.setColor(grid_color)

        # X軸に平行な線
        for i in range(-grid_size // self.grid_spacing, grid_size // self.grid_spacing + 1):
            x = i * self.grid_spacing
            lines.moveTo(x, -grid_size, 0)
            lines.drawTo(x, grid_size, 0)

        # Y軸に平行な線 (Panda3DのY軸はStormworksのZ軸に相当)
        for i in range(-grid_size // self.grid_spacing, grid_size // self.grid_spacing + 1):
            y = i * self.grid_spacing
            lines.moveTo(-grid_size, y, 0)
            lines.drawTo(grid_size, y, 0)

        self.grid_node = self.render.attachNewNode(lines.create()) # インスタンス変数として保持
        logger.debug("グリッドを設定しました。")

    def setup_ui(self):
        """
        GUI要素を設定します。
        """
        # ビークルリスト表示用のフレーム
        frame_width = 0.6 # フレームの幅を拡張
        frame_height = 2.0 # フレームの高さを画面下端まで拡張
        
        # frame_pos_z はフレームの中心を画面中央に設定するため、直接 0 に設定
        # 1.0 - (frame_height / 2) の計算は不要になる

        self.vehicle_list_frame = DirectFrame(
            frameColor=(0.1, 0.1, 0.1, 0.8), # 背景色 (RGBA)
            frameSize=(-frame_width / 2, frame_width / 2, -frame_height / 2, frame_height / 2), # フレームのサイズ (左右下上)
            pos=(0, 0, 0), # フレームの中心を画面中央に設定
            parent=self.aspect2d, # aspect2dを親に
            relief=DGG.FLAT
        )
        
        # ビークルリストのタイトル
        DirectLabel(
            parent=self.vehicle_list_frame,
            text="Vehicle List", # 英語に変更
            scale=0.07,
            pos=(0, 0, frame_height / 2 - 0.08), # フレーム内の相対位置 (上部に配置)
            text_fg=(1, 1, 1, 1), # テキスト色
            relief=None
        )

        # ビークルリストのコンテナフレーム (スクロール可能な領域)
        # タイトルの下端のZ座標を計算 (frame_height=2.0, scale=0.07, pos_z_offset=0.08 を考慮)
        # frame_height / 2 - 0.08 - (0.07 / 2) = 1.0 - 0.08 - 0.035 = 0.885
        container_top_z = 0.885
        container_bottom_z = -1.0 # 画面下端まで

        self.vehicle_list_container = DirectFrame(
            parent=self.vehicle_list_frame,
            frameColor=(0.1, 0.1, 0.1, 0.8), # Vehicle List全体と同じ背景色
            frameSize=(-(frame_width / 2), (frame_width / 2), container_bottom_z, container_top_z), # 幅をフレーム全体に、高さをタイトル下から画面下まで
            pos=(0, 0, (container_top_z + container_bottom_z) / 2), # コンテナの中心を計算
            relief=DGG.FLAT
            # clipBoundsはDirectFrameのオプションではないため削除
        )

        # スクロールバー
        scroll_bar_width = 0.04
        self.vehicle_list_scrollbar = DirectScrollBar(
            parent=self.vehicle_list_frame,
            range=(0, 1), # スクロール範囲 (0:一番上, 1:一番下)
            value=0, # 初期値
            pageSize=0.1, # ページサイズ (スクロール量)
            orientation=DGG.VERTICAL,
            frameSize=(frame_width / 2 - scroll_bar_width, frame_width / 2, -(frame_height / 2 - 0.1), (frame_height / 2 - 0.1)), # スクロールバーのフレームサイズ
            pos=(0, 0, -0.05), # フレーム内の相対位置 (リストの右端)
            command=self.scroll_vehicle_list,
            thumb_frameColor=(0.6, 0.6, 0.6, 1), # つまみの色
            incButton_text="", # ボタンテキスト
            decButton_text="", # ボタンテキスト
            incButton_text_scale=0.05,
            decButton_text_scale=0.05,
            incButton_pos=(0, 0, (frame_height / 2 - 0.1) + 0.05), # 上ボタンの位置
            decButton_pos=(0, 0, -(frame_height / 2 - 0.1) - 0.05), # 下ボタンの位置
            relief=DGG.FLAT,
            incButton_frameColor=(0.2, 0.2, 0.2, 1), # ボタンの背景色
            decButton_frameColor=(0.2, 0.2, 0.2, 1) # ボタンの背景色
        )
        # スクロールバーのつまみのサイズと色を調整
        self.vehicle_list_scrollbar['thumb_frameSize'] = (-scroll_bar_width / 2, scroll_bar_width / 2, -0.05, 0.05) # つまみの幅を調整
        self.vehicle_list_scrollbar['thumb_frameColor'] = (0.1, 0.1, 0.1, 0.8) # つまみの色を単色背景に
        self.vehicle_list_scrollbar['frameColor'] = (0.4, 0.4, 0.4, 1) # スクロールバーのトラックの色


        # ビークルリストを更新するタスクを追加
        self.taskMgr.add(self.update_vehicle_list_task, "update_vehicle_list_task")
        
        # 追従中のビークル情報表示ラベル
        self.vehicle_info_label = DirectLabel(
            parent=self.aspect2d,
            text="",
            scale=0.05,
            pos=(-self.getAspectRatio() + 0.1, 0, 0.9), # 画面左上
            text_align=TextNode.ALeft,
            text_fg=(1, 1, 1, 1),
            frameColor=(0.1, 0.1, 0.1, 0.8),
            relief=DGG.FLAT,
            pad=(0.02, 0.02)
        )
        self.vehicle_info_label.hide() # 最初は非表示

        # フレーム情報とFPS表示ラベル (新規追加)
        self.frame_info_label = DirectLabel(
            parent=self.aspect2d,
            text="",
            scale=0.05,
            pos=(-self.getAspectRatio() + 0.1, 0, -0.9), # 画面左下
            text_align=TextNode.ALeft,
            text_fg=(1, 1, 1, 1),
            frameColor=(0.1, 0.1, 0.1, 0.8),
            relief=DGG.FLAT,
            pad=(0.02, 0.02)
        )
        self.frame_info_label.show() # 最初から表示

        logger.debug("GUIを設定しました。")

    def update_vehicle_list_task(self, task):
        """
        ビークルリストを定期的に更新するタスク。
        """
        current_vehicle_ids = set(self.data_manager.get_current_frame_data().keys())
        
        # 現在GUIに表示されているビークルIDのセット
        displayed_gui_vids = set(self.gui_vehicle_items.keys())

        # 新しいビークルが追加されたか、既存のビークルが削除されたかを確認
        if current_vehicle_ids != displayed_gui_vids:
            self.rebuild_vehicle_list()

        return task.cont

    def add_vehicle_to_list(self, vehicle_id: int):
        """
        ビークルをリストに追加します。（DirectScrolledListは使用しない）
        このメソッドはrebuild_vehicle_listからのみ呼ばれる想定。
        """
        button = DirectButton(
            text=f"Vehicle ID: {vehicle_id}",
            scale=0.05,
            command=self.follow_vehicle,
            extraArgs=[vehicle_id],
            text_align=TextNode.ACenter, # 中央寄せ
            text_fg=(1, 1, 1, 1),
            relief=DGG.FLAT,
            frameColor=(0.2, 0.2, 0.2, 0), # 透明なフレーム
            rolloverSound=None,
            clickSound=None
        )
        return button

    def rebuild_vehicle_list(self):
        """
        ビークルリストを現在のビークルデータに基づいて再構築します。
        既存のGUI要素を破棄し、再生成します。
        """
        # 既存のビークルリスト項目を全て削除
        for item in self.gui_vehicle_items.values():
            item.destroy()
        self.gui_vehicle_items.clear()

        current_vehicle_ids = sorted(list(self.data_manager.get_current_frame_data().keys()))
        
        item_height = 0.08 # 各項目の高さ
        # コンテナの表示可能領域の高さ
        container_height = self.vehicle_list_container['frameSize'][3] - self.vehicle_list_container['frameSize'][2]
        
        # 全項目が表示された場合の仮想的な高さ
        total_items_height = len(current_vehicle_ids) * item_height

        # スクロールオフセットの初期化
        self.scroll_offset = 0.0

        # スクロールバーの範囲を設定
        if total_items_height > container_height:
            # スクロールが必要な場合
            self.vehicle_list_scrollbar.show()
            # スクロールバーの範囲は、表示可能な高さと全項目の高さの差に基づいて設定
            # value=0 (一番上) から value=1 (一番下) に対応させる
            self.vehicle_list_scrollbar['range'] = (0, total_items_height - container_height)
            self.vehicle_list_scrollbar['value'] = self.scroll_offset
        else:
            # スクロールが不要な場合
            self.vehicle_list_scrollbar.hide()
            self.scroll_offset = 0.0 # オフセットをリセット

        # 新しい項目を追加
        for i, vid in enumerate(current_vehicle_ids):
            button = DirectButton(
                parent=self.vehicle_list_container, # コンテナを親に
                text=f"Vehicle ID: {vid}",
                scale=0.05,
                command=self.follow_vehicle,
                extraArgs=[vid],
                text_align=TextNode.ACenter,
                text_fg=(1, 1, 1, 1),
                relief=DGG.FLAT,
                frameColor=(0.2, 0.2, 0.2, 0),
                rolloverSound=None,
                clickSound=None,
                # posのZ座標はスクロールオフセットを考慮
                pos=(0, 0, self.vehicle_list_container['frameSize'][3] - item_height / 2 - i * item_height + self.scroll_offset)
            )
            self.gui_vehicle_items[vid] = button
        
        logger.debug("ビークルリストを再構築しました。")

    def scroll_vehicle_list(self):
        """
        スクロールバーの値に基づいてビークルリストの表示を更新します。
        """
        self.scroll_offset = self.vehicle_list_scrollbar['value']
        # 各項目の位置を更新
        item_height = 0.08
        # container_height は DirectButton の pos 計算では直接使用しない
        # container_height = self.vehicle_list_container['frameSize'][3] - self.vehicle_list_container['frameSize'][2]
        
        # container_top_z を再利用
        container_top_z = self.vehicle_list_container['frameSize'][3] # 0.885

        for i, vid in enumerate(sorted(list(self.gui_vehicle_items.keys()))):
            button = self.gui_vehicle_items[vid]
            button.setZ(container_top_z - item_height / 2 - i * item_height + self.scroll_offset)

    def follow_vehicle(self, vehicle_id: int):
        """
        指定されたビークルにカメラを追従させます。
        """
        if vehicle_id in self.vehicles and self.vehicles[vehicle_id] is not None:
            target_np = self.vehicles[vehicle_id]
            target_pos = target_np.getPos()
            self.camera.setPos(target_pos.getX() - 50, target_pos.getY() - 50, target_pos.getZ() + 20) # type: ignore
            self.camera.lookAt(target_np) # type: ignore
            self.following_vehicle_id = vehicle_id # 追従中のビークルIDを設定
            logger.info(f"カメラがビークルID {vehicle_id} を追従します。")
        else:
            logger.warning(f"ビークルID {vehicle_id} が存在しないため、追従できません。")
