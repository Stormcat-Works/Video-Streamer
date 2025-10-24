--[[
  Pythonサーバーから画像データを受信し、モニターへ描画するスクリプト。
  
  ## 特徴
  - new_frame最適化: 初回リクエストに応答を含めることで高速化
  - 6モード対応: フル(F)/差分(D)/インデックス(I)/差分インデックス(DI)に加え、
                  RLE圧縮されたフル(FR)/インデックス(IR)を自動デコード
  - 1フレーム先読み: 描画とダウンロードを並行処理
  - [改良] LRUパレットキャッシュ: クライアントの状態をサーバーに送り、キャッシュを同期
  - デバッグ表示: FPS、CPS(Chunks Per Second)、更新モードを平滑化して表示
]]

-- =============================================================================
-- [[ 設定 ]]
-- =============================================================================
-- Pythonサーバーのポート番号
local SERVER_PORT = 8000
-- 画像の解像度
local IMAGE_WIDTH = 64
local IMAGE_HEIGHT = 64
-- パレットキャッシュの最大保持数 (Python側と合わせる)
local MAX_PALETTES_TO_KEEP = 500


-- =============================================================================
-- [[ 定数とグローバル変数 ]]
-- =============================================================================
local TOTAL_PIXELS = IMAGE_WIDTH * IMAGE_HEIGHT
local REQUEST_TIMEOUT_TICKS = 30
local ALPHA_FPS = 0.4
local ALPHA_CPS = 0.182
local display_pixel_data, download_pixel_data = {}, {}
for i = 1, TOTAL_PIXELS * 3 do
    display_pixel_data[i] = 0; download_pixel_data[i] = 0
end
local is_requesting_new_frame = false
local pause_sending = false
local last_received_tick = 0
local next_chunk_to_request = 0
local downloading_frame_id, downloading_total_chunks, downloaded_chunks_count = nil, 0, 0
local chunks_buffer = {}
local g_tick_count = 0
local last_chunk_tick = 0
local last_frame_tick = 0
local ema_cps, ema_fps = 0.0, 0.0
local g_last_frame_info = "N/A"
local g_palettes = {}
local g_palette_lru_keys = {}

-- =============================================================================
-- [[ ヘルパー関数 ]]
-- =============================================================================
local DECODE_MAP = {}
do local b = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/" for i = 1, #b do DECODE_MAP[b:sub(i,i)] = i-1 end end
local function decode_base64_pixel(q)
    local v1,v2,v3,v4 = DECODE_MAP[q:sub(1,1)], DECODE_MAP[q:sub(2,2)], DECODE_MAP[q:sub(3,3)], DECODE_MAP[q:sub(4,4)]
    if not (v1 and v2 and v3 and v4) then return end
    local n = v1*262144+v2*4096+v3*64+v4; return math.floor(n/65536), math.floor(n%65536/256), n%256
end

local function manage_palette_cache(palette_id)
    for i, pid in ipairs(g_palette_lru_keys) do if pid == palette_id then table.remove(g_palette_lru_keys, i); break end end
    table.insert(g_palette_lru_keys, palette_id)
    if #g_palette_lru_keys > MAX_PALETTES_TO_KEEP then
        local oldest_pid = table.remove(g_palette_lru_keys, 1)
        if oldest_pid then g_palettes[oldest_pid] = nil end
    end
end

local function handle_frame_completion()
    local frame_delta = g_tick_count - last_frame_tick
    if frame_delta > 0 then
        local current_fps = 60 / frame_delta
        ema_fps = (last_frame_tick == 0) and current_fps or (current_fps * ALPHA_FPS) + (ema_fps * (1 - ALPHA_FPS))
    end
    last_frame_tick = g_tick_count
    display_pixel_data, download_pixel_data = download_pixel_data, display_pixel_data
    downloading_frame_id = nil
end

-- =============================================================================
-- [[ Stormworks コールバック関数 ]]
-- =============================================================================
function onTick()
    g_tick_count = g_tick_count + 1
    if not is_requesting_new_frame and downloading_frame_id == nil then
        is_requesting_new_frame = true
        
        -- [新規] キャッシュ済みのパレットIDのリストを作成
        local cached_pids = {}
        for pid, _ in pairs(g_palettes) do
            table.insert(cached_pids, tostring(pid))
        end
        
        -- [修正] URLにキャッシュ情報を付与
        local url = "/?action=new_frame"
        if #cached_pids > 0 then
            url = url .. "&cached_pids=" .. table.concat(cached_pids, ",")
        end
        
        async.httpGet(SERVER_PORT, url)
    end
    if g_tick_count - last_received_tick > REQUEST_TIMEOUT_TICKS then pause_sending = true end
    if downloading_frame_id and not pause_sending and next_chunk_to_request < downloading_total_chunks then
        async.httpGet(SERVER_PORT, string.format("/?action=get_chunk&frame_id=%s&chunk=%d", downloading_frame_id, next_chunk_to_request))
        next_chunk_to_request = next_chunk_to_request + 1
    end
end

function httpReply(port, request_body, response_body)
    pause_sending, last_received_tick = false, g_tick_count
    local delta_ticks = g_tick_count - last_chunk_tick
    if delta_ticks > 0 then
        ema_cps = (last_chunk_tick==0) and (60/delta_ticks) or ((60/delta_ticks)*ALPHA_CPS)+(ema_cps*(1-ALPHA_CPS))
    end
    last_chunk_tick = g_tick_count

    local response_parts = {}
    for p in string.gmatch(response_body, "[^;]+") do table.insert(response_parts, p) end
    local request_action = string.match(request_body, "/?action=([^&]+)")

    if request_action == "new_frame" then
        is_requesting_new_frame = false
        if #response_parts >= 2 then
            downloading_frame_id, downloading_total_chunks = response_parts[1], tonumber(response_parts[2])
            next_chunk_to_request, downloaded_chunks_count, chunks_buffer = 0, 0, {}
            for i = 1, #display_pixel_data do download_pixel_data[i] = display_pixel_data[i] end
            if #response_parts >= 3 and downloading_total_chunks > 0 then
                chunks_buffer[1] = response_parts[3]; downloaded_chunks_count = 1; next_chunk_to_request = 1
                if downloading_total_chunks == 1 then goto process_frame end
            end
        end
    elseif request_action == "get_chunk" then
        if #response_parts == 3 then
            local frame_id, chunk_index = response_parts[1], tonumber(response_parts[2])
            if frame_id ~= downloading_frame_id then return end
            chunks_buffer[chunk_index + 1] = response_parts[3]; downloaded_chunks_count = downloaded_chunks_count + 1
        end
    end

    ::process_frame::
    if downloading_frame_id and downloaded_chunks_count >= downloading_total_chunks then
        local full_data = table.concat(chunks_buffer)
        local update_type, payload
        if #full_data > 2 and full_data:sub(3, 3) == "|" then
            update_type = full_data:sub(1, 2); payload = full_data:sub(4)
        elseif #full_data > 1 and full_data:sub(2, 2) == "|" then
            update_type = full_data:sub(1, 1); payload = full_data:sub(3)
        else
            downloading_frame_id = nil; return
        end
        
        -- (デコード処理は変更なし)
        if update_type == "F" then
            g_last_frame_info = "FULL"; local i = 0
            while #payload >= 4 and i < TOTAL_PIXELS do
                local r,g,b = decode_base64_pixel(payload:sub(1,4))
                if r then local base = i*3+1; download_pixel_data[base],download_pixel_data[base+1],download_pixel_data[base+2] = r,g,b; i=i+1 end
                payload = payload:sub(5)
            end
        elseif update_type == "FR" then
            g_last_frame_info = "FULL_RLE"; local pixel_cursor = 0
            for part in string.gmatch(payload, "[^|]+") do
                local color_hex, count_hex = string.match(part, "([^,]+),(.+)")
                if color_hex and count_hex then
                    local r,g,b = tonumber(color_hex:sub(1,2),16),tonumber(color_hex:sub(3,4),16),tonumber(color_hex:sub(5,6),16)
                    local count = tonumber(count_hex, 16)
                    if r and count then for _=1,count do if pixel_cursor>=TOTAL_PIXELS then break end local base=pixel_cursor*3+1; download_pixel_data[base],download_pixel_data[base+1],download_pixel_data[base+2]=r,g,b; pixel_cursor=pixel_cursor+1 end end
                end
                if pixel_cursor >= TOTAL_PIXELS then break end
            end
        elseif update_type == "D" then
            g_last_frame_info = "DIFF"
            for p in string.gmatch(payload, "[^|]+") do
                local idx_h, c_h = string.match(p, "([^:]+):(.+)"); if idx_h then local idx,r,g,b = tonumber(idx_h,16),tonumber(c_h:sub(1,2),16),tonumber(c_h:sub(3,4),16),tonumber(c_h:sub(5,6),16); local base=idx*3+1; download_pixel_data[base],download_pixel_data[base+1],download_pixel_data[base+2]=r,g,b end
            end
        elseif update_type == "I" or update_type == "DI" or update_type == "IR" then
            local palette_id_str, palette_data_str, indices_payload = string.match(payload, "([^|]+)|([^|]*)|(.+)")
            if palette_id_str then
                local palette_id = tonumber(palette_id_str)
                if palette_data_str and #palette_data_str > 0 then
                    local new_colors = {}; for color_hex in string.gmatch(palette_data_str, "[^,]+") do table.insert(new_colors, {tonumber(color_hex:sub(1,2),16), tonumber(color_hex:sub(3,4),16), tonumber(color_hex:sub(5,6),16)}) end
                    g_palettes[palette_id] = { colors = new_colors }
                end
                if g_palettes[palette_id] then
                    manage_palette_cache(palette_id); local current_palette = g_palettes[palette_id].colors
                    if update_type == "I" then
                        g_last_frame_info = "IDX:" .. palette_id; local num_colors=#current_palette; local chars_per_index=(num_colors<=16) and 1 or 2
                        for i=0,TOTAL_PIXELS-1 do local start_pos=i*chars_per_index+1; if start_pos+chars_per_index-1<=#indices_payload then local idx=tonumber(indices_payload:sub(start_pos,start_pos+chars_per_index-1),16); if idx and current_palette[idx+1] then local color=current_palette[idx+1]; local base=i*3+1; download_pixel_data[base],download_pixel_data[base+1],download_pixel_data[base+2]=color[1],color[2],color[3] end end end
                    elseif update_type == "IR" then
                        g_last_frame_info = "IDX_RLE:" .. palette_id; local pixel_cursor=0
                        for part in string.gmatch(indices_payload, "[^|]+") do
                            local index_hex, count_hex = string.match(part, "([^,]+),(.+)"); if index_hex and count_hex then local palette_idx=tonumber(index_hex,16); local count=tonumber(count_hex,16); if palette_idx and count and current_palette[palette_idx+1] then local color=current_palette[palette_idx+1]; for _=1,count do if pixel_cursor>=TOTAL_PIXELS then break end local base=pixel_cursor*3+1; download_pixel_data[base],download_pixel_data[base+1],download_pixel_data[base+2]=color[1],color[2],color[3]; pixel_cursor=pixel_cursor+1 end end end
                            if pixel_cursor>=TOTAL_PIXELS then break end
                        end
                    elseif update_type == "DI" then
                        g_last_frame_info = "D_IDX:" .. palette_id
                        for part in string.gmatch(indices_payload, "[^|]+") do
                            local pos_h, idx_h=string.match(part, "([^:]+):(.+)"); if pos_h and idx_h then local pixel_idx,palette_idx=tonumber(pos_h,16),tonumber(idx_h,16); if pixel_idx and palette_idx and current_palette[palette_idx+1] then local color=current_palette[palette_idx+1]; local base=pixel_idx*3+1; download_pixel_data[base],download_pixel_data[base+1],download_pixel_data[base+2]=color[1],color[2],color[3] end end
                        end
                    end
                end
            end
        end
        handle_frame_completion()
    end
end

function onDraw()
    screen.setColor(50, 50, 50); screen.drawClear()
    for y = 0, IMAGE_HEIGHT - 1 do
        for x = 0, IMAGE_WIDTH - 1 do
            local i = (y*IMAGE_WIDTH+x)*3+1; local r,g,b = display_pixel_data[i],display_pixel_data[i+1],display_pixel_data[i+2]
            if r then screen.setColor(r, g, b); screen.drawRectF(x, y, 1, 1) end
        end
    end
    local w, h = screen.getWidth(), screen.getHeight()
    screen.setColor(0,0,0,150); screen.drawRectF(w-90,h-35,90,35)
    screen.setColor(255,255,255); local y_pos=h-30; screen.drawText(w-85,y_pos,string.format("FPS: %.2f",ema_fps))
    y_pos=y_pos+8; screen.drawText(w-85,y_pos,string.format("CPS: %.2f",ema_cps))
    y_pos=y_pos+8; screen.drawText(w-85,y_pos,string.format("MODE: %s",g_last_frame_info))
end