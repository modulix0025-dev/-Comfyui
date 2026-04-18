/* smart_output_system — 30-slot preview grid */

.sos-wrapper {
    padding: 6px 6px 8px 6px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: transparent;
    width: 100%;
    box-sizing: border-box;
}

.sos-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 11px;
    font-weight: 600;
    color: #bbb;
    padding: 0 2px;
}

.sos-header .sos-counts {
    font-weight: 400;
    color: #888;
    font-size: 10px;
}

.sos-grid {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 3px;
    width: 100%;
}

.sos-cell {
    aspect-ratio: 1 / 1;
    border-radius: 4px;
    position: relative;
    overflow: hidden;
    cursor: pointer;
    background-size: cover;
    background-position: center;
    background-color: #2a2a2a;
    border: 1px dashed #444;
    transition: transform .12s ease, box-shadow .12s ease;
}

.sos-cell:hover {
    transform: scale(1.06);
    z-index: 4;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.55);
}

.sos-cell .sos-idx {
    position: absolute;
    top: 2px;
    left: 3px;
    font-size: 9px;
    font-weight: 700;
    color: #fff;
    text-shadow: 0 0 3px #000, 0 0 1px #000;
    pointer-events: none;
}

.sos-cell.ready {
    background-color: #1a3a1a;
    border: 1px solid #4caf50;
}

.sos-cell.error {
    background-color: #3a1a1a;
    border: 1px solid #e53935;
    display: flex; align-items: center; justify-content: center;
}
.sos-cell.error::after {
    content: "!";
    font-size: 16px; color: #ffb4b0; font-weight: 900;
}

.sos-cell.video-ready {
    background: linear-gradient(135deg, #16324a 0%, #0e1f30 100%);
    border: 1px solid #2196f3;
    display: flex; align-items: center; justify-content: center;
}
.sos-cell.video-ready::after {
    content: "▶";
    font-size: 22px;
    color: #64b5f6;
    text-shadow: 0 0 4px rgba(33, 150, 243, 0.6);
}

/* Expanded modal preview */
.sos-modal {
    position: fixed; inset: 0;
    background: rgba(0, 0, 0, 0.82);
    display: flex; align-items: center; justify-content: center;
    z-index: 9999;
    cursor: zoom-out;
}
.sos-modal .sos-modal-inner {
    max-width: 90vw;
    max-height: 90vh;
    display: flex; flex-direction: column; align-items: center;
    gap: 8px;
}
.sos-modal img, .sos-modal video {
    max-width: 90vw;
    max-height: 85vh;
    object-fit: contain;
    border-radius: 6px;
    box-shadow: 0 0 40px rgba(0, 0, 0, 0.9);
}
.sos-modal .sos-modal-caption {
    color: #ddd;
    font-family: system-ui, sans-serif;
    font-size: 13px;
}


/* ══════════════════════════════════════════════════════════════════════════
   Download ZIP button — used by the two packager nodes.
   Placed below all existing widgets via node.addDOMWidget().
   Additive only — does not affect any existing rules above.
   ══════════════════════════════════════════════════════════════════════════ */

.sos-dlbtn-wrapper {
    padding: 8px 6px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    box-sizing: border-box;
    width: 100%;
}

.sos-dlbtn {
    width: 100%;
    padding: 10px 14px;
    font-size: 13px;
    font-weight: 600;
    border-radius: 5px;
    border: 1px solid transparent;
    cursor: pointer;
    transition: background .15s ease, border-color .15s ease, transform .05s ease, box-shadow .15s ease;
    font-family: inherit;
    letter-spacing: 0.3px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    user-select: none;
}
.sos-dlbtn::before {
    content: "⬇";
    font-size: 14px;
    display: inline-block;
}
.sos-dlbtn.ready {
    background: linear-gradient(180deg, #2e7d32 0%, #1b5e20 100%);
    color: #fff;
    border-color: #4caf50;
    box-shadow: 0 2px 6px rgba(76, 175, 80, 0.25);
}
.sos-dlbtn.ready:hover {
    background: linear-gradient(180deg, #388e3c 0%, #2e7d32 100%);
    box-shadow: 0 3px 10px rgba(76, 175, 80, 0.4);
}
.sos-dlbtn.ready:active { transform: translateY(1px); }
.sos-dlbtn.not-ready {
    background: #3a3a3a;
    color: #888;
    border-color: #555;
    cursor: not-allowed;
    box-shadow: none;
}
.sos-dlbtn.not-ready::before { opacity: 0.5; }

.sos-dlbtn-info {
    font-size: 10px;
    color: #888;
    text-align: center;
    word-break: break-all;
    padding: 0 4px;
    line-height: 1.3;
    min-height: 1em;
}
.sos-dlbtn-info.error { color: #e57373; }
