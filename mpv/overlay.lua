-- 화면 오른쪽 위에 디스코드 임베드 모양의 반투명 알림창을 띄우는 mpv 스크립트
-- script-message overlay-show <JSON>: 알림창 표시 (새 알림이 오면 이전 알림을 바꿈)
--   JSON: {"title", "message", "color", "fields": [{"name", "value", "inline"}], "footer", "duration"}
--   duration: 표시 시간(초), 0 이하면 overlay-hide 전까지 계속 표시
-- script-message overlay-hide: 알림창 닫기
-- 옵션: --script-opts=overlay-font=Noto Sans,overlay-file=첫 알림 JSON 파일,overlay-quit_when_done=yes
--       (quit_when_done: 알림창이 닫히면 mpv 종료. 알림만 띄우려고 따로 실행한 mpv에서 사용)
local options = require "mp.options"
local utils = require "mp.utils"

local opts = {
    font = "",
    file = "",
    quit_when_done = false,
    duration = 15,
}
options.read_options(opts, "overlay")

local overlay = mp.create_osd_overlay("ass-events")
local current = nil   -- 표시 중인 알림 (JSON을 읽은 table)
local hide_timer = nil

-- 디스코드 다크 테마 색 (RRGGBB)
local BG = "2B2D31"
local TEXT = "FFFFFF"
local SUBTEXT = "B5BAC1"
local MUTED = "949BA4"
local DEFAULT_STRIPE = "1E1F22"
local BG_ALPHA = "30"   -- 00 불투명 ~ FF 투명

local function ass_color(rgb)
    return "&H" .. rgb:sub(5, 6) .. rgb:sub(3, 4) .. rgb:sub(1, 2) .. "&"
end

-- "#RRGGBB", "RRGGBB", 숫자(0xRRGGBB) → "RRGGBB"
local function parse_color(value)
    if type(value) == "number" then
        return string.format("%06X", math.floor(value) % 0x1000000)
    end
    if type(value) == "string" then
        local hex = value:match("^#?(%x%x%x%x%x%x)$")
        if hex then
            return hex:upper()
        end
        local num = tonumber(value)
        if num then
            return parse_color(num)
        end
    end
    return nil
end

local function ass_escape(text)
    return (text:gsub("\\", "\\\\"):gsub("{", "\\{"):gsub("}", "\\}"))
end

-- UTF-8 문자열을 글자 단위로 나눔
local function chars(text)
    local list = {}
    for ch in text:gmatch("[%z\1-\127\194-\244][\128-\191]*") do
        list[#list + 1] = ch
    end
    return list
end

-- 글자 폭 (글자 크기 대비 대략값): 한글·한자 등 전각은 0.92, 공백은 0.3, 그 밖의 반각은 0.55
local function char_width(ch)
    local b = ch:byte(1)
    if ch == " " then
        return 0.3
    elseif b >= 0xE1 then
        return 0.92
    elseif b >= 0xC0 then
        return 0.6
    end
    return 0.55
end

-- 폭 max_em(글자 크기 배수)에 맞춰 줄 나누기. "**"(굵게 표시)는 폭 0으로 셈
local function wrap(text, max_em)
    local lines = {}
    for paragraph in (text .. "\n"):gmatch("(.-)\r?\n") do
        local list = chars(paragraph)
        local line, width = {}, 0
        local last_space = nil
        local i = 1
        while i <= #list do
            local ch = list[i]
            if ch == "*" and list[i + 1] == "*" then
                line[#line + 1] = "**"
                i = i + 2
            else
                local w = char_width(ch)
                if width + w > max_em and #line > 0 then
                    if ch == " " then
                        -- 줄 끝 공백은 버림
                        lines[#lines + 1] = table.concat(line)
                        line, width, last_space = {}, 0, nil
                        i = i + 1
                    elseif last_space then
                        -- 마지막 공백에서 나누고 나머지는 다음 줄로
                        local rest = {}
                        for j = last_space + 1, #line do
                            rest[#rest + 1] = line[j]
                        end
                        local head = {}
                        for j = 1, last_space - 1 do
                            head[#head + 1] = line[j]
                        end
                        lines[#lines + 1] = table.concat(head)
                        line, width, last_space = rest, 0, nil
                        for _, c in ipairs(rest) do
                            width = width + (c == "**" and 0 or char_width(c))
                        end
                    else
                        lines[#lines + 1] = table.concat(line)
                        line, width = {}, 0
                    end
                else
                    if ch == " " then
                        last_space = #line + 1
                    end
                    line[#line + 1] = ch
                    width = width + w
                    i = i + 1
                end
            end
        end
        lines[#lines + 1] = table.concat(line)
    end
    return lines
end

-- 줄 폭 (글자 크기 배수)
local function line_width(line)
    local w = 0
    for _, ch in ipairs(chars((line:gsub("%*%*", "")))) do
        w = w + char_width(ch)
    end
    return w
end

-- 모서리마다 반지름을 지정한 둥근 사각형 (ASS 그리기 명령)
local function rounded_rect(x, y, w, h, tl, tr, br, bl)
    local k = 0.45  -- 원에 가까운 베지어 곡선 비율
    local function f(n)
        return string.format("%d", math.floor(n + 0.5))
    end
    local p = {}
    p[#p + 1] = "m " .. f(x + tl) .. " " .. f(y)
    p[#p + 1] = "l " .. f(x + w - tr) .. " " .. f(y)
    if tr > 0 then
        p[#p + 1] = "b " .. f(x + w - tr * k) .. " " .. f(y) .. " " .. f(x + w) .. " " .. f(y + tr * k) .. " " .. f(x + w) .. " " .. f(y + tr)
    end
    p[#p + 1] = "l " .. f(x + w) .. " " .. f(y + h - br)
    if br > 0 then
        p[#p + 1] = "b " .. f(x + w) .. " " .. f(y + h - br * k) .. " " .. f(x + w - br * k) .. " " .. f(y + h) .. " " .. f(x + w - br) .. " " .. f(y + h)
    end
    p[#p + 1] = "l " .. f(x + bl) .. " " .. f(y + h)
    if bl > 0 then
        p[#p + 1] = "b " .. f(x + bl * k) .. " " .. f(y + h) .. " " .. f(x) .. " " .. f(y + h - bl * k) .. " " .. f(x) .. " " .. f(y + h - bl)
    end
    p[#p + 1] = "l " .. f(x) .. " " .. f(y + tl)
    if tl > 0 then
        p[#p + 1] = "b " .. f(x) .. " " .. f(y + tl * k) .. " " .. f(x + tl * k) .. " " .. f(y) .. " " .. f(x + tl) .. " " .. f(y)
    end
    return table.concat(p, " ")
end

local function shape(path, rgb, alpha)
    return string.format("{\\an7\\pos(0,0)\\bord0\\shad0\\1c%s\\1a&H%s&\\p1}%s{\\p0}", ass_color(rgb), alpha, path)
end

local function render()
    if not current then
        overlay:remove()
        return
    end
    local width, height = mp.get_osd_size()
    if not width or width <= 0 or height <= 0 then
        return
    end
    overlay.res_x = width
    overlay.res_y = height

    local s = height / 1080
    local margin = math.floor(40 * s)
    local pad = math.floor(26 * s)
    local stripe = math.floor(8 * s)
    local box_w = math.min(width - 2 * margin, math.floor(820 * s))
    local cw = box_w - stripe - 2 * pad
    local font = opts.font ~= "" and ("\\fn" .. opts.font) or ""

    local title = current.title and tostring(current.title) or ""
    local message = current.message and tostring(current.message) or ""
    local fields = type(current.fields) == "table" and current.fields or {}
    local footer = current.footer and tostring(current.footer) or ""
    footer = (footer ~= "" and (footer .. "  •  ") or "") .. os.date("%H:%M", current.received)

    -- 필드가 없으면 상자 폭을 글 길이에 맞춤 (짧은 알림이 넓은 상자에 뜨지 않게)
    if #fields == 0 then
        local need = 0
        for _, item in ipairs({ { title, 38 }, { message, 32 }, { footer, 24 } }) do
            local size = math.floor(item[2] * s)
            for _, line in ipairs(wrap(item[1], cw / size)) do
                need = math.max(need, line_width(line) * size)
            end
        end
        cw = math.max(math.floor(360 * s), math.min(cw, math.ceil(need + 4 * s)))
        box_w = cw + stripe + 2 * pad
    end
    local x0 = width - margin - box_w
    local y0 = margin
    local cx = x0 + stripe + pad

    local events = {}
    local y = y0 + pad

    -- 한 덩어리의 글을 줄 단위로 그림. "**" 사이는 굵게
    local function text_block(text, x, max_w, size, rgb, bold)
        local line_h = math.floor(size * 1.3)
        local bold_on = false
        local lines = wrap(text, max_w / size)
        for i, line in ipairs(lines) do
            -- 줄 시작 굵게 상태: 앞 줄에서 "**"가 열린 채로 이어진 경우 반영
            local start = (bold or bold_on) and "\\b1" or "\\b0"
            local body = ass_escape(line):gsub("%*%*", function()
                bold_on = not bold_on
                return (bold or bold_on) and "{\\b1}" or "{\\b0}"
            end)
            events[#events + 1] = string.format("{\\an7\\pos(%d,%d)%s\\fs%d%s\\bord0\\shad0\\1c%s\\q2}%s",
                x, y + (i - 1) * line_h, font, size, start, ass_color(rgb), body)
        end
        return #lines * line_h
    end

    local stripe_rgb = parse_color(current.color) or DEFAULT_STRIPE

    if title ~= "" then
        y = y + text_block(title, cx, cw, math.floor(38 * s), TEXT, true)
        if message ~= "" then
            y = y + math.floor(8 * s)
        end
    end
    if message ~= "" then
        y = y + text_block(message, cx, cw, math.floor(32 * s), TEXT, false)
    end

    -- 필드: inline이 연속이면 한 줄에 최대 3칸 (디스코드와 같은 배치)
    local rows, row = {}, {}
    for _, f in ipairs(fields) do
        if type(f) == "table" then
            if f.inline then
                row[#row + 1] = f
                if #row == 3 then
                    rows[#rows + 1] = row
                    row = {}
                end
            else
                if #row > 0 then
                    rows[#rows + 1] = row
                    row = {}
                end
                rows[#rows + 1] = { f }
            end
        end
    end
    if #row > 0 then
        rows[#rows + 1] = row
    end
    if #rows > 0 and (title ~= "" or message ~= "") then
        y = y + math.floor(14 * s)
    end
    local gap = math.floor(20 * s)
    for _, r in ipairs(rows) do
        local cols = (#r == 1 and not r[1].inline) and 1 or 3
        local col_w = math.floor((cw - (cols - 1) * gap) / cols)
        local row_h = 0
        for i, f in ipairs(r) do
            local x = cx + (i - 1) * (col_w + gap)
            local top = y
            local name = f.name ~= nil and tostring(f.name) or ""
            local value = f.value == nil and "null" or tostring(f.value)
            local h = 0
            if name ~= "" then
                h = h + text_block(name, x, col_w, math.floor(26 * s), SUBTEXT, true)
                y = top + h
            end
            h = h + text_block(value, x, col_w, math.floor(30 * s), TEXT, false)
            y = top
            row_h = math.max(row_h, h)
        end
        y = y + row_h + math.floor(10 * s)
    end

    -- 바닥글과 시각
    y = y + math.floor(10 * s)
    y = y + text_block(footer, cx, cw, math.floor(24 * s), MUTED, false)

    local box_h = y - y0 + pad
    local radius = math.floor(12 * s)
    local corner = math.min(radius, stripe)
    -- 반투명이라 겹치면 진해지므로 색 띠와 배경을 겹치지 않게 나눠 그림
    table.insert(events, 1, shape(rounded_rect(x0 + stripe, y0, box_w - stripe, box_h, 0, radius, radius, 0), BG, BG_ALPHA))
    table.insert(events, 1, shape(rounded_rect(x0, y0, stripe, box_h, corner, 0, 0, corner), stripe_rgb, "00"))

    overlay.data = table.concat(events, "\n")
    overlay:update()
end

local function hide()
    if hide_timer then
        hide_timer:kill()
        hide_timer = nil
    end
    current = nil
    overlay:remove()
    if opts.quit_when_done then
        mp.command("quit")
    end
end

local function show(json)
    local data = utils.parse_json(json or "")
    if type(data) ~= "table" then
        -- JSON이 아니면 글 그대로
        data = { message = json or "" }
    end
    data.received = os.time()
    current = data
    if hide_timer then
        hide_timer:kill()
        hide_timer = nil
    end
    local duration = tonumber(data.duration) or opts.duration
    if duration > 0 then
        hide_timer = mp.add_timeout(duration, hide)
    end
    render()
end

mp.register_script_message("overlay-show", show)
mp.register_script_message("overlay-hide", hide)
mp.observe_property("osd-dimensions", "native", render)

-- 알림만 띄우려고 따로 실행한 경우: 첫 알림은 파일로 받음 (IPC보다 스크립트가 늦게 뜰 수 있어서)
if opts.file ~= "" then
    local f = io.open(opts.file, "r")
    if f then
        local json = f:read("*a")
        f:close()
        os.remove(opts.file)
        show(json)
    elseif opts.quit_when_done then
        mp.command("quit")
    end
end
