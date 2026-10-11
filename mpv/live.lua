-- 라이브 방송을 항상 맨 앞(실시간)에서 보이게 하는 mpv 스크립트
-- 받아 둔 끝(가장 최근 장면)보다 많이 뒤처지면 맨 앞으로 이동한다.
-- 라이브는 몇 초짜리 조각으로 오므로(유튜브는 보통 5초), 맨 앞에 딱 붙이면 다음 조각을 기다리느라
-- 버벅인다. 그래서 조각 길이를 지켜보고 그 2배만큼 여유를 둔 곳을 "맨 앞"으로 삼는다.
-- 받아 둔 분량(캐시)보다 오래 멈춰 있었으면 캐시 끝도 실시간이 아니므로 방송을 다시 연다.
-- script-message live-edge: 지금 바로 맨 앞으로
-- 옵션: --script-opts=live-max_lag=10
local options = require "mp.options"

local opts = {
    max_lag = 10,       -- 맨 앞(여유 포함)보다 이만큼(초) 넘게 뒤처지면 맨 앞으로
    reload_after = 50,  -- 이만큼(초) 넘게 일시정지했다가 다시 재생하면 방송을 다시 열기
}
options.read_options(opts, "live")

local paused_at = nil
local segment = 2       -- 지금까지 본 가장 긴 조각 길이(초), 받은 끝이 한 번에 늘어난 양으로 추정
local last_end = nil

-- 받아 둔 분량의 끝과 지금 위치의 차이(초)
local function lag()
    local cache_end = mp.get_property_number("demuxer-cache-time")
    local pos = mp.get_property_number("time-pos")
    if not cache_end or not pos then
        return nil
    end
    return cache_end - pos, cache_end
end

-- 맨 앞에서 둘 여유: 조각 2개 + 1초
local function margin()
    return segment * 2 + 1
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
    local target = cache_end - margin()
    if target <= mp.get_property_number("time-pos", 0) + 0.5 then
        return  -- 이미 맨 앞
    end
    mp.msg.info(string.format("%.1fs behind live (%s), jumping to %.1fs before the newest part",
        behind, reason, margin()))
    mp.commandv("seek", tostring(target), "absolute+keyframes")
end

local function check()
    local behind, cache_end = lag()
    if not behind then
        return
    end
    -- 받은 끝이 한 번에 늘어난 양 = 조각 길이 (너무 큰 값은 처음 받을 때 몰아 받은 것이라 제외)
    if last_end and cache_end > last_end then
        local step = cache_end - last_end
        if step <= 15 and step > segment then
            segment = step
        end
    end
    last_end = cache_end
    -- 앱(웹 UI)이 "실시간보다 몇 초 늦음"을 계산할 때 여유분을 빼도록 알려 줌
    mp.set_property_native("user-data/live/margin", margin())
    if mp.get_property_bool("pause") or mp.get_property_bool("seeking") then
        return
    end
    if behind > margin() + opts.max_lag then
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

mp.add_periodic_timer(1, check)
