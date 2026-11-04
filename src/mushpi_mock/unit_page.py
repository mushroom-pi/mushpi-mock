"""
Vendored on-unit page — loader and the mock's render-time seam.

NestJS mapping:
    This is a plain service module (like an ``@Injectable()`` class) that owns
    one job: turn the firmware's page bytes into an HTTP response for the mock.
    The analog in the firmware is ``app/api.py`` — specifically the
    ``_unit_page()`` helper, the ``_provision_form()`` route, and the
    ``_static()`` route. We mirror all three:

        firmware:  _UNIT_HTML = open('app/unit.html').read()          # load once
                   html = _UNIT_HTML.replace("MODE", mode).replace("DEVNAME", name)
                   _PROVISIONING_HTML.replace("DEVNAME", name)
                   app.get('/static/<string:filename>') -> send_file(...)

        mock:      _UNIT_HTML = read vendored copy                     # load once
                   render_unit_page(mode, name) -> the same substitution
                   render_provision_page(name)  -> the firmware's DEVNAME inject
                   static_response(filename)     -> Response w/ content type + Cache-Control

    FastAPI equivalent of the firmware's ``Response(html, headers=...)`` is
    ``fastapi.Response`` (the Starlette response the firmware's ``Response`` maps to).

What this module is NOT
    The pages themselves never live here as reimplementations.
    ``src/mushpi_mock/static/`` holds **byte-identical** copies of the firmware's
    ``app/unit.html``, ``app/static/unit.js``, ``app/static/favicon.svg`` and
    ``app/provision.html`` (see ``static/VENDOR`` and
    ``scripts/check-unit-page-drift.sh``). Everything a real unit shows is
    shown by those bytes, unchanged and unit-agnostic.

    The provisioning form (``GET /provision``) carries **no seam at all**: the
    only substitution it gets is the firmware's own ``DEVNAME`` injection. Its
    form posts to ``POST /provision``, where the mock answers with the
    firmware's STA-mode refusal — the page surfaces that ``403`` body verbatim,
    which is exactly what a connected real unit shows. Labelling it further
    would make the mirror less faithful, not more.

The seam (the only allowed differences)
    A simulated unit must be *labelled* as simulated, and must say which of its
    claims the mock cannot honour. Since the runtime-state build the mock
    persists grower-chosen values across a stop/start (setpoints, the loop flag,
    hand-set fan/humidifier — heater never), the banner says so and names the
    two divergences from a real unit: state lives in a file instead of flash,
    and the PIN mapping (``config.json`` on the device) is memory-only here.
    Those statements — and nothing else — are applied here as anchored string
    substitutions (``MOCK_PAGE_EDITS``), exactly the way the firmware
    substitutes ``MODE``/``DEVNAME``, so:
      * the vendored bytes stay a verbatim copy (the drift check is a plain hash),
      * the page shipped by ``GET /ui`` and ``GET /`` is the same page a unit ships
        plus the labels the requirements ask for,
      * a firmware reword of a labelled sentence **fails loudly at import**
        instead of silently dropping the label (see ``_validate_anchors``).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Response
from fastapi.responses import JSONResponse

# Same directory layout as the firmware: the page plus its same-origin assets.
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# ── load once (mirrors the firmware's import-time read of app/unit.html) ──────


def _read_text(name: str) -> str | None:
    """Read a vendored page file; ``None`` when it is absent.

    The firmware opens its page inside the request handler and turns an OSError
    into a 404/"" stub; the mock reads once at import, so absence is reported
    with a ``None`` the callers handle rather than a stack trace at first request.
    """
    try:
        return (_STATIC_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return None


def _read_bytes(name: str) -> bytes | None:
    """Read a vendored static asset; ``None`` when absent (served as 404)."""
    try:
        return (_STATIC_DIR / name).read_bytes()
    except OSError:
        return None


_UNIT_HTML = _read_text("unit.html")
if _UNIT_HTML is None:
    raise RuntimeError(
        "unit page is missing from src/mushpi_mock/static/ — "
        "run scripts/refresh-unit-page.sh (or git checkout the vendored copy)."
    )

# The Wi-Fi provisioning form, loaded once like the unit page (the firmware
# reads app/provision.html at import). Unlike unit.html, an absent copy does
# NOT abort the boot: the form is peripheral to the mirrored surface, so the
# route answers with the firmware-shaped JSON 404 below instead (the firmware's
# own fallback is a 200 stub page; the mock's maintainer-approved shape for
# this edge is the 404 — a silently served stub would hide a broken vendor).
_PROVISION_HTML = _read_text("provision.html")

_PROVISION_MISSING = {"ok": False, "error": "provisioning form missing"}

# Closed asset set — the spec's /static/{filename} enum. Never a wildcard, and no
# filesystem path is ever built from the request string: a name outside this map
# 404s by construction, which is the mock's equivalent of the firmware's
# `<string:filename>` segment type (a single path component, no slashes).
STATIC_ASSETS: dict[str, tuple[str, str]] = {
    "unit.js": ("unit.js", "application/javascript"),
    "favicon.svg": ("favicon.svg", "image/svg+xml"),
}

_ASSET_BYTES: dict[str, bytes | None] = {
    name: _read_bytes(name) for name, (_filename, _media_type) in STATIC_ASSETS.items()
}

# ── the mock's only page differences ─────────────────────────────────────────
# Each entry is (anchor, replacement). Every anchor must occur EXACTLY ONCE in the
# vendored page — see _validate_anchors(). Wording mirrors the requirements:
#   * this unit is simulated — no real sensor read, no real device switched
#   * grower-chosen values survive a mock stop/start (state file, not flash):
#     targets, loop flag, hand-set fan/humidifier; the heater always comes back off
#   * a real unit keeps pin changes across reboot (config.json on flash); this one
#     holds them in memory only, and its file state can be reset by deleting it
# The small-caps "simulated" tag on the Climate and Board-condition cards labels
# the values that come from the simulation (both are read-only on the page — they
# have no inputs — so no control has to be disabled here).

_SIM_TAG = (
    '<span style="font-size:11px;font-weight:700;color:#9AA6A0;'
    "border:1px solid #9AA6A0;border-radius:999px;padding:1px 10px;"
    'margin-left:8px;vertical-align:2px">simulated</span>'
)

_SIMULATED_BANNER = """<div class="msg e" id="fetchMsg"></div>
        </div>

        <!-- ── simulated-unit banner ── -->
        <div class="card banner">
            <h2>Simulated unit</h2>
            <div class="why" style="font-size:13px">
                This mock unit is <b>simulated</b>. No real sensor is read and no real device is
                switched — the environment and board-condition values below come from the mock's
                own control-loop simulation.
                <br><br>
                <b>Your targets, the loop switch and hand-set fan and humidifier positions survive
                a stop/start of the mock</b> — saved to a per-unit state file on this machine and
                restored on start, the way a real unit writes them to flash and brings them back
                after a power cut. The same safety rule holds: the <b>heater always comes back
                off</b>. Two claims a real unit makes, this one cannot: the state lives in a
                <b>file, not flash</b> (delete the file and the unit resets to defaults), and the
                <b>PIN mapping is memory-only</b> — a real unit saves it to config.json and keeps
                it across reboots; here it is lost when the mock stops.
            </div>
        </div>"""

MOCK_PAGE_EDITS: list[tuple[str, str]] = [
    # D15/D16 — statement that the unit is simulated + the restart caveat,
    # inserted as a banner right after the header card.
    (
        '<div class="msg e" id="fetchMsg"></div>\n        </div>',
        _SIMULATED_BANNER,
    ),
    # B6 — environment values labelled simulated.
    (
        "<h2>Climate &amp; outputs</h2>",
        f"<h2>Climate &amp; outputs {_SIM_TAG}</h2>",
    ),
    # B7 — board-condition values labelled simulated too.
    (
        "<h2>Board condition</h2>",
        f"<h2>Board condition {_SIM_TAG}</h2>",
    ),
    # D16 — the PIN card's "written to flash and survives reboot" claim, corrected.
    (
        "A saved change is written to flash and survives\n                reboot.</div>",
        "On this simulated unit a change lives in memory only and is lost when\n                the mock stops.</div>",
    ),
    # D16 — the closing PIN-card paragraph's persistence claim, corrected.
    (
        "A saved mapping is persisted to config.json",
        "On this simulated unit the mapping lives in memory only",
    ),
    (
        "<b>it survives a reboot</b>",
        "<b>it is lost when the mock stops</b>",
    ),
]


def _validate_anchors() -> None:
    """Fail loudly if a substitution no longer lands, or the token contract moved.

    Two invariants, both inherited from the firmware page contract:
      1. every ``MOCK_PAGE_EDITS`` anchor occurs **exactly once**, so a label is
         never silently dropped and never applied to the wrong passage;
      2. ``MODE`` and ``DEVNAME`` occur exactly once each in the unit page and
         never in the script, and ``DEVNAME`` occurs exactly once in the
         provisioning form — the injection is load-bearing there, and this
         module must not be what breaks it.

    NestJS analogy: a boot-time assertion in ``onModuleInit`` that the wiring a
    route depends on still exists — better to refuse to start than to serve a
    page that quietly misrepresents itself.
    """
    problems: list[str] = []

    for anchor, _replacement in MOCK_PAGE_EDITS:
        found = _UNIT_HTML.count(anchor)
        if found != 1:
            preview = anchor[:72].replace("\n", "\\n")
            problems.append(f"anchor found {found}× (expected exactly 1): {preview!r}")

    for token in ("MODE", "DEVNAME"):
        if _UNIT_HTML.count(token) != 1:
            problems.append(f"page token {token} found {_UNIT_HTML.count(token)}× (expected 1)")
    if _PROVISION_HTML is not None and _PROVISION_HTML.count("DEVNAME") != 1:
        problems.append(
            f"provision token DEVNAME found {_PROVISION_HTML.count('DEVNAME')}× "
            "(expected 1)"
        )
    unit_js = _read_text("unit.js")
    if unit_js is not None:
        for token in ("MODE", "DEVNAME"):
            if token in unit_js:
                problems.append(f"static asset unit.js must never contain the token {token}")

    if problems:
        raise RuntimeError(
            "the vendored unit page no longer matches this module's seam — "
            "labels would be silently dropped. Re-point the anchors in "
            "src/mushpi_mock/unit_page.py after running scripts/refresh-unit-page.sh:\n  - "
            + "\n  - ".join(problems)
        )


_validate_anchors()


# ── rendering ─────────────────────────────────────────────────────────────────


def render_unit_page(mode: str, device_name: str) -> str:
    """Produce the page the unit would serve, with the mock's labels applied.

    Args:
        mode: ``"sta"`` or ``"ap"`` — the operating mode the page boots in.
        device_name: the unit handle, injected as ``DEVNAME``.

    Substitution order is load-bearing and copied from the firmware: **MODE is
    replaced before DEVNAME**, so a handle that literally contains the ``MODE``
    token cannot be re-scanned by the later pass (each ``.replace()`` only ever
    touches the string that exists at that moment).
    """
    html = _UNIT_HTML
    for anchor, replacement in MOCK_PAGE_EDITS:
        html = html.replace(anchor, replacement, 1)
    return html.replace("MODE", mode).replace("DEVNAME", device_name)


def unit_page_response(mode: str, device_name: str) -> Response:
    """The page as an HTTP response — always ``text/html`` (firmware parity)."""
    return Response(content=render_unit_page(mode, device_name), media_type="text/html")


def render_provision_page(device_name: str) -> str:
    """Produce the provisioning form the unit would serve — unmodified bytes.

    Mirrors the firmware's ``_provision_form``: the single substitution is
    ``DEVNAME`` → the unit handle (the firmware uses
    ``cfg.get("device_name", "pico")``; the mock's handle is its equivalent),
    exactly as ``render_unit_page`` does — no MOCK_PAGE_EDITS seam is applied
    here. The form's own ``fetch("/provision")`` submit hits the router's
    firmware-mode STA refusal (403), which the page renders in its error
    slot; that faithful refusal IS the correct simulated-unit behaviour, so
    there is nothing to label.
    """
    if _PROVISION_HTML is None:
        # Defensive: provision_page_response answers 404 before ever calling
        # here, so this only fires if a caller uses the renderer directly.
        raise RuntimeError("provisioning form is not vendored — cannot render")
    return _PROVISION_HTML.replace("DEVNAME", device_name)


def provision_page_response(device_name: str) -> Response:
    """The form as an HTTP response — ``text/html``, or the firmware-shaped 404.

    Same path, content type and status as the firmware's ``GET /provision``;
    the ``None`` vendor (file unreadable at import) answers with the
    ``{"ok": false, "error": ...}`` JSON shape the firmware uses for its
    missing-file edges, never FastAPI's ``{"detail": ...}``.
    """
    if _PROVISION_HTML is None:
        return JSONResponse(status_code=404, content=_PROVISION_MISSING)
    return Response(content=render_provision_page(device_name), media_type="text/html")


def static_response(filename: str) -> Response:
    """Serve one page asset, or the firmware's 404 body when it is not one.

    Mirrors the firmware's ``_static``: explicit content type from a closed map,
    ``Cache-Control: max-age=3600`` (that build has no ETag/304 revalidation, so
    the copy a browser holds gets a conservative lifetime), and a 404
    ``{"ok": false, "error": "static asset missing"}`` when the asset is absent —
    without the guard a missing asset would surface as a 500 while the page
    rendered with no controls.
    """
    entry = STATIC_ASSETS.get(filename)
    if entry is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "static asset missing"})
    name, media_type = entry
    data = _ASSET_BYTES.get(name)
    if data is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "static asset missing"})
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "max-age=3600"},
    )
