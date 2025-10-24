// electron-app/main.ts
import { app, BrowserWindow, ipcMain } from 'electron';
import * as path from 'path';
import express from 'express';
import http from 'http';
import { AddressInfo } from 'net';
import crypto from 'crypto';

// HTTPサーバーのインスタンスを保持する変数
let server: http.Server | null = null;

let mainWindow: BrowserWindow | null;

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 800,
        height: 600,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
        },
    });

    mainWindow.loadFile(path.join(__dirname, '../index.html'));

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

// --- ここから追加 ---

// サーバーを起動する関数
function startServer(port: number) {
    if (server) {
        console.log('Server is already running.');
        return;
    }

    const app = express();

    // Stormworksからのリクエストを処理するエンドポイント
    app.get('/', (req, res) => {
        const action = req.query.action;
        if (action === 'new_frame') {
            // 固定の単色画像データ (64x64の赤い画像) を生成
            const width = 64;
            const height = 64;
            const pixels = Buffer.alloc(width * height * 3, 0);
            for (let i = 0; i < width * height; i++) {
                pixels[i * 3] = 255; // R
                pixels[i * 3 + 1] = 0;   // G
                pixels[i * 3 + 2] = 0;   // B
            }

            // 'F' (Full) 形式でエンコード
            const encodedData = "F|" + pixels.toString('base64');
            
            const frameId = crypto.randomUUID();
            const totalChunks = 1; // データが小さいのでチャンクは1つ
            
            // レスポンスを送信
            const responseBody = `${frameId};${totalChunks};${encodedData}`;
            res.send(responseBody);

        } else if (action === 'get_chunk') {
            // この段階ではget_chunkは実装しない
            res.status(404).send('Not implemented');
        } else {
            res.status(400).send('Invalid action');
        }
    });

    server = http.createServer(app);

    server.listen(port, () => {
        const address = server?.address() as AddressInfo;
        console.log(`Server listening on port ${address.port}`);
        // フロントエンドにステータスを通知
        mainWindow?.webContents.send('server-status', 'running', address.port);
    });

    server.on('error', (error) => {
        console.error('Server error:', error);
        mainWindow?.webContents.send('server-status', 'error', error.message);
    });
}

// サーバーを停止する関数
function stopServer() {
    if (server) {
        server.close(() => {
            console.log('Server stopped.');
            server = null;
            mainWindow?.webContents.send('server-status', 'stopped');
        });
    }
}

// IPC通信のハンドラを設定
ipcMain.on('start-server', (event, port) => {
    startServer(port);
});

ipcMain.on('stop-server', () => {
    stopServer();
});


// --- ここまで追加 ---


app.whenReady().then(() => {
    createWindow();

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});

app.on('window-all-closed', () => {
    stopServer(); // アプリケーション終了時にサーバーを停止
    if (process.platform !== 'darwin') {
        app.quit();
    }
});
