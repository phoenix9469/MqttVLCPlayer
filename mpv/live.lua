-- 라이브 방송을 항상 맨 앞(실시간)에서 보이게 하는 mpv 스크립트
-- 버퍼링이나 일시정지로 실시간보다 max_lag초 넘게 뒤처지면 바로 맨 앞으로 이동한다.
-- 받아 둔 분량(캐시)보다 오래 멈춰 있었으면 캐시 끝도 실시간이 아니므로 방송을 다시 연다.
-- script-message live-edge: 지금 바로 맨 앞으로
-- 옵션: --script-opts=live-max_lag=8
local options = require "mp.options"

local opts = {
    max_lag = 8,        -- 이만큼(초) 넘게 뒤처지면 맨 앞으로
    reload_after = 50,  -- 이만큼(초) 넘게 일시정지했다가 다시 재생하면 방송을 다시 열기
}
options.read_options(opts, "live")

local paused_at = nil

-- 받아 둔 분량의 끝과 지금 위치의 차이(초)
local function lag()
    local cache_end = mp.get_property_number("demuxer-cache-time")
    local pos = mp.get_property_number("time-pos")
    if not cache_end or not pos then
        return nil
    end
    return cache_end - pos, cache_end
end

local function reload()
    local path = mp.get_property("path")
    if path then
        mp.msg.info("reloading live stream to get back to real time")
        mp.commandv("loadfile", path, "replace")
    end
end

local function jump_to_edge(reason)
    local behind, cache_end = lag()
    if not behind then
        return
    end
    mp.msg.info(string.format("%.1fs behind live (%s), jumping to the front", behind, reason))
    -- 끝에 딱 붙이면 바로 다시 버퍼링하므로 1.5초 여유
    mp.commandv("seek", tostring(cache_end - 1.5), "absolute+keyframes")
end

local function check()
    if mp.get_property_bool("pause") or mp.get_property_bool("seeking") then
        return
    end
    local behind = lag()
    if behind and behind > opts.max_lag then
        jump_to_edge("auto")
    end
end

mp.observe_property("pause", "bool", function(_, paused)
    if paused then
        paused_at = mp.get_time()
    elseif paused_at then
        local long = mp.get_time() - paused_at > opts.reload_after
        paused_at = nil
        if long then
            reload()
        else
            check()
        end
    end
end)

mp.register_script_message("live-edge", function()
    if mp.get_property_bool("pause") then
        mp.set_property_bool("pause", false)  -- 다시 재생하면서 위의 처리로 맨 앞으로
    else
        jump_to_edge("button")
    end
end)

mp.add_periodic_timer(2, check)
