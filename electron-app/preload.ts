// electron-app/preload.ts
import { contextBridge, ipcRenderer } from 'electron';
import * as path from 'path';

// 'api'というグローバルオブジェクトを介して、安全にNode.jsの機能を
// レンダラープロセス（フロントエンドのJavaScript）に公開します。
contextBridge.exposeInMainWorld('api', {
  // メインプロセスにメッセージを送信する関数
  startServer: (port: number) => ipcRenderer.send('start-server', port),
  stopServer: () => ipcRenderer.send('stop-server'),
  togglePlayback: (isPlaying: boolean) => ipcRenderer.send('toggle-playback', isPlaying),
  seekVideo: (time: number) => ipcRenderer.send('seek-video', time),
  getVideoPath: (): Promise<string> => {
    // main.tsで `sandbox: false` が設定されているため、ここでNode.jsモジュールが使用可能
    const videoPath = path.join(__dirname, '..', '..', 'python-version', 'video.mp4');
    // Windowsのパス区切り文字'\'をURLで有効な'/'に置換する
    const normalizedPath = path.normalize(videoPath).replace(/\\/g, '/');
    return Promise.resolve(normalizedPath);
  },

  // メインプロセスからのメッセージを受信する関数
  // (例: サーバーの状態が変化したときの通知を受け取る)
  onServerStatus: (callback: (status: string, ...args: any[]) => void) => {
    const listener = (event: any, status: string, ...args: any[]) => callback(status, ...args);
    ipcRenderer.on('server-status', listener);
    
    // クリーンアップ関数を返すことで、コンポーネントのアンマウント時などに
    // リスナーを削除できるようにします。
    return () => {
      ipcRenderer.removeListener('server-status', listener);
    };
  }
});
