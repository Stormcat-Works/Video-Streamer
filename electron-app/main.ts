// electron-app/main.ts
import { app, BrowserWindow, ipcMain } from 'electron';
import * as path from 'path';
import express from 'express';
import http from 'http';
import { AddressInfo } from 'net';
import crypto from 'crypto';
import ffmpeg from 'fluent-ffmpeg';
import ffmpegStatic from 'ffmpeg-static';
import fs from 'fs';
import { PassThrough } from 'stream';

// --- 定数とグローバル変数 ---
const IMG_WIDTH = 200;
const IMG_HEIGHT = 150;
const CHUNK_SIZE_LIMIT = 4000;

// HTTPサーバーとビデオ処理プロセスのインスタンス
let server: http.Server | null = null;
let ffmpegProcess: ffmpeg.FfmpegCommand | null = null;

// 最新のビデオフレームを保持するバッファ
let frameBuffer: Buffer[] = [];
// 送信中のフレームのチャンクを保持するマップ
const IMAGE_CHUNKS: Map<string, string[]> = new Map();


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

// --- ここからビデオ処理ロジック ---

// FFmpegを使用してビデオからフレームを抽出する関数
function setupVideoCapture() {
    // 実行時の`dist`ディレクトリからの相対パスでビデオファイルを探す
    const videoPath = path.join(__dirname, '../../python-version/video.mp4');

    if (!fs.existsSync(videoPath)) {
        console.error(`Video file not found at: ${videoPath}`);
        mainWindow?.webContents.send('server-status', 'error', `Video not found at ${videoPath}`);
        return;
    }

    // ffmpegのパスを設定
    if (typeof ffmpegStatic !== 'string') {
        console.error('ffmpeg-static path is not a string.');
        mainWindow?.webContents.send('server-status', 'error', 'ffmpeg-static path is invalid');
        return;
    }
    ffmpeg.setFfmpegPath(ffmpegStatic);

    const frameStream = new PassThrough();
    const frameSize = IMG_WIDTH * IMG_HEIGHT * 3;
    let currentFrame = Buffer.alloc(0);

    // ストリームからフレームデータを組み立てる
    frameStream.on('data', (chunk) => {
        currentFrame = Buffer.concat([currentFrame, chunk]);
        while (currentFrame.length >= frameSize) {
            const frame = currentFrame.subarray(0, frameSize);
            currentFrame = currentFrame.subarray(frameSize);
            // 最新のフレームのみをバッファに保持
            frameBuffer = [frame];
        }
    });

    // FFmpegプロセスを開始
    ffmpegProcess = ffmpeg(videoPath);
    ffmpegProcess.inputOptions('-re') // ネイティブのフレームレートで読み込む
        .outputOptions([
            '-f', 'rawvideo',      // 生のビデオデータとして出力
            '-pix_fmt', 'rgb24',   // ピクセルフォーマットをRGB24に
            '-s', `${IMG_WIDTH}x${IMG_HEIGHT}` // 解像度を指定
        ])
        .on('start', () => console.log('FFmpeg processing started.'))
        .on('error', (err) => {
            console.error('FFmpeg error:', err.message);
            // エラーが発生したら再起動してループさせる
            if (ffmpegProcess) {
                setTimeout(setupVideoCapture, 1000);
            }
        })
        .on('end', () => {
            console.log('Video ended, looping...');
            // 動画が終了したら再起動してループさせる
            if (ffmpegProcess) {
                setupVideoCapture();
            }
        });
    ffmpegProcess.pipe(frameStream, { end: true });
}


// サーバーを起動する関数
function startServer(port: number) {
    if (server) {
        console.log('Server is already running.');
        return;
    }

    // ビデオキャプチャを開始
    setupVideoCapture();

    const app = express();

    // Stormworksからのリクエストを処理するエンドポイント
    app.get('/', (req, res) => {
        const action = req.query.action;

        if (action === 'new_frame') {
            if (frameBuffer.length === 0) {
                res.status(503).send('No frame available yet.');
                return;
            }
            const pixels = frameBuffer[0];

            // 'F' (Full) 形式でエンコード
            const encodedData = "F|" + pixels.toString('base64');
            
            // データをチャンクに分割
            const chunks = [];
            for (let i = 0; i < encodedData.length; i += CHUNK_SIZE_LIMIT) {
                chunks.push(encodedData.substring(i, i + CHUNK_SIZE_LIMIT));
            }
            
            const frameId = crypto.randomUUID();
            IMAGE_CHUNKS.set(frameId, chunks);
            
            // 古いチャンクを削除
            if (IMAGE_CHUNKS.size > 10) {
                const oldestKey = IMAGE_CHUNKS.keys().next().value;
                if (oldestKey) {
                    IMAGE_CHUNKS.delete(oldestKey);
                }
            }

            const totalChunks = chunks.length;
            const responseBody = `${frameId};${totalChunks}`;
            res.send(responseBody);

        } else if (action === 'get_chunk') {
            const frameId = req.query.frame_id;
            const chunkQuery = req.query.chunk;

            if (typeof frameId !== 'string' || typeof chunkQuery !== 'string') {
                res.status(400).send('Invalid frame_id or chunk');
                return;
            }
            
            const chunkIndex = parseInt(chunkQuery, 10);
            const chunks = IMAGE_CHUNKS.get(frameId);
            if (chunks && chunkIndex >= 0 && chunkIndex < chunks.length) {
                const responseBody = `${frameId};${chunkIndex};${chunks[chunkIndex]}`;
                res.send(responseBody);
            } else {
                res.status(404).send('Chunk not found');
            }
        } else {
            res.status(400).send('Invalid action');
        }
    });

    server = http.createServer(app);

    server.listen(port, () => {
        const address = server?.address() as AddressInfo;
        console.log(`Server listening on port ${address.port}`);
        mainWindow?.webContents.send('server-status', 'running', address.port);
    });

    server.on('error', (error) => {
        console.error('Server error:', error);
        mainWindow?.webContents.send('server-status', 'error', error.message);
    });
}

// サーバーを停止する関数
function stopServer() {
    // FFmpegプロセスを停止
    if (ffmpegProcess) {
        ffmpegProcess.kill('SIGKILL');
        ffmpegProcess = null;
        frameBuffer = [];
        console.log('FFmpeg process stopped.');
    }

    // HTTPサーバーを停止
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
