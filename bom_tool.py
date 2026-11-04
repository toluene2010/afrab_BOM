import streamlit as st
import pandas as pd
import os, json
from datetime import datetime
from io import BytesIO

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

DATA_FILE = "bom_data.json"
GIST_FILENAME = "bom_data.json"

st.set_page_config(page_title="BOM Digitizer", layout="wide")

def _local_load():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            c = f.read().strip()
        return json.loads(c) if c else {}
    except (json.JSONDecodeError, OSError):
        try:
            os.rename(DATA_FILE, f"{DATA_FILE}.corrupt_{datetime.now():%Y%m%d_%H%M%S}")
        except OSError:
            pass
        return {}

def _local_save(d):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)

def _gist_config():
    try:
        gh = st.secrets.get("github", {})
        tok = gh.get("token", "")
        gid = gh.get("gist_id", "")
        if tok and gid:
            return tok, gid
    except Exception:
        pass
    return None, None

def _gist_load(tok, gid):
    r = requests.get(
        f"https://api.github.com/gists/{gid}",
        headers={"Authorization": f"token {tok}"},
        timeout=10,
    )
    r.raise_for_status()
    files = r.json().get("files", {})
    content = files.get(GIST_FILENAME, {}).get("content", "")
    return json.loads(content) if content.strip() else {}

def _gist_save(tok, gid, d):
    r = requests.patch(
        f"https://api.github.com/gists/{gid}",
        headers={"Authorization": f"token {tok}"},
        json={"files": {GIST_FILENAME: {"content": json.dumps(d, ensure_ascii=False)}}},
        timeout=10,
    )
    r.raise_for_status()

def _normalize(d):
    d.setdefault("products", {})
    d.setdefault("materials", {})
    d.setdefault("pending", {})
    return d
    def load_data():
    tok, gid = _gist_config() if REQUESTS_OK else (None, None)
    if tok and gid:
        try:
            d = _gist_load(tok, gid)
            if not d.get("products") and not d.get("materials"):
                local = _local_load()
                if local.get("products") or local.get("materials"):
                    d = local
                    try:
                        _gist_save(tok, gid, _normalize(d))
                        st.session_state["_migrated"] = True
                    except Exception:
                        pass
            return _normalize(d)
        except Exception as e:
            st.session_state["_gist_err"] = str(e)
    return _normalize(_local_load())

def save_data(d):
    _local_save(d)
    tok, gid = _gist_config() if REQUESTS_OK else (None, None)
    if tok and gid:
        try:
            _gist_save(tok, gid, d)
        except Exception as e:
            st.session_state["_gist_err"] = str(e)

def kg_gm_to_float(kg, gm):
    return float(kg) + float(gm) / 1000.0

def fmt_kg_gm(v):
    if v is None:
        return ""
    total = round(float(v) * 1000, 4)
    kg = int(total // 1000)
    gm = round(total - kg * 1000, 4)
    if abs(gm - 1000) < 1e-6:
        kg += 1
        gm = 0.0
    def f(g):
        s = f"{g:.3f}".rstrip("0").rstrip(".")
        return s if s else "0"
    parts = []
    if kg: parts.append(f"{kg} kg")
    if gm > 0 or not kg: parts.append(f"{f(gm)} gm")
    return " ".join(parts)

def parse_qty(raw, mode):
    if raw is None:
        return None
    s = str(raw).lower().replace("kg","").replace("gm","").replace("g","").strip()
    if not s:
        return None
    if mode == "kg_gm" and "," in s:
        l, r = s.split(",", 1)
        r = (r.strip() or "0").ljust(3, "0")[:3]
        try:
            return float(l or 0) + float(r) / 1000
        except ValueError:
            return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None

data = load_data()
st.title("📋 BOM Digitizer")
if st.session_state.pop("_migrated", False):
    st.success("Migrated local data to GitHub Gist.")
if "_gist_err" in st.session_state:
    st.warning(f"Gist error: {st.session_state.pop('_gist_err')}")

with st.sidebar:
    st.subheader("⚙️ Settings")
    unit_mode = st.radio(
        "Commas in OCR mean:",
        ["kg + gm (2, 500 = 2.5 kg)", "thousands (2,500 = 2500)"],
    )
    MODE = "kg_gm" if unit_mode.startswith("kg") else "decimal"
    st.markdown("---")
    tok, gid = _gist_config() if REQUESTS_OK else (None, None)
    if tok and gid:
        st.success("☁️ Gist sync ON")
    else:
        st.warning("⚠️ Local only — resets on restart")
        st.info("Add [github] block to Streamlit Secrets to enable Gist sync.")

tabs = st.tabs(["📷 Paste", "🧱 Materials", "🛠️ Build", "⏳ Pending", "✏️ Manage", "📥 Export"])
with tabs[0]:
    st.header("Add BOM by pasting text")
    st.caption("Take photo with Google Lens on phone, copy text, paste below. Each line: CODE | NAME | QTY")

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1: pcode = st.text_input("Product Code *", key="pc1")
    with c2: pname = st.text_input("Product Name *", key="pn1")
    with c3: bsize = st.number_input("Batch Size", min_value=1, value=1, key="bs1")

    raw = st.text_area("BOM lines", height=260,
                       placeholder="MAT-001 | Sugar | 2, 500\nMAT-002 | Cocoa | 500",
                       key="paste_box")

    if st.button("💾 Save BOM"):
        if not pcode or not pname:
            st.error("Product code and name required.")
        else:
            mats, new_pending = [], []
            for line in raw.splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 3:
                    continue
                mc, mn, qraw = parts[0], parts[1], parts[2]
                q = parse_qty(qraw, MODE)
                if q is None:
                    continue
                mats.append({"code": mc, "name": mn, "qty_kg": q})
                if mc not in data["materials"] and mc not in data["pending"]:
                    data["pending"][mc] = {"name": mn, "unit": "kg"}
                    new_pending.append(mc)
            data["products"][pcode] = {
                "name": pname, "batch_size": bsize,
                "materials": mats, "source": "paste",
            }
            save_data(data)
            st.success(f"Saved {pcode} ({len(mats)} materials)")
            st.rerun()
            with tabs[1]:
    st.header("🧱 Materials")
    sub1, sub2 = st.tabs(["➕ Add one", "📤 Bulk"])

    with sub1:
        with st.form("add_mat", clear_on_submit=True):
            c1, c2, c3 = st.columns([1, 2, 1])
            with c1: mc = st.text_input("Code *", placeholder="MAT-001")
            with c2: mn = st.text_input("Name *", placeholder="Sugar")
            with c3: mu = st.selectbox("Unit", ["kg", "gm", "L", "ml", "pcs"])
            if st.form_submit_button("Add"):
                if mc and mn and mc not in data["materials"]:
                    data["materials"][mc] = {"name": mn, "unit": mu}
                    save_data(data)
                    st.rerun()
                elif mc in data["materials"]:
                    st.error(f"{mc} already exists")
                else:
                    st.error("Code and name required")

    with sub2:
        tpl = pd.DataFrame([{"Code": "MAT-001", "Name": "Sugar", "Unit": "kg"}])
        st.download_button("⬇️ Template CSV", tpl.to_csv(index=False).encode(),
                           "materials_template.csv", "text/csv")
        f = st.file_uploader("CSV/Excel", type=["csv", "xlsx"], key="bulk_mat")
        if f:
            try:
                df = pd.read_csv(f) if f.name.lower().endswith(".csv") else pd.read_excel(f)
                df.columns = [str(c).strip() for c in df.columns]
                cmap = {c.lower(): c for c in df.columns}
                if "code" not in cmap or "name" not in cmap:
                    st.error("File needs Code and Name columns")
                else:
                    added = 0
                    for _, row in df.iterrows():
                        code = str(row[cmap["code"]]).strip()
                        name = str(row[cmap["name"]]).strip()
                        unit = (str(row[cmap["unit"]]).strip()
                                if "unit" in cmap and pd.notna(row[cmap["unit"]])
                                else "kg")
                        if code and name and code not in data["materials"]:
                            data["materials"][code] = {"name": name, "unit": unit}
                            added += 1
                    save_data(data)
                    st.success(f"Imported {added}")
                    st.rerun()
            except Exception as e:
                st.error(f"Read error: {e}")

    st.markdown(f"**Registered: {len(data['materials'])}**")
    if data["materials"]:
        st.dataframe(pd.DataFrame([
            {"Code": k, "Name": v["name"], "Unit": v["unit"]}
            for k, v in data["materials"].items()
        ]), use_container_width=True, hide_index=True)
        with tabs[2]:
    st.header("🛠️ Build Product")
    if not data["materials"]:
        st.warning("Add materials first.")
    else:
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1: bp_code = st.text_input("Product Code *", key="bpc")
        with c2: bp_name = st.text_input("Product Name *", key="bpn")
        with c3: bp_batch = st.number_input("Batch Size", 1, value=1, key="bpb")

        if "draft" not in st.session_state:
            st.session_state.draft = [{"code": None, "kg": 0, "gm": 0.0}]

        mcodes = ["—"] + list(data["materials"].keys())
        labels = {c: f"{c} — {data['materials'][c]['name']}" for c in data["materials"]}
        labels["—"] = "— select —"

        for i, row in enumerate(st.session_state.draft):
            c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
            with c1:
                ch = st.selectbox(f"m{i}", mcodes, format_func=lambda x: labels.get(x, x),
                                  index=(mcodes.index(row["code"]) if row["code"] in mcodes else 0),
                                  key=f"s{i}", label_visibility="collapsed")
                row["code"] = None if ch == "—" else ch
            with c2:
                row["kg"] = st.number_input(f"kg{i}", 0, step=1, value=int(row["kg"]),
                                            key=f"k{i}", label_visibility="collapsed")
            with c3:
                row["gm"] = st.number_input(f"gm{i}", 0.0, 999.999, step=0.1,
                                            value=float(row["gm"]), key=f"g{i}",
                                            format="%.3f", label_visibility="collapsed")
            with c4:
                if st.button("✖", key=f"x{i}") and len(st.session_state.draft) > 1:
                    st.session_state.draft.pop(i)
                    st.rerun()

        if st.button("➕ Add row"):
            st.session_state.draft.append({"code": None, "kg": 0, "gm": 0.0})
            st.rerun()

        if st.button("💾 Save Product"):
            if bp_code and bp_name and any(r["code"] for r in st.session_state.draft):
                mats = [{"code": r["code"], "name": data["materials"][r["code"]]["name"],
                         "qty_kg": kg_gm_to_float(r["kg"], r["gm"])}
                        for r in st.session_state.draft if r["code"]]
                data["products"][bp_code] = {
                    "name": bp_name, "batch_size": bp_batch,
                    "materials": mats, "source": "manual",
                }
                save_data(data)
                st.session_state.draft = [{"code": None, "kg": 0, "gm": 0.0}]
                st.success(f"Saved {bp_code}")
                st.rerun()
            else:
                st.error("Fill code, name, one material")
