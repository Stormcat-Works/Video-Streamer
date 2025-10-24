// electron-app/preload.ts
import { contextBridge, ipcRenderer } from 'electron';

// 'api'というグローバルオブジェクトを介して、安全にNode.jsの機能を
// レンダラープロセス（フロントエンドのJavaScript）に公開します。
contextBridge.exposeInMainWorld('api', {
  // メインプロセスにメッセージを送信する関数
  startServer: (port: number) => ipcRenderer.send('start-server', port),
  stopServer: () => ipcRenderer.send('stop-server'),

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
