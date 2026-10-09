-- 재생 화면 좌측 상단에 기기의 현재 시각을 표시하는 mpv 스크립트
-- 옵션: --script-opts=clock-format=%H:%M,clock-size=0,clock-font=Noto Sans
--       (size는 px, 0이면 화면 높이의 5%. font가 비어 있으면 mpv 기본 OSD 글꼴)
--       clock-enabled=no: 시계를 표시하지 않음 (수동 재생에서 --script-opts-append로 지정)
local options = require "mp.options"

local opts = {
    format = "%H:%M",
    size = 0,
    font = "",
    x = 30,
    y = 20,
    enabled = true,
}
options.read_options(opts, "clock")
if not opts.enabled then
    return
end

local overlay = mp.create_osd_overlay("ass-events")

local function ass_escape(text)
    return (text:gsub("\\", "\\\\"):gsub("{", "\\{"):gsub("}", "\\}"))
end

local function update()
    local width, height = mp.get_osd_size()
    if not width or height <= 0 then
        return
    end
    -- OSD 크기를 그대로 좌표계로 써서 size, x, y를 화면 픽셀 단위로 맞춘다
    overlay.res_x = width
    overlay.res_y = height
    local size = opts.size > 0 and opts.size or math.floor(height * 0.05)
    local font = opts.font ~= "" and ("\\fn" .. opts.font) or ""
    overlay.data = string.format("{\\an7\\pos(%d,%d)%s\\fs%d\\bord2\\shad0\\1c&HFFFFFF&\\3c&H000000&}%s",
        opts.x, opts.y, font, size, ass_escape(os.date(opts.format)))
    overlay:update()
end

-- 분이 바뀌는 순간을 놓치지 않도록 1초마다 갱신
mp.add_periodic_timer(1, update)
mp.observe_property("osd-dimensions", "native", update)
