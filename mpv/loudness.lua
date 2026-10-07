-- 영상별 음량 보정: 앱(loudness.py)이 측정해 둔 보정값(dB)을 영상 전체에 같은 크기로 적용하는 mpv 스크립트
-- 옵션: --script-opts=loudness-file=/path/loudness-gains.json  ({"영상 경로": 보정값 dB, ...})
local options = require "mp.options"
local utils = require "mp.utils"

local opts = {
    file = "",
}
options.read_options(opts, "loudness")

local function read_gains()
    local f = io.open(opts.file, "r")
    if not f then
        return {}
    end
    local text = f:read("*a")
    f:close()
    return utils.parse_json(text) or {}
end

-- 영상을 열 때마다 파일을 다시 읽어서 재생 중에 끝난 측정 결과도 반영
mp.add_hook("on_load", 50, function()
    local path = mp.get_property("path")
    local gain = read_gains()[path]
    if type(gain) ~= "number" or math.abs(gain) < 0.1 then
        return
    end
    local filter = string.format("volume=%.2fdB", gain)
    if gain > 0 then
        -- 키운 소리가 찢어지지 않도록 최고 음량만 제한
        filter = filter .. ",alimiter=limit=0.95:level=false"
    end
    local base = mp.get_property("options/af", "")
    local af = "lavfi=[" .. filter .. "]"
    if base ~= "" then
        af = base .. "," .. af
    end
    -- file-local: 이 영상이 끝나면 원래 설정으로 돌아감
    mp.set_property("file-local-options/af", af)
    mp.msg.info(string.format("loudness gain %+.1f dB", gain))
end)
