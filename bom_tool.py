import streamlit as st
import pandas as pd
import os, json, asyncio
from datetime import datetime
from io import BytesIO
from PIL import Image

# ---- Lens OCR import ----
try:
    from chrome_lens_py import LensAPI
    LENS_AVAILABLE = True
except ImportError:
    LENS_AVAILABLE = False

DATA_FILE = "bom_data.json"

def load_data():
    d = json.load(open(DATA_FILE)) if os.path.exists(DATA_FILE) else {}
    d.setdefault("products", {})
    d.setdefault("materials", {})
    d.setdefault("pending", {})
    return d

def save_data(d):
    json.dump(d, open(DATA_FILE, "w"), indent=2)

data = load_data()

# ---------- Lens OCR ----------
def lens_ocr(pil_image):
    """Call Chrome Lens endpoint via chrome-lens-py"""
    if not LENS_AVAILABLE:
        raise RuntimeError("chrome-lens-py not installed. Run: pip install chrome-lens-py")
    api = LensAPI()
    result = asyncio.run(api.process_image(image=pil_image))
    return result.get("ocr_text", "")

# ---------- Unit helpers ----------
def kg_gm_to_float(kg, gm):
    return float(kg) + float(gm) / 1000.0

def fmt_kg_gm(v):
    if v is None:
        return ""
    total_gm = round(float(v) * 1000, 4)
    kg = int(total_gm // 1000)
    gm = round(total_gm - kg * 1000, 4)
    if abs(gm - 1000) < 1e-6:
        kg += 1; gm = 0.0
    def f(g):
        s = f"{g:.3f}".rstrip("0").rstrip(".")
        return s if s else "0"
    parts = []
    if kg: parts.append(f"{kg} kg")
    if gm > 0 or not kg: parts.append(f"{f(gm)} gm")
    return " ".join(parts)

def parse_kg_gm_text(raw, mode="kg_gm"):
    if raw is None: return None
    s = str(raw).lower().replace("kg","").replace("gm","").replace("g","").strip()
    if not s: return None
    if mode == "kg_gm" and "," in s:
        l, r = s.split(",", 1)
        r = (r.strip() or "0").ljust(3,"0")[:3]
        try: return float(l or 0) + float(r)/1000
        except: return None
    try: return float(s.replace(",", ""))
    except: return None

# ---------- UI ----------
st.set_page_config(page_title="BOM Digitizer", layout="wide")
st.title("📋 BOM Digitizer")

with st.sidebar:
    st.subheader("⚙️ Settings")
    unit_mode = st.radio("Commas in OCR text mean:",
        ["kg + gm (2, 500 = 2.5 kg)", "thousands (2,500 = 2500)"])
    MODE = "kg_gm" if unit_mode.startswith("kg") else "decimal"
    if not LENS_AVAILABLE:
        st.error("chrome-lens-py not installed. OCR unavailable.")

tabs = st.tabs(["📷 OCR / Paste", "🧱 Materials", "🛠️ Build Product",
                "⏳ Pending", "✏️ Manage", "📥 Export"])

# ============ TAB 1: OCR / Paste ============
with tabs[0]:
    st.header("Add BOM from image or paste text")
    
    c1, c2, c3 = st.columns([2,1,1])
    with c1: pcode = st.text_input("Product Code *", key="pc1")
    with c2: pname = st.text_input("Product Name *", key="pn1")
    with c3: bsize = st.number_input("Batch Size", min_value=1, value=1, key="bs1")

    # --- Lens OCR section ---
    if LENS_AVAILABLE:
        st.markdown("#### 📷 Use Google Lens to read image")
        src = st.radio("Source", ["📷 Camera", "📁 Upload"],
                       horizontal=True, label_visibility="collapsed")
        image = None
        if src == "📷 Camera":
            shot = st.camera_input("Take a photo of BOM")
            if shot: image = Image.open(shot)
        else:
            up = st.file_uploader("Upload image", type=["png","jpg","jpeg","webp"])
            if up: image = Image.open(up)
        
        if image is not None:
            st.image(image, use_column_width=True)
            if st.button("🔍 Read with Google Lens"):
                with st.spinner("Lens is reading..."):
                    try:
                        st.session_state["ocr"] = lens_ocr(image)
                    except Exception as e:
                        st.error(f"Lens error: {e}")
    
    # --- Paste text box (always available) ---
    st.markdown("#### 📋 Or paste text copied from Google Lens / any source")
    default_text = st.session_state.get("ocr", "")
    raw = st.text_area(
        "BOM lines (format: CODE | NAME | QTY)",
        value="\n".join(l.strip() for l in default_text.splitlines() if l.strip()),
        height=280,
        placeholder="MAT-001 | Sugar | 2, 500\nMAT-002 | Cocoa | 500",
    )

    if st.button("💾 Save BOM"):
        if not pcode or not pname:
            st.error("Product code and name required.")
        else:
            mats, newly_pending = [], []
            for line in raw.splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 3: continue
                mc, mn, qraw = parts[0], parts[1], parts[2]
                q = parse_kg_gm_text(qraw, MODE)
                if q is None: continue
                mats.append({"code": mc, "name": mn, "qty_kg": q})
                if mc not in data["materials"] and mc not in data["pending"]:
                    data["pending"][mc] = {"name": mn, "unit": "kg"}
                    newly_pending.append(mc)
            data["products"][pcode] = {"name": pname, "batch_size": bsize,
                                        "materials": mats, "source": "lens_or_paste"}
            save_data(data)
            st.success(f"Saved {pcode} with {len(mats)} materials.")
            st.session_state.pop("ocr", None)
            st.rerun()

# ============ TAB 2: Materials ============
with tabs[1]:
    st.header("🧱 Raw Materials Master List")
    sub1, sub2 = st.tabs(["➕ Add one", "📤 Bulk upload"])
    
    with sub1:
        with st.form("add_mat", clear_on_submit=True):
            c1,c2,c3 = st.columns([1,2,1])
            with c1: mc = st.text_input("Code *", placeholder="MAT-001")
            with c2: mn = st.text_input("Name *", placeholder="Sugar")
            with c3: mu = st.selectbox("Unit", ["kg","gm","L","ml","pcs"])
            if st.form_submit_button("Add"):
                if mc and mn and mc not in data["materials"]:
                    data["materials"][mc] = {"name": mn, "unit": mu}
                    save_data(data); st.rerun()
                else:
                    st.error("Invalid or duplicate code")
    
    with sub2:
        tpl = pd.DataFrame([{"Code":"MAT-001","Name":"Sugar","Unit":"kg"}])
        st.download_button("⬇️ Template CSV",
            tpl.to_csv(index=False).encode(), "materials_template.csv", "text/csv")
        f = st.file_uploader("CSV/Excel", type=["csv","xlsx"])
        if f:
            df = pd.read_csv(f) if f.name.endswith(".csv") else pd.read_excel(f)
            df.columns = [c.strip() for c in df.columns]
            cmap = {c.lower(): c for c in df.columns}
            if "code" not in cmap or "name" not in cmap:
                st.error("Need Code and Name columns")
            else:
                for _, row in df.iterrows():
                    code = str(row[cmap["code"]]).strip()
                    name = str(row[cmap["name"]]).strip()
                    unit = str(row[cmap["unit"]]).strip() if "unit" in cmap else "kg"
                    if code and name and code not in data["materials"]:
                        data["materials"][code] = {"name": name, "unit": unit}
                save_data(data)
                st.success("Imported")
                st.rerun()
    
    st.markdown("---")
    if data["materials"]:
        st.dataframe(pd.DataFrame([
            {"Code":k,"Name":v["name"],"Unit":v["unit"]}
            for k,v in data["materials"].items()]), use_container_width=True)

# ============ TAB 3: Build Product ============
with tabs[2]:
    st.header("🛠️ Build Product")
    if not data["materials"]:
        st.warning("Add materials first.")
    else:
        c1,c2,c3 = st.columns([2,1,1])
        with c1: bp_code = st.text_input("Product Code *", key="bpc")
        with c2: bp_name = st.text_input("Product Name *", key="bpn")
        with c3: bp_batch = st.number_input("Batch Size", 1, value=1, key="bpb")
        
        if "draft" not in st.session_state:
            st.session_state.draft = [{"code":None,"kg":0,"gm":0.0}]
        
        mcodes = ["—"] + list(data["materials"].keys())
        labels = {c: f"{c} — {data['materials'][c]['name']}" for c in data["materials"]}
        labels["—"] = "— select —"
        
        for i, row in enumerate(st.session_state.draft):
            c1,c2,c3,c4 = st.columns([3,1,1,1])
            with c1:
                ch = st.selectbox(f"m{i}", mcodes,
                    format_func=lambda x: labels.get(x,x),
                    index=mcodes.index(row["code"]) if row["code"] in mcodes else 0,
                    key=f"s{i}", label_visibility="collapsed")
                row["code"] = None if ch=="—" else ch
            with c2:
                row["kg"] = st.number_input(f"kg{i}", 0, step=1,
                    value=int(row["kg"]), key=f"k{i}", label_visibility="collapsed")
            with c3:
                row["gm"] = st.number_input(f"gm{i}", 0.0, 999.999, step=0.1,
                    value=float(row["gm"]), key=f"g{i}",
                    format="%.3f", label_visibility="collapsed")
            with c4:
                if st.button("✖", key=f"x{i}") and len(st.session_state.draft)>1:
                    st.session_state.draft.pop(i); st.rerun()
        
        if st.button("➕ Add row"):
            st.session_state.draft.append({"code":None,"kg":0,"gm":0.0}); st.rerun()
        
        st.markdown("#### Preview")
        prev = []
        for r in st.session_state.draft:
            if not r["code"]: continue
            v = kg_gm_to_float(r["kg"], r["gm"])
            prev.append({"Code":r["code"],"Name":data["materials"][r["code"]]["name"],
                         "Display":fmt_kg_gm(v)})
        if prev: st.dataframe(pd.DataFrame(prev), hide_index=True, use_container_width=True)
        
        if st.button("💾 Save Product"):
            if bp_code and bp_name and any(r["code"] for r in st.session_state.draft):
                mats = [{"code":r["code"],"name":data["materials"][r["code"]]["name"],
                         "qty_kg":kg_gm_to_float(r["kg"],r["gm"])}
                        for r in st.session_state.draft if r["code"]]
                data["products"][bp_code] = {"name":bp_name,"batch_size":bp_batch,
                                              "materials":mats,"source":"manual"}
                save_data(data)
                st.session_state.draft = [{"code":None,"kg":0,"gm":0.0}]
                st.success(f"Saved {bp_code}"); st.rerun()
            else:
                st.error("Fill code, name, at least one material")

# ============ TAB 4: Pending ============
with tabs[3]:
    st.header("⏳ Pending Review")
    if not data["pending"]:
        st.success("None pending.")
    else:
        st.dataframe(pd.DataFrame([
            {"Code":k,"Name":v["name"],"Unit":v["unit"]}
            for k,v in data["pending"].items()]), use_container_width=True)
        ap = st.selectbox("Approve", ["—"]+list(data["pending"].keys()))
        if ap != "—":
            if st.button(f"✅ Approve {ap}"):
                if ap not in data["materials"]:
                    data["materials"][ap] = data["pending"][ap]
                del data["pending"][ap]; save_data(data); st.rerun()
        if st.button("✅ Approve ALL"):
            for k,v in list(data["pending"].items()):
                if k not in data["materials"]: data["materials"][k]=v
                del data["pending"][k]
            save_data(data); st.rerun()

# ============ TAB 5: Manage ============
with tabs[4]:
    st.header("Products")
    for pc, info in data["products"].items():
        with st.expander(f"📦 {pc} — {info['name']} (batch {info['batch_size']})"):
            st.dataframe(pd.DataFrame(info["materials"]), use_container_width=True)
            if st.button(f"🗑️ Delete {pc}", key=f"d{pc}"):
                del data["products"][pc]; save_data(data); st.rerun()

# ============ TAB 6: Export ============
with tabs[5]:
    st.header("Export Excel")
    if not data["products"]:
        st.info("No products yet.")
    else:
        idx = {}
        for info in data["products"].values():
            for m in info["materials"]:
                idx.setdefault(m["code"], m["name"])
        rows = []
        br = {"Material Code":"— BATCH SIZE —","Material Name":""}
        for pc,info in data["products"].items():
            br[f"{pc}\n{info['name']}"] = info["batch_size"]
        rows.append(br)
        for mc,mn in sorted(idx.items()):
            row = {"Material Code":mc,"Material Name":mn}
            for pc,info in data["products"].items():
                q = next((m["qty_kg"] for m in info["materials"] if m["code"]==mc), 0)
                row[f"{pc}\n{info['name']}"] = fmt_kg_gm(q) if q else ""
            rows.append(row)
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)
        out = BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="BOM")
        out.seek(0)
        st.download_button("⬇️ Download Excel", out,
            f"BOM_{datetime.now():%Y%m%d_%H%M}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
