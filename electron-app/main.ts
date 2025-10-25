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
const MAX_PALETTE_COLORS = 256;
const MAX_PALETTES_TO_KEEP = 500;

// --- パレット管理 ---
type Palette = { colors: number[][]; colorsTuple: string };
class PaletteManager {
    private palettes: Map<number, Palette> = new Map();
    private lruKeys: number[] = [];
    private nextPaletteId = 0;

    getOrCreatePalette(colors: number[][]): { id: number; isNew: boolean; palette: number[][] } {
        const colorsTuple = JSON.stringify(colors); // 簡単なハッシュ化
        
        for (const [id, paletteData] of this.palettes.entries()) {
            if (paletteData.colorsTuple === colorsTuple) {
                this.updateLru(id);
                return { id, isNew: false, palette: paletteData.colors };
            }
        }

        const newId = this.nextPaletteId++;
        this.palettes.set(newId, { colors, colorsTuple });
        this.updateLru(newId);

        if (this.lruKeys.length > MAX_PALETTES_TO_KEEP) {
            const oldestKey = this.lruKeys.shift();
            if (oldestKey !== undefined) {
                this.palettes.delete(oldestKey);
            }
        }
        
        return { id: newId, isNew: true, palette: colors };
    }

    private updateLru(id: number) {
        const index = this.lruKeys.indexOf(id);
        if (index > -1) {
            this.lruKeys.splice(index, 1);
        }
        this.lruKeys.push(id);
    }
}
const paletteManager = new PaletteManager();

// RLEエンコードを行うヘルパー関数
function rleEncode<T>(data: T[]): [T, number][] {
    if (data.length === 0) return [];
    const encoded: [T, number][] = [];
    let current = data[0];
    let count = 1;
    for (let i = 1; i < data.length; i++) {
        if (data[i] === current) {
            count++;
        } else {
            encoded.push([current, count]);
            current = data[i];
            count = 1;
        }
    }
    encoded.push([current, count]);
    return encoded;
}


// HTTPサーバーとビデオ処理プロセスのインスタンス
let server: http.Server | null = null;
let ffmpegProcess: ffmpeg.FfmpegCommand | null = null;

// 最新のビデオフレームを保持するバッファ
let frameBuffer: Buffer[] = [];
// 差分比較のために直前のフレームを保持するバッファ
let prevFrameBuffer: Buffer | null = null;
// 送信中のフレームのチャンクを保持するマップ
const IMAGE_CHUNKS: Map<string, string[]> = new Map();

// 再生状態の管理
let isPaused = false;
let currentSeekTime = 0;


let mainWindow: BrowserWindow | null;

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 800,
        height: 600,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
            // preloadスクリプトで'path'などのNode.jsモジュールを使用できるようにするため、サンドボックスを無効にします。
            sandbox: false,
        },
    });

    mainWindow.loadFile(path.join(__dirname, '../index.html'));

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

// --- ここからビデオ処理ロジック ---

// FFmpegを使用してビデオからフレームを抽出する関数
function setupVideoCapture(startTime: number = 0) {
    // 既存のプロセスがあれば停止
    if (ffmpegProcess) {
        ffmpegProcess.kill('SIGKILL');
        ffmpegProcess = null;
    }

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
    if (startTime > 0) {
        ffmpegProcess.inputOptions(`-ss ${startTime}`);
    }
    ffmpegProcess.inputOptions('-re') // ネイティブのフレームレートで読み込む
        .outputOptions([
            '-f', 'rawvideo',      // 生のビデオデータとして出力
            '-pix_fmt', 'rgb24',   // ピクセルフォーマットをRGB24に
            '-s', `${IMG_WIDTH}x${IMG_HEIGHT}` // 解像度を指定
        ])
        .on('start', (commandLine) => console.log(`FFmpeg started: ${commandLine}`))
        .on('error', (err) => {
            // 意図的な停止(kill)によるエラーは無視
            if (err.message.includes('SIGKILL')) return;
            console.error('FFmpeg error:', err.message);
        })
        .on('end', () => {
            console.log('Video ended, looping...');
            // 動画が終了したらループさせる
            if (ffmpegProcess) {
                currentSeekTime = 0;
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
    
    isPaused = false;
    currentSeekTime = 0;

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
            const currentFrame = frameBuffer[0];
            const clientCachedPids = new Set(
                (req.query.cached_pids as string || '').split(',').map(p => parseInt(p, 10)).filter(p => !isNaN(p))
            );

            const candidates: { [key: string]: string | null } = {};

            // 1. フルフレーム (F)
            candidates['F'] = "F|" + currentFrame.toString('base64');

            // 2. 差分 (D)
            if (prevFrameBuffer) {
                const diffs: string[] = [];
                for (let i = 0; i < currentFrame.length; i += 3) {
                    if (currentFrame[i] !== prevFrameBuffer[i] || currentFrame[i+1] !== prevFrameBuffer[i+1] || currentFrame[i+2] !== prevFrameBuffer[i+2]) {
                        const pixelIndex = i / 3;
                        const r = currentFrame[i].toString(16).padStart(2, '0');
                        const g = currentFrame[i+1].toString(16).padStart(2, '0');
                        const b = currentFrame[i+2].toString(16).padStart(2, '0');
                        diffs.push(`${pixelIndex.toString(16)}:${r}${g}${b}`);
                    }
                }
                if (diffs.length > 0) candidates['D'] = "D|" + diffs.join('|');
            }

            // 3. フルRLE (FR)
            const hexColors = [];
            for (let i = 0; i < currentFrame.length; i += 3) {
                const r = currentFrame[i].toString(16).padStart(2, '0');
                const g = currentFrame[i+1].toString(16).padStart(2, '0');
                const b = currentFrame[i+2].toString(16).padStart(2, '0');
                hexColors.push(`${r}${g}${b}`);
            }
            const rleFullData = rleEncode(hexColors);
            if (rleFullData.length > 0) {
                candidates['FR'] = "FR|" + rleFullData.map(([color, count]) => `${color},${count.toString(16)}`).join('|');
            }

            // --- インデックス系エンコード ---
            const uniqueColors = new Map<string, number[]>();
            for (let i = 0; i < currentFrame.length; i += 3) {
                const r = currentFrame[i], g = currentFrame[i+1], b = currentFrame[i+2];
                uniqueColors.set(`${r},${g},${b}`, [r, g, b]);
            }
            const colors = Array.from(uniqueColors.values());

            if (colors.length > 1 && colors.length <= MAX_PALETTE_COLORS) {
                const { id, isNew, palette } = paletteManager.getOrCreatePalette(colors);
                const colorToIndex = new Map(palette.map((c, i) => [c.join(','), i]));
                
                const sendPaletteData = isNew || !clientCachedPids.has(id);
                const palettePayload = sendPaletteData ? palette.map(c => c.map(v => v.toString(16).padStart(2, '0')).join('')).join(',') : "";
                const hexFormat = (n: number) => (colors.length <= 16 ? n.toString(16) : n.toString(16).padStart(2, '0'));

                // 4. インデックス (I)
                const indices: number[] = [];
                for (let i = 0; i < currentFrame.length; i += 3) {
                    const key = `${currentFrame[i]},${currentFrame[i+1]},${currentFrame[i+2]}`;
                    indices.push(colorToIndex.get(key)!);
                }
                candidates['I'] = `I|${id}|${palettePayload}|${indices.map(hexFormat).join('')}`;

                // 5. インデックスRLE (IR)
                const rleIndexedData = rleEncode(indices);
                if (rleIndexedData.length > 0) {
                    candidates['IR'] = `IR|${id}|${palettePayload}|${rleIndexedData.map(([index, count]) => `${hexFormat(index)},${count.toString(16)}`).join('|')}`;
                }

                // 6. 差分インデックス (DI)
                if (prevFrameBuffer) {
                    const diffs: string[] = [];
                    for (let i = 0; i < currentFrame.length; i += 3) {
                         if (currentFrame[i] !== prevFrameBuffer[i] || currentFrame[i+1] !== prevFrameBuffer[i+1] || currentFrame[i+2] !== prevFrameBuffer[i+2]) {
                            const pixelIndex = i / 3;
                            const key = `${currentFrame[i]},${currentFrame[i+1]},${currentFrame[i+2]}`;
                            diffs.push(`${pixelIndex.toString(16)}:${hexFormat(colorToIndex.get(key)!)}`);
                        }
                    }
                    if (diffs.length > 0) candidates['DI'] = `DI|${id}|${palettePayload}|${diffs.join('|')}`;
                }
            }

            // --- 最もデータサイズが小さい形式を選択 ---
            const validCandidates = Object.values(candidates).filter((p): p is string => p !== null);
            if (validCandidates.length === 0) {
                res.status(204).send();
                return;
            }
            const bestPayload = validCandidates.reduce((a, b) => (a.length < b.length ? a : b));
            
            // データをチャンクに分割
            const chunks = [];
            for (let i = 0; i < bestPayload.length; i += CHUNK_SIZE_LIMIT) {
                chunks.push(bestPayload.substring(i, i + CHUNK_SIZE_LIMIT));
            }
            
            const frameId = crypto.randomUUID();
            IMAGE_CHUNKS.set(frameId, chunks);
            
            if (IMAGE_CHUNKS.size > 10) {
                const oldestKey = IMAGE_CHUNKS.keys().next().value;
                if (oldestKey) IMAGE_CHUNKS.delete(oldestKey);
            }

            const totalChunks = chunks.length;
            const responseBody = `${frameId};${totalChunks}`;
            res.send(responseBody);

            prevFrameBuffer = Buffer.from(currentFrame);

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
        prevFrameBuffer = null; // 前フレームバッファもクリア
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

ipcMain.on('toggle-playback', (event, shouldPlay) => {
    if (isPaused === !shouldPlay) return; // 状態が同じなら何もしない

    isPaused = !shouldPlay;
    console.log(`Playback toggled. isPaused: ${isPaused}`);

    if (isPaused) {
        if (ffmpegProcess) {
            ffmpegProcess.kill('SIGKILL');
            ffmpegProcess = null;
        }
    } else {
        if (!ffmpegProcess) {
            setupVideoCapture(currentSeekTime);
        }
    }
});

ipcMain.on('seek-video', (event, time) => {
    console.log(`Seek request to: ${time}`);
    currentSeekTime = time;
    prevFrameBuffer = null; // シーク後は差分が取れないのでリセット

    // 一時停止中でなければ、シーク後に再生を再開
    if (!isPaused) {
        setupVideoCapture(currentSeekTime);
    }
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
