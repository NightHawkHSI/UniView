// UniView loading screen (see loading_screen.py). Everything that moves is worked out here, in Qt's JavaScript
// engine, so frames never wait for Python: the loader thread holds Python's GIL for long stretches while it
// indexes, and a Python paintEvent stuttered with it. Python only sends events through `bridge`.
import QtQuick

Rectangle {
    id: root
    color: th.BG

    // ---- settings from Python (bridge "config")
    property var th: ({BG: "#16171a", TEXT: "#e4e6ea", MUTED: "#8b9099", FAINT: "#5c6068", ACCENT: "#4c8dff",
                      RAISED: "#25272c", BORDER_STRONG: "#41444c"})
    property var kindStyle: ({})
    property var kindLabels: ({})
    property var kindOrder: []
    property string iconUrl: ""
    property bool running: false
    property var frameLog: null   // tests: set to [] to record the gap before each frame (seconds)

    // ---- state
    property var st: fresh("")

    readonly property real maxStages: 5
    readonly property real slowHint: 8.0
    readonly property real diveTime: 0.6
    readonly property real diveGap: 0.07
    readonly property real sparkTime: 0.8
    readonly property real crumbTime: 0.9
    readonly property int maxSparks: 140
    readonly property int maxCrumbs: 90
    readonly property int maxIcons: 72
    readonly property var filePalette: ["#4c8dff", "#3fb950", "#d29922", "#e040fb", "#ff7b54", "#26c6da", "#b388ff", "#8b9099"]

    function now() { return Date.now() / 1000 }

    function fresh(title) {
        var t = Date.now() / 1000
        return {title: title, stages: [], icons: [], names: [], sizes: [], realFiles: false, at: 0, done: 0, total: 0,
                pulse: -10, lastDive: t, started: t, lastFrame: t, chips: {}, sparks: [], crumbs: [], crumbDue: 0,
                crumbSeq: 0}
    }

    // ---- events from Python
    Connections {
        target: bridge
        function onEvent(name, payload) {
            if (name === "config") {
                root.th = payload.theme; root.kindStyle = payload.kindStyle; root.kindLabels = payload.kindLabels
                root.kindOrder = payload.kindOrder; root.iconUrl = payload.icon
            } else if (name === "start") {
                root.st = fresh(payload); addStage("Starting ..."); root.running = true; canvas.requestPaint()
            } else if (name === "stop") {
                root.running = false; st.icons = []
            } else if (name === "queue") {
                st.realFiles = true
                makeIcons(payload.map(function (e) { var l = fileLabel(e[0]); return [e[0], e[1], l[0], l[1]] }))
            } else if (name === "at") {
                advance(payload)
            } else if (name === "found") {
                found(payload)
            }
        }

        function onStage(text) { addStage(text) }
        function onProgress(done, total) { setProgress(done, total) }
    }

    // file name pattern -> [label, colour] for a game file's icon
    readonly property var fileLabels: [
        [/level\d+$/, "SCENE", "#3fb950"], [/\.assets$/, "ASSET", "#4c8dff"], [/\.(unity3d|bundle|ab)$/, "BNDL", "#e040fb"],
        [/\.(ress|resource)$/, "RES", "#d29922"], [/globalgamemanagers/, "GGM", "#8b9099"],
        [/unity default resources|unity_builtin_extra/, "UNITY", "#8b9099"], [/\.vpk$/, "VPK", "#ff7b54"],
        [/\.pak$/, "PAK", "#ff7b54"], [/\.utoc$/, "IOS", "#26c6da"], [/\.dat$/, "DAT", "#b388ff"]]

    function fileLabel(name) {
        var base = String(name).split("\\").join("/").split("/").pop().toLowerCase()
        for (var i = 0; i < fileLabels.length; i++)
            if (fileLabels[i][0].test(base)) return [fileLabels[i][1], fileLabels[i][2]]
        var dot = base.lastIndexOf("."), ext = dot > 0 ? base.substr(dot + 1, 4).toUpperCase() : ""
        return [ext || "FILE", filePalette[hash(ext) % filePalette.length]]
    }

    function stageKey(text) {
        return text.split("...")[0].replace(/[\d,.]+\s*$/, "").trim().toLowerCase()
    }

    function addStage(text) {
        text = String(text).trim()
        if (!text) return
        var stl = st.stages
        if (stl.length && stageKey(stl[0].text) === stageKey(text)) stl[0].text = text  // same step, new count
        else { stl.unshift({text: text, started: now()}); if (stl.length > maxStages) stl.length = maxStages }
    }

    function setProgress(done, total) {
        st.done = done; st.total = total
        if (st.realFiles || total <= 0) return
        if (st.names.length !== total && st.at >= st.names.length) {  // a new counted step: one anonymous icon per unit
            var q = []
            for (var i = 0; i < total; i++) q.push(["", 0, "FILE", filePalette[i % filePalette.length]])
            makeIcons(q)
        }
        advance(done)
    }

    // queue entries: [name, size, label, colour]
    function makeIcons(q) {
        st.names = q.map(function (e) { return e[0] }); st.sizes = q.map(function (e) { return e[1] })
        st.at = 0
        var n = q.length, per = Math.max(1, Math.ceil(n / maxIcons))
        st.icons = []
        for (var first = 0; first < n; first += per) {
            var last = Math.min(n, first + per) - 1, size = 0
            for (var k = first; k <= last; k++) size += q[k][1]
            st.icons.push({first: first, last: last, size: size, label: per > 1 ? "×" + (last - first + 1) : q[first][2],
                          color: q[first][3], slotA: 0, slotR: 0, x: null, y: null, dive: null, diveFrom: null, gone: false})
        }
        var count = st.icons.length
        for (var j = 0; j < count; j++) {  // sunflower spiral: even spacing for any count
            st.icons[j].slotA = j * 2.39996
            st.icons[j].slotR = 150 + 95 * Math.sqrt((j + 0.5) / Math.max(1, count))
        }
    }

    function advance(index) {
        index = Math.max(st.at, Math.min(Math.floor(index), st.names.length))
        if (index === st.at) return
        st.at = index
        var t = now()
        for (var i = 0; i < st.icons.length; i++) {
            var ic = st.icons[i]
            if (ic.dive === null && ic.last < index) { ic.dive = Math.max(t, st.lastDive + diveGap); st.lastDive = ic.dive }
        }
    }

    function current() {
        for (var i = 0; i < st.icons.length; i++) {
            var ic = st.icons[i]
            if (ic.first <= st.at && st.at <= ic.last && ic.dive === null) return ic
        }
        return null
    }

    function hash(s) {  // small stable hash (FNV-1a)
        var h = 2166136261
        for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = (h * 16777619) >>> 0 }
        return h
    }

    function found(counts) {
        var t = now()
        for (var kind in counts) {
            var n = counts[kind]
            if (n <= 0) continue
            var chip = st.chips[kind]
            if (!chip) {
                var style = kindStyle[kind] || [kind.substr(0, 4).toUpperCase(), "#8b9099"]
                chip = st.chips[kind] = {kind: kind, label: style[0], color: style[1], target: 0, shown: 0, x: null, y: null,
                                        bump: -10, born: t}
            }
            var delta = n - chip.target
            chip.target = n
            if (delta <= 0) continue
            var spray = Math.min(8, 1 + Math.floor(Math.log(delta + 1) / Math.LN2))
            for (var j = 0; j < spray && st.sparks.length < maxSparks; j++)
                st.sparks.push({chip: chip, born: t + j * 0.06, bend: ((hash(kind + n + "/" + j) % 200) - 100) / 100})
        }
    }

    function iconPx(size, scale) {
        if (size <= 0) return 22 * scale
        var t = Math.min(1, Math.max(0, (Math.log(Math.max(size, 1000)) / Math.LN10 - 3) / 6.5))  // 1 KB .. ~3 GB
        return (15 + 37 * t) * scale
    }

    function fmtBytes(n) {
        var units = ["B", "KB", "MB", "GB"]
        for (var i = 0; i < units.length; i++) {
            if (n < 1000 || i === units.length - 1)
                return (i < 2 ? Math.round(n).toLocaleString(Qt.locale("en_US"), "f", 0) : n.toFixed(1)) + " " + units[i]
            n /= 1000
        }
        return ""
    }

    function fmtInt(n) { return Math.round(n).toLocaleString(Qt.locale("en_US"), "f", 0) }

    // ---- drawing
    Canvas {
        id: canvas
        anchors.fill: parent
        renderStrategy: Canvas.Cooperative

        onPaint: {
            var ctx = getContext("2d")
            var t = root.now(), dt = Math.min(0.1, t - st.lastFrame)
            if (root.frameLog) root.frameLog.push(t - st.lastFrame)
            st.lastFrame = t
            ctx.reset()
            ctx.fillStyle = th.BG
            ctx.fillRect(0, 0, width, height)
            var scale = Math.max(0.4, Math.min(1.0, width / 700, height / 780))
            var cx = width / 2, cy = Math.max(height * 0.36, 230 * scale)
            drawIcons(ctx, t, dt, cx, cy, scale)
            drawCrumbs(ctx, t, dt, cx, cy, scale)
            var bump = Math.exp(-(t - st.pulse) * 6)
            if (Object.keys(st.chips).length) {
                drawScan(ctx, t, cx, cy, scale)
                drawFound(ctx, t, dt, cx, cy, scale)
            }
            drawCenter(ctx, t, cx, cy, scale, bump)
            drawStages(ctx, t, cy + 232 * scale + 44)
            drawBar(ctx, t)
            var side = (56 + 8 * bump) * scale
            logo.width = side; logo.height = side; logo.x = cx - side / 2; logo.y = cy - side / 2
            if (root.running) requestAnimationFrame(function () { canvas.requestPaint() })
        }

        function fileIcon(ctx, size, color, label) {
            var w = size * 0.78, h = size, fold = w * 0.3
            ctx.beginPath()
            ctx.moveTo(-w / 2, -h / 2); ctx.lineTo(w / 2 - fold, -h / 2); ctx.lineTo(w / 2, -h / 2 + fold)
            ctx.lineTo(w / 2, h / 2); ctx.lineTo(-w / 2, h / 2); ctx.closePath()
            ctx.fillStyle = "#fafafa"; ctx.fill()
            ctx.lineWidth = Math.max(1, size / 22); ctx.strokeStyle = Qt.darker(color, 1.4); ctx.stroke()
            ctx.beginPath()
            ctx.moveTo(w / 2 - fold, -h / 2); ctx.lineTo(w / 2 - fold, -h / 2 + fold); ctx.lineTo(w / 2, -h / 2 + fold)
            ctx.closePath(); ctx.fillStyle = "#dcdcdc"; ctx.fill(); ctx.stroke()
            ctx.fillStyle = color
            ctx.fillRect(-w / 2, h * 0.02, w, h * 0.3)
            if (size > 14) {
                ctx.font = "bold " + Math.max(6, Math.floor(size * (label.length > 4 ? 0.2 : 0.22))) + "px sans-serif"
                ctx.fillStyle = "white"; ctx.textAlign = "center"; ctx.textBaseline = "middle"
                ctx.fillText(label, 0, h * 0.02 + h * 0.15)
            }
        }

        function drawIcons(ctx, t, dt, cx, cy, scale) {
            var cur = root.current(), swirl = (t - st.started) * 0.12, fadeIn = Math.min(1, (t - st.started) * 3)
            var keep = []
            for (var i = 0; i < st.icons.length; i++) {
                var ic = st.icons[i]
                if (ic.gone) continue
                var px = root.iconPx(ic.size, scale), alpha = 1, tilt = 0, extra = 1
                if (ic.dive !== null && t >= ic.dive) {
                    if (ic.diveFrom === null) {
                        var sx = ic.x === null ? cx : ic.x, sy = ic.y === null ? cy : ic.y
                        ic.diveFrom = [Math.hypot(sx - cx, (sy - cy) / 0.82), Math.atan2((sy - cy) / 0.82, sx - cx)]
                    }
                    var p = (t - ic.dive) / diveTime
                    if (p >= 1) { ic.gone = true; st.pulse = t; continue }
                    var e = p * p, r = ic.diveFrom[0] * (1 - e), a = ic.diveFrom[1] + 2.2 * e
                    ic.x = cx + Math.cos(a) * r; ic.y = cy + Math.sin(a) * r * 0.82
                    extra = 1 - 0.75 * e; alpha = 1 - Math.max(0, p - 0.6) / 0.4; tilt = a
                } else {
                    var ta, tr
                    if (ic === cur) {                    // being read: circles just outside the dots, wobbling
                        ta = (t - st.started) * 0.8; tr = 140 * scale
                        extra = 1.12 + 0.08 * Math.sin(t * 6); tilt = Math.sin(t * 3) * 0.25
                    } else {                             // waiting: slow swirl, gentle bob
                        ta = ic.slotA + swirl; tr = ic.slotR * scale + 5 * Math.sin(t * 1.3 + ic.slotA)
                        tilt = Math.sin(t * 0.8 + ic.slotA) * 0.15
                    }
                    var tx = cx + Math.cos(ta) * tr, ty = cy + Math.sin(ta) * tr * 0.82
                    if (ic.x === null) { ic.x = tx; ic.y = ty }
                    var follow = 1 - Math.exp(-dt * (ic === cur ? 3 : 4))  // glide, don't jump, between spots
                    ic.x += (tx - ic.x) * follow; ic.y += (ty - ic.y) * follow
                    alpha = fadeIn
                }
                keep.push(ic)
                if (Math.hypot(ic.x - cx, ic.y - cy) < 34 * scale) continue  // behind the app icon
                ctx.save()
                ctx.translate(ic.x, ic.y); ctx.rotate(tilt); ctx.globalAlpha = Math.max(0, alpha)
                fileIcon(ctx, px * extra, ic.color, ic.label)
                ctx.restore()
                if (ic === cur && ic.size)
                    caption(ctx, ic.x, ic.y + px * extra * 0.5 + 4, root.fmtBytes(ic.size))
            }
            st.icons = keep
        }

        function caption(ctx, x, y, text) {
            ctx.font = "bold 11px sans-serif"; ctx.fillStyle = th.MUTED; ctx.textAlign = "center"; ctx.textBaseline = "top"
            ctx.fillText(text, x, y)
        }

        function drawCrumbs(ctx, t, dt, cx, cy, scale) {
            // while a file is being read, bits peel off it and stream into the centre - more for bigger files
            var cur = root.current()
            if (cur !== null && cur.x !== null) {
                var mb = Math.max(cur.size, 1) / 1e6
                st.crumbDue += dt * Math.min(26, 7 + 5 * Math.log(1 + mb) / Math.LN10)
                var px = root.iconPx(cur.size, scale)
                while (st.crumbDue >= 1 && st.crumbs.length < maxCrumbs) {
                    st.crumbDue -= 1; st.crumbSeq += 1
                    var h = root.hash("c" + st.crumbSeq)
                    st.crumbs.push({x: cur.x + ((h % 100) / 100 - 0.5) * px * 0.7, y: cur.y + (((h >>> 8) % 100) / 100 - 0.5) * px * 0.8,
                                   born: t, bend: ((h >>> 16) % 200 - 100) / 100, color: cur.color,
                                   glyph: h % 3 ? "" : ((h >>> 4) % 2 ? "1" : "0"), size: (3.5 + (h >>> 20) % 3) * scale})
                }
                st.crumbDue = Math.min(st.crumbDue, 2)
            } else {
                st.crumbDue = 0
            }
            var keep = []
            ctx.font = "bold " + Math.max(8, Math.floor(12 * scale)) + "px sans-serif"
            ctx.textAlign = "center"; ctx.textBaseline = "middle"
            for (var i = 0; i < st.crumbs.length; i++) {
                var c = st.crumbs[i], p = (t - c.born) / crumbTime
                if (p >= 1) continue
                keep.push(c)
                var e = p * p * (3 - 2 * p), dx = cx - c.x, dy = cy - c.y, len = Math.hypot(dx, dy) || 1
                var arc = Math.sin(Math.PI * e) * 26 * scale * c.bend   // a slight curve, not a straight line
                var x = c.x + dx * e - dy / len * arc, y = c.y + dy * e + dx / len * arc
                if (Math.hypot(x - cx, y - cy) < 28 * scale) continue
                ctx.globalAlpha = Math.min(1, p * 6) * (1 - e * 0.6)
                ctx.fillStyle = c.color
                if (c.glyph) ctx.fillText(c.glyph, x, y)
                else { var s = c.size * (1 - 0.5 * e); ctx.fillRect(x - s / 2, y - s / 2, s, s) }
            }
            ctx.globalAlpha = 1
            st.crumbs = keep
        }

        function drawScan(ctx, t, cx, cy, scale) {
            // a radar-style sweep round the centre while the plugin looks through what it loaded
            var r = 74 * scale, start = -(t - st.started) * 220 * Math.PI / 180
            ctx.lineCap = "round"; ctx.lineWidth = 3 * scale; ctx.strokeStyle = th.ACCENT
            for (var k = 0; k < 10; k++) {  // a fading tail behind the bright head
                ctx.globalAlpha = 0.5 * (1 - k / 10)
                ctx.beginPath()
                var a0 = start - k * 9 * Math.PI / 180
                ctx.arc(cx, cy, r, a0 - 9 * Math.PI / 180, a0, false)
                ctx.stroke()
            }
            ctx.globalAlpha = 0.12; ctx.lineWidth = 1.2 * scale
            ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2, false); ctx.stroke()
            ctx.globalAlpha = 1
        }

        function chipSpots(cx, cy, scale) {
            // evenly round an ellipse, kinds in the app's usual order, starting at the top
            var list = []
            for (var k in st.chips) list.push(st.chips[k])
            list.sort(function (a, b) {
                var ia = kindOrder.indexOf(a.kind), ib = kindOrder.indexOf(b.kind)
                return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
            })
            var n = list.length, rx = 255 * scale, ry = 168 * scale, out = []
            for (var i = 0; i < n; i++) {
                var a = -Math.PI / 2 + i * Math.PI * 2 / n
                out.push([list[i], cx + Math.cos(a) * rx, cy + Math.sin(a) * ry])
            }
            return out
        }

        function drawFound(ctx, t, dt, cx, cy, scale) {
            var spots = chipSpots(cx, cy, scale), follow = 1 - Math.exp(-dt * 5), i
            for (i = 0; i < spots.length; i++) {
                var chip = spots[i][0]
                if (chip.x === null) { chip.x = cx; chip.y = cy }  // new counters slide out from the middle
                chip.x += (spots[i][1] - chip.x) * follow; chip.y += (spots[i][2] - chip.y) * follow
                chip.shown += (chip.target - chip.shown) * (1 - Math.exp(-dt * 3))
                if (chip.target - chip.shown < 0.5) chip.shown = chip.target
            }
            // the icons in flight: out of the centre, along a slight curve, into their counter
            var keep = []
            for (i = 0; i < st.sparks.length; i++) {
                var s = st.sparks[i], p = (t - s.born) / sparkTime
                if (p < 0) { keep.push(s); continue }
                if (p >= 1) { s.chip.bump = t; continue }
                keep.push(s)
                var e = 1 - Math.pow(1 - p, 3), dx = s.chip.x - cx, dy = s.chip.y - cy, len = Math.hypot(dx, dy) || 1
                var arc = Math.sin(Math.PI * e) * 40 * scale * s.bend
                var x = cx + dx * e - dy / len * arc, y = cy + dy * e + dx / len * arc
                if (Math.hypot(x - cx, y - cy) < 30 * scale) continue
                ctx.save()
                ctx.translate(x, y); ctx.rotate(s.bend * (1 - e) * 1.2)
                ctx.globalAlpha = Math.min(1, p * 5) * (1 - Math.max(0, e - 0.85) / 0.15)
                fileIcon(ctx, (14 + 8 * (1 - e)) * scale, s.chip.color, s.chip.label)
                ctx.restore()
            }
            st.sparks = keep
            for (i = 0; i < spots.length; i++) drawChip(ctx, t, spots[i][0], scale)
        }

        function drawChip(ctx, t, chip, scale) {
            var bump = Math.exp(-(t - chip.bump) * 7), grow = 1 + 0.1 * bump
            ctx.font = "bold " + Math.max(9, Math.floor(12 * scale)) + "px sans-serif"
            var text = (kindLabels[chip.kind] || chip.kind) + "  " + root.fmtInt(chip.shown)
            var w = (ctx.measureText(text).width + 30 * scale) * grow, h = 24 * scale * grow
            ctx.globalAlpha = Math.min(1, (t - chip.born) * 3)
            ctx.fillStyle = th.RAISED
            ctx.beginPath(); ctx.roundedRect(chip.x - w / 2, chip.y - h / 2, w, h, h / 2, h / 2)
            ctx.fill()
            ctx.save(); ctx.globalAlpha *= 0.45 + 0.55 * bump
            ctx.lineWidth = 1.5 * scale; ctx.strokeStyle = chip.color; ctx.stroke(); ctx.restore()
            ctx.fillStyle = chip.color
            ctx.beginPath(); ctx.arc(chip.x - w / 2 + 12 * scale * grow, chip.y, 4.5 * scale * grow, 0, Math.PI * 2, false); ctx.fill()
            ctx.fillStyle = th.TEXT; ctx.textAlign = "left"; ctx.textBaseline = "middle"
            ctx.fillText(text, chip.x - w / 2 + 20 * scale * grow, chip.y)
            ctx.globalAlpha = 1
        }

        function drawCenter(ctx, t, cx, cy, scale, bump) {
            // orbiting dots, Gmod style: orange with one magenta, each breathing on its own beat
            var n = 9, ring = (52 + 6 * bump) * scale, spin = (t - st.started) * 1.7
            for (var i = 0; i < n; i++) {
                var a = spin + i * Math.PI * 2 / n, r = (4.5 + 1.8 * Math.sin(t * 5 + i * 1.3)) * scale
                var x = cx + Math.cos(a) * ring, y = cy + Math.sin(a) * ring
                ctx.fillStyle = i === 0 ? "#e040fb" : "#ff9f1c"
                ctx.globalAlpha = 0.24
                ctx.beginPath(); ctx.arc(x, y, r * 2, 0, Math.PI * 2, false); ctx.fill()
                ctx.globalAlpha = 1
                ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2, false); ctx.fill()
            }
            if (bump > 0.02) {  // a ring that flashes out each time a file is swallowed
                ctx.globalAlpha = 0.55 * bump; ctx.strokeStyle = th.ACCENT; ctx.lineWidth = 2.5 * scale
                ctx.beginPath(); ctx.arc(cx, cy, (36 + 30 * (1 - bump)) * scale, 0, Math.PI * 2, false); ctx.stroke()
                ctx.globalAlpha = 1
            }
            if (!logo.ready) {
                var side = (56 + 8 * bump) * scale
                ctx.fillStyle = th.ACCENT
                ctx.beginPath(); ctx.roundedRect(cx - side / 2, cy - side / 2, side, side, side * 0.2, side * 0.2); ctx.fill()
                ctx.fillStyle = "white"; ctx.font = "bold " + Math.floor(side * 0.6) + "px sans-serif"
                ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText("U", cx, cy)
            }
        }

        function elide(ctx, text, maxw) {
            if (ctx.measureText(text).width <= maxw) return text
            var keep = text.length
            while (keep > 4) {
                keep -= 2
                var half = Math.floor(keep / 2), s = text.substr(0, half) + "…" + text.substr(text.length - (keep - half))
                if (ctx.measureText(s).width <= maxw) return s
            }
            return text.substr(0, 4) + "…"
        }

        function filesLeft() {
            if (!st.names.length || st.at >= st.names.length) return ""
            var left = st.names.length - st.at, text = root.fmtInt(left) + " file" + (left !== 1 ? "s" : "") + " left"
            if (st.realFiles) {
                var bytes = 0
                for (var i = st.at; i < st.sizes.length; i++) bytes += st.sizes[i]
                if (bytes) text += " (" + root.fmtBytes(bytes) + ")"
            }
            return text
        }

        function drawStages(ctx, t, top) {
            ctx.textAlign = "center"; ctx.textBaseline = "middle"
            if (st.title) {
                ctx.font = "bold 14px sans-serif"; ctx.fillStyle = th.TEXT
                ctx.fillText("Opening " + st.title, width / 2, top - 23)
            }
            var y = top
            for (var i = 0; i < st.stages.length; i++) {
                ctx.font = (i === 0 ? "bold " : "") + "14px sans-serif"
                ctx.fillStyle = i === 0 ? th.TEXT : th.MUTED
                ctx.globalAlpha = i === 0 ? 1 : Math.max(0.12, 0.75 - i * 0.14)
                ctx.fillText(elide(ctx, st.stages[i].text, Math.max(100, width - 60)), width / 2, y + 10)
                y += 20
            }
            ctx.globalAlpha = 1
            var el = Math.floor(t - st.started)
            var parts = [Math.floor(el / 60) + ":" + ("0" + el % 60).slice(-2) + " elapsed"]
            var left = filesLeft()
            if (left) parts.push(left)
            var total = 0
            for (var k in st.chips) total += st.chips[k].target
            if (total) parts.push(root.fmtInt(total) + " assets found")
            if (st.stages.length && t - st.stages[0].started > slowHint) parts.push("still working, big files can take a few minutes")
            ctx.font = "12px sans-serif"; ctx.fillStyle = th.FAINT
            var room = Math.max(160, width - 2 * 285)  // stay clear of the LOADING box in the corner
            var line = parts.join("  •  ")
            if (ctx.measureText(line).width > room && parts.length > 2) line = parts.slice(0, -1).join("  •  ")
            ctx.fillText(elide(ctx, line, room), width / 2, y + 23)
        }

        function drawBar(ctx, t) {
            // the little grey 'LOADING...' box in the bottom right, with a segmented bar
            var w = 260, h = 50, m = 10, bx = width - w - m, by = height - h - m
            ctx.fillStyle = th.RAISED; ctx.strokeStyle = th.BORDER_STRONG; ctx.lineWidth = 1
            ctx.beginPath(); ctx.roundedRect(bx + 0.5, by + 0.5, w, h, 4, 4); ctx.fill(); ctx.stroke()
            var done, total, label
            if (st.realFiles && st.at < st.names.length) {
                done = st.at; total = st.names.length; label = "LOADING...  file " + (done + 1) + " / " + total
            } else {
                done = st.done; total = st.total
                var word = Object.keys(st.chips).length ? "INDEXING..." : "LOADING..."
                label = total <= 0 ? word : word + "  " + root.fmtInt(done) + " / " + root.fmtInt(total)
            }
            ctx.font = "bold 10px sans-serif"; ctx.fillStyle = th.MUTED; ctx.textAlign = "left"; ctx.textBaseline = "top"
            ctx.fillText(label, bx + 10, by + 7)
            var tx = bx + 10, ty = by + 24, tw = w - 20, trackH = 16
            ctx.fillStyle = th.BG; ctx.fillRect(tx, ty, tw, trackH)
            var segs = 22, gap = 2, sw = (tw - 4 - gap * (segs - 1)) / segs, from = 0, to = 0
            if (total > 0) { to = Math.min(segs, Math.round(segs * done / total)) }
            else { from = Math.floor((Math.sin((t - st.started) * 1.6) + 1) / 2 * (segs - 5)); to = from + 5 }
            ctx.fillStyle = th.TEXT
            for (var i = from; i < to; i++) ctx.fillRect(tx + 2 + i * (sw + gap), ty + 2, sw, trackH - 4)
        }
    }

    Image {
        id: logo
        property bool ready: status === Image.Ready
        source: root.iconUrl
        fillMode: Image.PreserveAspectFit
        smooth: true
        mipmap: true
        visible: ready
    }
}
