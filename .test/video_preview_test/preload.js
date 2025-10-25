const { contextBridge, ipcRenderer } = require('electron');
const path = require('path');

contextBridge.exposeInMainWorld('electronAPI', {
  /**
   * video.mp4への絶対パスを返します。
   * main.jsで `sandbox: false` が設定されているため、レンダラープロセスは
   * このパスを直接 <video> タグのsrcとして使用できます。
   * @returns {Promise<string>} ビデオファイルへの絶対パス
   */
  getVideoPath: () => {
    // __dirnameは現在のファイル(preload.js)のディレクトリを指します: .test/video_preview_test/
    // そこからプロジェクトルートに戻り、目的のファイルへのパスを構築します。
    const videoPath = path.join(__dirname, '..', '..', 'python-version', 'video.mp4');
    // Windowsのパス区切り文字'\'をURLで有効な'/'に置換する
    const normalizedPath = path.normalize(videoPath).replace(/\\/g, '/');
    return Promise.resolve(normalizedPath);
  }
});
