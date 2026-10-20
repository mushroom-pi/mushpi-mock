var unitMode = window.__UNIT__.mode;
var devName = window.__UNIT__.name;

        var snap = null;
        var saveBusy = false;
        var dirty = { controls: false, devices: false, mapping: false };
        var ROLES = ["dht", "humidifier", "fan", "heater"];
        var DEV_ROLES = ["humidifier", "fan", "heater"];
        var ROLE_LABEL = { dht: "the DHT11 sensor", humidifier: "the Humidifier", fan: "the Fan", heater: "the Heater" };

        function $(id) { return document.getElementById(id); }
        function setText(id, v) { var e = $(id); if (e) e.textContent = v; }

        function fmtBytes(b) {
            if (b == null || isNaN(b)) return "—";
            var k = 1024;
            if (b < k) return b + " B";
            if (b < k * k) return (b / k).toFixed(1) + " KB";
            return (b / (k * k)).toFixed(2) + " MB";
        }

        function fmtDur(s) {
            if (s == null || isNaN(s)) return "—";
            s = Math.floor(s);
            var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
            return (h ? h + "h " : "") + ((h || m) ? m + "m " : "") + sec + "s";
        }

        function jsonOpts(body) {
            return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
        }

        function api(path, opts) {
            return fetch(path, opts || {}).then(function (res) {
                return res.json().catch(function () { return {}; }).then(function (data) {
                    return { status: res.status, ok: res.status >= 200 && res.status < 300, data: data };
                });
            });
        }

        function errText(r) {
            if (r && r.data && r.data.error) return String(r.data.error);
            return "HTTP " + (r ? r.status : "no response");
        }

        function setMsg(id, text, isError) {
            var e = $(id);
            e.textContent = (isError ? "× " : "✓ ") + text;
            e.className = "msg " + (isError ? "e" : "s");
            e.style.display = "block";
        }

        function clearMsgs() {
            ["ctlMsg", "devMsg", "mapMsg", "rebootMsg"].forEach(function (id) { $(id).style.display = "none"; });
            $("fetchMsg").style.display = "none";
        }

        function chip(id, on, known) {
            var e = $(id);
            if (known === false) { e.textContent = "—"; e.className = "chip off"; return; }
            e.textContent = on ? "ON" : "OFF";
            e.className = "chip " + (on ? "on" : "off");
        }

        // ── load ────────────────────────────────────────────────────────────
        // Connected (STA): one snapshot from GET / (explicit Accept keeps the
        // firmware's content negotiation returning JSON). Offline (AP): GET /
        // is the Wi-Fi setup form, so the page composes the same shape from
        // the individual GET endpoints.
        function loadSnapshot() {
            if (unitMode === "ap") {
                var paths = ["/system", "/health", "/sensors", "/outputs", "/setpoints", "/control", "/setup"];
                return Promise.all(paths.map(function (p) { return api(p); })).then(function (rs) {
                    var bad = null;
                    rs.forEach(function (r) { if (!bad && !r.ok) bad = r; });
                    if (bad) throw new Error(errText(bad));
                    var sys = rs[0].data;
                    return {
                        system: sys,
                        health: rs[1].data,
                        sensors: { dht: rs[2].data },
                        outputs: rs[3].data,
                        setpoints: rs[4].data,
                        control_loop_enabled: !!rs[5].data.enabled,
                        devices: rs[6].data,
                        firmware_version: sys.software ? sys.software.version : null,
                        api_version: null,
                        uptime: sys.uptime,
                        hub: null
                    };
                });
            }
            return api("/", { headers: { "Accept": "application/json" } }).then(function (r) {
                if (!r.ok) throw new Error(errText(r));
                return r.data;
            });
        }

        // ── render ──────────────────────────────────────────────────────────
        function render(s) {
            var sys = s.system || {};
            var hw = sys.hardware || {};
            var mp = sys.micropython || {};
            var wf = sys.wifi || {};
            var dht = (s.sensors || {}).dht || {};
            var health = s.health || {};
            var mem = health.mem || {};
            var fs = health.fs || {};
            var up = s.uptime || {};
            var outs = s.outputs || {};
            var loop = !!s.control_loop_enabled;
            var port = wf.port ? ":" + wf.port : "";

            setText("devname", devName);
            setText("hdrAddr", "http://" + location.host + (wf.ip ? "  ·  unit IP " + wf.ip + port : ""));
            setText("hdrMdns", wf.hostname ? "mDNS: http://" + wf.hostname + ".local" + port : "");
            setText("footVer", devName + " · firmware " + (s.firmware_version || "—") + (s.api_version != null ? " · interface gen " + s.api_version : ""));

            setText("idBoard", hw.board || "—");
            setText("idMp", mp.build || mp.version || "—");
            setText("idFw", s.firmware_version || "—");
            setText("idApi", s.api_version != null ? String(s.api_version) : "not reported while offline");

            setText("bSince", fmtDur(up.since_boot_s) + (up.reboot_reason ? "  ·  last reboot: " + up.reboot_reason : ""));
            setText("bTemp", health.mcu_temp_c != null ? health.mcu_temp_c.toFixed(1) + " °C" : "—");
            setText("bMem", fmtBytes(mem.used) + " used · " + fmtBytes(mem.free) + " free of " + fmtBytes(mem.total) + (mem.used_pct != null ? " (" + mem.used_pct + "%)" : ""));
            setText("bFs", fmtBytes(fs.used) + " used · " + fmtBytes(fs.free) + " free of " + fmtBytes(fs.total));

            setText("cTemp", dht.temperature != null ? dht.temperature + " °C" : "no reading");
            setText("cHum", dht.humidity != null ? dht.humidity + " %" : "no reading");
            setText("cAge", dht.last_ok_age_s != null ? "Last good sensor read " + fmtDur(dht.last_ok_age_s) + " ago; sampled again every few seconds." : "No successful sensor read yet.");
            chip("cHumidifier", outs.humidifier);
            chip("cFan", outs.fan);
            chip("cHeater", outs.heater);
            chip("cSensorChip", dht.sensor_ok, true);
            $("cSensorChip").textContent = dht.sensor_ok ? "OK" : "FAULTED";
            var al = $("cSensorAlert");
            if (!dht.sensor_ok) {
                al.style.display = "block";
                al.textContent = "Sensor faulted" + (dht.last_error ? " (" + dht.last_error + ")" : "") + (
                    unitMode === "ap"
                        ? ". While offline the unit cuts the outputs until a good reading returns — the relays will not run on stale values."
                        : ". The loop keeps running on the last good reading — treat the climate numbers as stale until this clears.");
            } else {
                al.style.display = "none";
            }

            if (!dirty.controls) {
                $("loopChk").checked = loop;
                $("tempSl").value = (s.setpoints && s.setpoints.temperature != null) ? s.setpoints.temperature : 25;
                $("humSl").value = (s.setpoints && s.setpoints.humidity != null) ? s.setpoints.humidity : 60;
            }
            if (!dirty.devices) {
                DEV_ROLES.forEach(function (k) { $("dev-" + k).checked = !!outs[k]; });
            }
            var pins = (s.devices || {}).pins || {};
            if (!dirty.mapping) {
                ROLES.forEach(function (r) { if (pins[r] != null) $("pin-" + r).value = pins[r]; });
            }

            if (s.hub == null) {
                chip("hubCfgChip", false, false);
                chip("hubConnChip", false, false);
                setText("hubState", "Hub state is not tracked while offline — the unit announces itself to a hub only after it has joined a network.");
            } else {
                $("hubCfgChip").textContent = s.hub.configured ? "YES" : "NO";
                $("hubCfgChip").className = "chip " + (s.hub.configured ? "on" : "off");
                $("hubConnChip").textContent = s.hub.connected ? "ACKED" : "NO";
                $("hubConnChip").className = "chip " + (s.hub.connected ? "on" : "off");
                if (!s.hub.configured) {
                    setText("hubState", "No hub address is configured — this unit runs standalone and never announces itself.");
                } else if (s.hub.connected) {
                    setText("hubState", "Hub configured · acknowledged at the most recent announce. The unit cannot see hub outages between announcements.");
                } else {
                    setText("hubState", "Hub configured · not yet acknowledged. The unit keeps retrying its announce with growing back-off; the onboard LED stays solid until the hub accepts.");
                }
            }

            updateControls();
            updateDevices();
            updateMapping();
            updateReboot();
            setText("lastFetch", "view refreshed " + new Date().toLocaleTimeString());
        }

        // ── interactive state ───────────────────────────────────────────────
        // Controls, devices and mapping are fully operable in both modes —
        // offline the unit regulates against its held targets just like when
        // connected.
        function updateControls() {
            var loop = $("loopChk").checked;
            var al = $("ctlAlert");
            if (loop) {
                al.className = "alert info";
                al.textContent = "While the control loop is on, the unit will turn devices on and/or off automatically to try to meet the targets.";
            } else {
                al.className = "alert warn";
                al.textContent = "With the control loop off, the devices have to be turned on and/or off manually, as the unit won't automatically try to reach the targets. All relays are forced off the moment the loop is switched off.";
            }
            $("loopChk").disabled = saveBusy || !snap;
            $("tempSl").disabled = saveBusy || !snap || !loop;
            $("humSl").disabled = saveBusy || !snap || !loop;
            setText("tempVal", $("tempSl").value + " °C");
            setText("humVal", $("humSl").value + " %");
            var parts = [];
            if (snap) {
                if (loop !== snap.control_loop_enabled) parts.push("control loop → " + (loop ? "ON" : "OFF"));
                var tsp = snap.setpoints || {};
                var t = Number($("tempSl").value), h = Number($("humSl").value);
                if (t !== tsp.temperature) parts.push("temperature target " + tsp.temperature + " → " + t + " °C");
                if (h !== tsp.humidity) parts.push("humidity target " + tsp.humidity + " → " + h + " %");
            }
            setText("ctlSum", parts.length ? "Will send: " + parts.join("; ") + "." : "No changes to send.");
            $("ctlSave").disabled = saveBusy || !snap || !dirty.controls;
        }

        function updateDevices() {
            var loopOn = !!(snap && snap.control_loop_enabled);
            var al = $("devAlert");
            al.className = loopOn ? "alert warn" : "alert info";
            al.textContent = loopOn
                ? "While the control loop is on, the devices cannot be turned on and/or off manually, since the unit controls them."
                : "With the control loop disabled, the devices can be turned on and/or off manually.";
            DEV_ROLES.forEach(function (k) { $("dev-" + k).disabled = loopOn || saveBusy || !snap; });
            setText("devWhy", loopOn ? "Locked: the control loop is driving the devices. Turn the loop OFF in the Controls card (and Apply it) to unlock hand-switching here." : "");
            var parts = [];
            var outs = (snap && snap.outputs) || {};
            if (snap) {
                DEV_ROLES.forEach(function (k) {
                    if ($("dev-" + k).checked !== !!outs[k]) parts.push(k + " → " + ($("dev-" + k).checked ? "ON" : "OFF"));
                });
            }
            setText("devSum", parts.length ? "Will send: " + parts.join(", ") + "." : "No changes to send.");
            $("devSave").disabled = loopOn || saveBusy || !snap || !dirty.devices || parts.length === 0;
        }

        function mapErrors() {
            var errs = {}, vals = {};
            ROLES.forEach(function (r) {
                var raw = $("pin-" + r).value;
                var v = raw === "" ? NaN : Number(raw);
                vals[r] = v;
                if (isNaN(v) || v % 1 !== 0) { errs[r] = "A whole GPIO number is required."; return; }
                if (v === 0) { errs[r] = "GP0 is reserved for the force-provision jumper (read at every boot) — pick 1-22, 26, 27 or 28."; return; }
                if (v === 23 || v === 24 || v === 25 || v === 29) { errs[r] = "GP" + v + " is reserved for the CYW43439 wireless chip — pick 1-22, 26, 27 or 28."; return; }
                if (!((v >= 1 && v <= 22) || (v >= 26 && v <= 28))) { errs[r] = "GP" + v + " is not a usable Pico 2 W GPIO — pick 1-22, 26, 27 or 28."; return; }
            });
            ROLES.forEach(function (a) {
                if (errs[a] || isNaN(vals[a])) return;
                ROLES.forEach(function (b) {
                    if (a !== b && !errs[b] && vals[b] === vals[a]) {
                        errs[a] = "GP" + vals[a] + " is already assigned to " + ROLE_LABEL[b] + ".";
                    }
                });
            });
            return errs;
        }

        function updateMapping() {
            var ack = $("mapAck").checked;
            $("mapAck").disabled = saveBusy || !snap;
            ROLES.forEach(function (r) { $("pin-" + r).disabled = !ack || saveBusy || !snap; });
            setText("mapWhy", !ack ? "Tick the wiring check above to unlock the fields." : "Fields unlocked — changes are previewed below and only sent on Save.");

            var errs = mapErrors();
            var pins = (snap && snap.devices && snap.devices.pins) || {};
            var changed = [];
            ROLES.forEach(function (r) {
                var hint = $("hint-" + r);
                if (errs[r]) { hint.textContent = "⚠ " + errs[r]; hint.className = "hint bad"; }
                else { hint.textContent = "OK"; hint.className = "hint"; }
                var cur = pins[r] != null ? "GP" + pins[r] : "—";
                var raw = $("pin-" + r).value;
                var prop = raw === "" || isNaN(Number(raw)) ? "—" : "GP" + Number(raw);
                setText("cur-" + r, cur);
                setText("prp-" + r, prop + (cur !== "—" && prop === cur ? " (same)" : ""));
                if (cur !== "—" && prop !== "—" && prop !== cur && !errs[r]) changed.push(r + " " + cur + " → " + prop);
            });
            var okAll = Object.keys(errs).length === 0;
            setText("mapSum", changed.length ? "Will send: " + changed.join(", ") + ". The unit re-checks duplicates and reserved pins and refuses with a reason." :
                (okAll ? "No changes to send." : "Fix the highlighted fields first."));
            $("mapSave").disabled = saveBusy || !snap || !ack || !dirty.mapping || !okAll || changed.length === 0;
        }

        function updateReboot() {
            // Reboot works in both modes — offline it is how the grower retries
            // the join when the router comes back.
            $("rebootSoft").disabled = saveBusy;
            $("rebootHard").disabled = saveBusy;
        }

        function updateAll() { updateControls(); updateDevices(); updateMapping(); updateReboot(); }

        // ── actions ─────────────────────────────────────────────────────────
        function finishSave(rs, msgId, card) {
            saveBusy = false;
            var bad = null;
            rs.forEach(function (r) { if (!bad && (!r.ok || (r.data && r.data.ok === false))) bad = r; });
            dirty[card] = false;
            if (rs.length === 0) {
                setMsg(msgId, "No changes needed — the unit's reported state is unchanged.", false);
            } else if (bad) {
                setMsg(msgId, "Refused (HTTP " + bad.status + "): " + errText(bad) + " — kept the unit's reported state.", true);
            } else {
                setMsg(msgId, "Changed successfully. Showing the unit's reported state.", false);
            }
            refresh(false);
        }

        function failSave(msgId, e) {
            saveBusy = false;
            setMsg(msgId, "Request failed: " + (e && e.message ? e.message : String(e)), true);
            refresh(false);
        }

        function saveControls() {
            if (saveBusy || !snap) return;
            var t = Number($("tempSl").value), h = Number($("humSl").value);
            if (t % 1 !== 0 || t < 0 || t > 50 || h % 1 !== 0 || h < 10 || h > 90) {
                setMsg("ctlMsg", "Targets must be whole numbers: temperature 0-50, humidity 10-90.", true);
                return;
            }
            saveBusy = true;
            updateControls();
            var jobs = [];
            var want = $("loopChk").checked;
            if (want !== snap.control_loop_enabled) jobs.push(api("/control", jsonOpts({ enabled: want })));
            var tsp = snap.setpoints || {};
            if (t !== tsp.temperature || h !== tsp.humidity) jobs.push(api("/setpoints", jsonOpts({ temperature: t, humidity: h })));
            Promise.all(jobs).then(function (rs) { finishSave(rs, "ctlMsg", "controls"); }).catch(function (e) { failSave("ctlMsg", e); });
        }

        function saveDevices() {
            if (saveBusy || !snap) return;
            var body = {};
            var outs = snap.outputs || {};
            DEV_ROLES.forEach(function (k) {
                if ($("dev-" + k).checked !== !!outs[k]) body[k] = $("dev-" + k).checked;
            });
            if (Object.keys(body).length === 0) return;
            saveBusy = true;
            updateDevices();
            api("/outputs", jsonOpts(body)).then(function (r) { finishSave([r], "devMsg", "devices"); }).catch(function (e) { failSave("devMsg", e); });
        }

        function saveMapping() {
            if (saveBusy || !snap) return;
            var errs = mapErrors();
            if (Object.keys(errs).length) return;
            var pins = (snap.devices && snap.devices.pins) || {};
            var changed = {};
            ROLES.forEach(function (r) {
                var v = Number($("pin-" + r).value);
                if (pins[r] !== v) changed[r] = v;
            });
            if (Object.keys(changed).length === 0) return;
            saveBusy = true;
            updateMapping();
            api("/setup", jsonOpts({ pins: changed })).then(function (r) { finishSave([r], "mapMsg", "mapping"); }).catch(function (e) { failSave("mapMsg", e); });
        }

        function doReboot(kind) {
            var soft = kind === "soft";
            var txt = soft
                ? "Soft reboot: restarts the program (machine.soft_reset()), preserves cumulative uptime, reconnects Wi-Fi and re-announces to the hub. The unit is unreachable for about a minute.\n\nContinue?"
                : "Hard reboot: full power-cycle (machine.reset()); cumulative uptime resets to zero. Reconnects Wi-Fi and re-announces to the hub. The unit is unreachable for about a minute.\n\nContinue?";
            if (!window.confirm(txt)) return;
            saveBusy = true;
            updateReboot();
            api("/reboot", jsonOpts({ type: kind })).then(function (r) {
                saveBusy = false;
                updateReboot();
                if (!r.ok || (r.data && r.data.ok === false)) setMsg("rebootMsg", "Refused (HTTP " + r.status + "): " + errText(r), true);
                else setMsg("rebootMsg", "Reboot initiated — the unit will be unreachable for about a minute; reload this page then.", false);
            }).catch(function (e) { saveBusy = false; updateReboot(); setMsg("rebootMsg", "Request failed: " + e.message, true); });
        }

        // ── refresh ─────────────────────────────────────────────────────────
        function refresh(full) {
            if (full) {
                dirty = { controls: false, devices: false, mapping: false };
                clearMsgs();
            }
            return loadSnapshot().then(function (s) {
                snap = s;
                render(s);
            }).catch(function (e) {
                var m = $("fetchMsg");
                m.textContent = "× Could not read the unit: " + (e && e.message ? e.message : String(e));
                m.style.display = "block";
                setText("lastFetch", "last successful refresh: see error above");
            });
        }

        // ── wiring ──────────────────────────────────────────────────────────
        function wire() {
            $("refreshBtn").onclick = function () { clearMsgs(); refresh(true); };
            $("loopChk").onchange = function () { dirty.controls = true; updateControls(); };
            $("tempSl").oninput = function () { dirty.controls = true; updateControls(); };
            $("humSl").oninput = function () { dirty.controls = true; updateControls(); };
            $("ctlSave").onclick = saveControls;
            DEV_ROLES.forEach(function (k) {
                $("dev-" + k).onchange = function () { dirty.devices = true; updateDevices(); };
            });
            $("devSave").onclick = saveDevices;
            $("mapAck").onchange = function () { updateMapping(); };
            ROLES.forEach(function (r) {
                $("pin-" + r).oninput = function () { dirty.mapping = true; updateMapping(); };
            });
            $("mapSave").onclick = saveMapping;
            $("rebootSoft").onclick = function () { doReboot("soft"); };
            $("rebootHard").onclick = function () { doReboot("hard"); };
        }

        wire();
        $("apBanner").style.display = unitMode === "ap" ? "block" : "none";
        setText("devname", devName);
        updateAll();
        refresh(true);
        setInterval(function () { refresh(false); }, 30000);
