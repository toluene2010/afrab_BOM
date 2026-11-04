import streamlit as st
import pytesseract
from PIL import Image
import pandas as pd
import os, json
from datetime import datetime
from io import BytesIO

# Windows: pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

DATA_FILE = "bom_data.json"
IMAGE_DIR = "bom_images"
os.makedirs(IMAGE_DIR, exist_ok=True)

# ---------- storage ----------
def load_data():
    d = json.load(open(DATA_FILE)) if os.path.exists(DATA_FILE) else {}
    d.setdefault("products", {})   # code -> {name, batch_size, materials[], source}
    d.setdefault("materials", {})  # code -> {name, unit}
    d.setdefault("pending", {})    # code -> {name, unit}
    return d

def save_data(d):
    json.dump(d, open(DATA_FILE, "w"), indent=2)

data = load_data()

# ---------- helpers ----------
def kg_gm_to_float(kg, gm):
    """kg + gm -> float kg. gm may be decimal (e.g. 0.2)."""
    return float(kg) + float(gm) / 1000.0

def fmt_kg_gm(v):
    """Format float kg -> '2 kg 500 gm', '0.2 gm', '2 kg 0.2 gm' etc."""
    if v is None:
        return ""
    total_gm = round(float(v) * 1000, 4)
    kg = int(total_gm // 1000)
    gm = round(total_gm - kg * 1000, 4)

    if abs(gm - 1000) < 1e-6:
        kg += 1
        gm = 0.0

    def fmt_gm(g):
        s = f"{g:.3f}".rstrip("0").rstrip(".")
        return s if s else "0"

    parts = []
    if kg:
        parts.append(f"{kg} kg")
    if gm > 0 or not kg:
        parts.append(f"{fmt_gm(gm)} gm")
    return " ".join(parts)

def parse_kg_gm_text(raw, mode="kg_gm"):
    if raw is None:
        return None
    s = str(raw).lower().replace("kg", "").replace("gm", "").replace("g", "").strip()
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

# ---------- UI ----------
st.set_page_config(page_title="BOM Digitizer", layout="wide")
st.title("📋 BOM Digitizer")

with st.sidebar:
    st.subheader("⚙️ Settings")
    unit_mode = st.radio(
        "Interpret commas in OCR as:",
        ["kg + gm (2, 500 = 2.5 kg)", "thousands (2,500 = 2500)"],
    )
    MODE = "kg_gm" if unit_mode.startswith("kg") else "decimal"

(tab_capture, tab_master, tab_build,
 tab_pending, tab_manage, tab_export) = st.tabs(
    ["📷 Capture", "🧱 Materials", "🛠️ Build Product",
     "⏳ Pending Review", "✏️ Manage", "📥 Export"]
)

# ============ TAB 1: OCR capture ============
with tab_capture:
    st.header("Add a BOM from image")
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        pcode = st.text_input("Product Code *", key="pc1")
    with col2:
        pname = st.text_input("Product Name *", key="pn1")
    with col3:
        bsize = st.number_input("Batch Size", min_value=1, value=1, key="bs1")

    src = st.radio(
        "Source", ["📷 Camera", "📁 Upload"],
        horizontal=True, label_visibility="collapsed"
    )
    image = None
    if src == "📷 Camera":
        shot = st.camera_input("Take a photo")
        if shot:
            image = Image.open(shot)
    else:
        up = st.file_uploader("Upload image", type=["png", "jpg", "jpeg", "webp"])
        if up:
            image = Image.open(up)

    if image is not None:
        st.image(image, use_column_width=True)
        if st.button("🔍 OCR"):
            with st.spinner("Reading..."):
                st.session_state["ocr"] = pytesseract.image_to_string(image)

        if "ocr" in st.session_state:
            st.info("Format each line: `CODE | NAME | QTY`  (QTY can be `2, 500`)")
            raw = st.text_area(
                "Edit lines",
                value="\n".join(
                    l.strip() for l in st.session_state["ocr"].splitlines() if l.strip()
                ),
                height=280,
            )

            if st.button("💾 Save BOM (OCR)"):
                if not pcode or not pname:
                    st.error("Product code and name required.")
                else:
                    mats = []
                    newly_pending = []
                    for line in raw.splitlines():
                        parts = [p.strip() for p in line.split("|")]
                        if len(parts) < 3:
                            continue
                        mc, mn, qraw = parts[0], parts[1], parts[2]
                        q = parse_kg_gm_text(qraw, MODE)
                        if q is None:
                            continue
                        mats.append({"code": mc, "name": mn, "qty_kg": q})
                        if mc not in data["materials"] and mc not in data["pending"]:
                            data["pending"][mc] = {"name": mn, "unit": "kg"}
                            newly_pending.append(mc)

                    data["products"][pcode] = {
                        "name": pname,
                        "batch_size": bsize,
                        "materials": mats,
                        "source": "ocr",
                    }
                    save_data(data)
                    msg = f"Saved {pcode} with {len(mats)} materials."
                    if newly_pending:
                        msg += (
                            f"  ⏳ {len(newly_pending)} new material(s) sent to Pending "
                            f"Review: " + ", ".join(newly_pending)
                        )
                    st.success(msg)
                    st.session_state.pop("ocr", None)
                    st.rerun()

# ============ TAB 2: Materials master list (WITH BULK UPLOAD) ============
with tab_master:
    st.header("🧱 Raw Materials Master List")
    st.caption("Register each material once. It appears in dropdowns everywhere else.")

    sub_single, sub_bulk = st.tabs(["➕ Add one", "📤 Bulk upload (CSV/Excel)"])

    with sub_single:
        with st.form("add_material", clear_on_submit=True):
            c1, c2, c3 = st.columns([1, 2, 1])
            with c1:
                mcode = st.text_input("Code *", placeholder="MAT-001")
            with c2:
                mname = st.text_input("Name *", placeholder="Sugar")
            with c3:
                munit = st.selectbox("Base Unit", ["kg", "gm", "L", "ml", "pcs"])
            if st.form_submit_button("➕ Add material"):
                if not mcode or not mname:
                    st.error("Code and name required")
                elif mcode in data["materials"]:
                    st.error(f"{mcode} already exists")
                else:
                    data["materials"][mcode] = {"name": mname, "unit": munit}
                    save_data(data)
                    st.success(f"Added {mcode}")
                    st.rerun()

    with sub_bulk:
        st.markdown("#### Upload a CSV or Excel file")
        st.caption(
            "File must have columns: **Code | Name | Unit** "
            "(Unit is optional — defaults to `kg`). "
            "Duplicates (existing codes) will be skipped."
        )

        template = pd.DataFrame([
            {"Code": "MAT-001", "Name": "Sugar", "Unit": "kg"},
            {"Code": "MAT-002", "Name": "Cocoa powder", "Unit": "kg"},
            {"Code": "MAT-003", "Name": "Flour", "Unit": "kg"},
        ])
        tpl_csv = template.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download template (CSV)",
            data=tpl_csv,
            file_name="materials_template.csv",
            mime="text/csv",
        )

        bulk_file = st.file_uploader(
            "Choose file", type=["csv", "xlsx", "xls"], key="bulk_mat"
        )

        if bulk_file is not None:
            try:
                if bulk_file.name.lower().endswith(".csv"):
                    df_in = pd.read_csv(bulk_file)
                else:
                    df_in = pd.read_excel(bulk_file)

                df_in.columns = [str(c).strip() for c in df_in.columns]
                col_map = {c.lower(): c for c in df_in.columns}

                code_col = col_map.get("code")
                name_col = col_map.get("name")
                unit_col = col_map.get("unit")

                if not code_col or not name_col:
                    st.error(
                        "File must contain at least **Code** and **Name** columns. "
                        f"Found columns: {list(df_in.columns)}"
                    )
                else:
                    st.markdown("#### Preview")
                    st.dataframe(df_in.head(20), use_container_width=True)

                    issues = []
                    valid_rows = []
                    seen_in_file = set()
                    for i, row in df_in.iterrows():
                        code = (
                            str(row[code_col]).strip()
                            if pd.notna(row[code_col]) else ""
                        )
                        name = (
                            str(row[name_col]).strip()
                            if pd.notna(row[name_col]) else ""
                        )
                        unit = (
                            str(row[unit_col]).strip()
                            if unit_col and pd.notna(row[unit_col])
                            else "kg"
                        )

                        if not code:
                            issues.append(f"Row {i+2}: missing Code — skipped")
                            continue
                        if not name:
                            issues.append(f"Row {i+2}: missing Name — skipped")
                            continue
                        if code in data["materials"]:
                            issues.append(f"Row {i+2}: {code} already exists — skipped")
                            continue
                        if code in seen_in_file:
                            issues.append(f"Row {i+2}: {code} duplicate in file — skipped")
                            continue

                        seen_in_file.add(code)
                        valid_rows.append({"code": code, "name": name, "unit": unit})

                    st.markdown(
                        f"#### Summary: **{len(valid_rows)}** rows will be imported"
                    )
                    if issues:
                        with st.expander(f"⚠️ {len(issues)} issue(s)"):
                            for msg in issues:
                                st.write("• " + msg)

                    if valid_rows:
                        st.dataframe(
                            pd.DataFrame(valid_rows), use_container_width=True
                        )
                        if st.button("✅ Import these materials"):
                            for r in valid_rows:
                                data["materials"][r["code"]] = {
                                    "name": r["name"],
                                    "unit": r["unit"],
                                }
                            save_data(data)
                            st.success(
                                f"Imported {len(valid_rows)} materials. "
                                "Check the master list below."
                            )
                            st.rerun()
            except Exception as e:
                st.error(f"Could not read file: {e}")

    st.markdown("---")
    st.markdown(f"#### Registered materials ({len(data['materials'])})")
    if not data["materials"]:
        st.info("None yet — add or upload above.")
    else:
        mat_df = pd.DataFrame([
            {"Code": k, "Name": v["name"], "Unit": v["unit"]}
            for k, v in data["materials"].items()
        ])
        st.dataframe(mat_df, use_container_width=True, hide_index=True)

        to_del = st.selectbox(
            "Delete material", ["—"] + list(data["materials"].keys())
        )
        if to_del != "—" and st.button(f"🗑️ Delete {to_del}"):
            del data["materials"][to_del]
            save_data(data)
            st.rerun()

# ============ TAB 3: Build Product ============
with tab_build:
    st.header("🛠️ Build a Product from Materials")

    if not data["materials"]:
        st.warning("Add materials first in the 🧱 Materials tab.")
    else:
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            bp_code = st.text_input("Product Code *", key="bp_code")
        with c2:
            bp_name = st.text_input("Product Name *", key="bp_name")
        with c3:
            bp_batch = st.number_input(
                "Batch Size", min_value=1, value=1, key="bp_batch"
            )

        if "draft" not in st.session_state:
            st.session_state.draft = [{"code": None, "kg": 0, "gm": 0.0}]

        st.markdown("#### Materials in this product")

        h = st.columns([3, 1, 1, 1, 1])
        h[0].write("**Material**")
        h[1].write("**kg**")
        h[2].write("**gm**")
        h[3].write("")
        h[4].write("")

        mat_codes = ["—"] + list(data["materials"].keys())
        mat_labels = {
            c: f"{c} — {data['materials'][c]['name']}" for c in data["materials"]
        }
        mat_labels["—"] = "— select —"

        for i, row in enumerate(st.session_state.draft):
            c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
            with c1:
                chosen = st.selectbox(
                    f"mat_{i}",
                    mat_codes,
                    format_func=lambda x: mat_labels.get(x, x),
                    index=(
                        mat_codes.index(row["code"])
                        if row["code"] in mat_codes else 0
                    ),
                    key=f"sel_{i}",
                    label_visibility="collapsed",
                )
                row["code"] = None if chosen == "—" else chosen
            with c2:
                row["kg"] = st.number_input(
                    f"kg_{i}",
                    min_value=0,
                    step=1,
                    value=int(row["kg"]),
                    key=f"kg_{i}",
                    label_visibility="collapsed",
                )
            with c3:
                row["gm"] = st.number_input(
                    f"gm_{i}",
                    min_value=0.0,
                    max_value=999.999,
                    step=0.1,
                    value=float(row["gm"]),
                    key=f"gm_{i}",
                    label_visibility="collapsed",
                    format="%.3f",
                )
            with c4:
                if st.button("✖", key=f"rm_{i}") and len(st.session_state.draft) > 1:
                    st.session_state.draft.pop(i)
                    st.rerun()
            with c5:
                st.write("")

        if st.button("➕ Add another material"):
            st.session_state.draft.append({"code": None, "kg": 0, "gm": 0.0})
            st.rerun()

        st.markdown("#### Preview")
        preview = []
        for r in st.session_state.draft:
            if not r["code"]:
                continue
            v = kg_gm_to_float(r["kg"], r["gm"])
            preview.append({
                "Code": r["code"],
                "Name": data["materials"][r["code"]]["name"],
                "kg": r["kg"],
                "gm": r["gm"],
                "Total kg": round(v, 5),
                "Display": fmt_kg_gm(v),
            })
        if preview:
            st.dataframe(
                pd.DataFrame(preview), hide_index=True, use_container_width=True
            )

        st.markdown("---")
        if st.button("💾 Save Product"):
            if not bp_code or not bp_name:
                st.error("Product code and name required.")
            elif not any(r["code"] for r in st.session_state.draft):
                st.error("Add at least one material.")
            else:
                mats = []
                for r in st.session_state.draft:
                    if not r["code"]:
                        continue
                    mats.append({
                        "code": r["code"],
                        "name": data["materials"][r["code"]]["name"],
                        "qty_kg": kg_gm_to_float(r["kg"], r["gm"]),
                    })
                data["products"][bp_code] = {
                    "name": bp_name,
                    "batch_size": bp_batch,
                    "materials": mats,
                    "source": "manual",
                }
                save_data(data)
                st.session_state.draft = [{"code": None, "kg": 0, "gm": 0.0}]
                st.success(f"Saved {bp_code} with {len(mats)} materials")
                st.rerun()

# ============ TAB 4: Pending Review ============
with tab_pending:
    st.header("⏳ Pending Material Review")
    st.caption(
        "Materials seen by OCR but not yet approved into the master list. "
        "Approve to add them, or reject to discard."
    )

    if not data["pending"]:
        st.success("No pending materials. 🎉")
    else:
        rows = [
            {"Code": code, "Name": info["name"], "Unit": info["unit"]}
            for code, info in data["pending"].items()
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.markdown("#### Approve")
        ap_codes = ["—"] + list(data["pending"].keys())
        ap_code = st.selectbox("Material to approve", ap_codes, key="ap_code")
        if ap_code != "—":
            c1, c2, c3 = st.columns([1, 2, 1])
            with c1:
                new_code = st.text_input("Code", value=ap_code, key="ap_newcode")
            with c2:
                new_name = st.text_input(
                    "Name",
                    value=data["pending"][ap_code]["name"],
                    key="ap_newname",
                )
            with c3:
                units = ["kg", "gm", "L", "ml", "pcs"]
                new_unit = st.selectbox(
                    "Unit",
                    units,
                    index=units.index(data["pending"][ap_code]["unit"]),
                    key="ap_newunit",
                )

            c_ap, c_rej = st.columns(2)
            with c_ap:
                if st.button("✅ Approve into master list"):
                    if new_code in data["materials"]:
                        st.error(f"{new_code} already in master list.")
                    else:
                        data["materials"][new_code] = {
                            "name": new_name,
                            "unit": new_unit,
                        }
                        del data["pending"][ap_code]
                        if new_code != ap_code:
                            for pinfo in data["products"].values():
                                for m in pinfo["materials"]:
                                    if m["code"] == ap_code:
                                        m["code"] = new_code
                                        m["name"] = new_name
                        save_data(data)
                        st.success(f"Approved {new_code}")
                        st.rerun()
            with c_rej:
                if st.button("❌ Reject (discard)"):
                    del data["pending"][ap_code]
                    save_data(data)
                    st.success(f"Rejected {ap_code}")
                    st.rerun()

        st.markdown("---")
        st.markdown("#### Approve all / Reject all")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Approve ALL as-is"):
                for code, info in list(data["pending"].items()):
                    if code not in data["materials"]:
                        data["materials"][code] = info
                    del data["pending"][code]
                save_data(data)
                st.success("All pending approved.")
                st.rerun()
        with c2:
            if st.button("❌ Reject ALL"):
                data["pending"] = {}
                save_data(data)
                st.warning("All pending rejected.")
                st.rerun()

# ============ TAB 5: Manage ============
with tab_manage:
    st.header("Saved Products")
    if not data["products"]:
        st.info("No products yet.")
    for pc, info in data["products"].items():
        with st.expander(
            f"📦 {pc} — {info['name']}  "
            f"(batch {info['batch_size']}, {info.get('source', '?')})"
        ):
            df = pd.DataFrame(info["materials"])
            if not df.empty:
                df["display"] = df["qty_kg"].apply(fmt_kg_gm)
            st.dataframe(df, use_container_width=True)
            if st.button(f"🗑️ Delete {pc}", key=f"del_{pc}"):
                del data["products"][pc]
                save_data(data)
                st.rerun()

# ============ TAB 6: Export ============
with tab_export:
    st.header("Export to Excel")
    if not data["products"]:
        st.info("Add a product first.")
    else:
        mat_index = {}
        for info in data["products"].values():
            for m in info["materials"]:
                mat_index.setdefault(m["code"], m["name"])

        rows = []
        batch_row = {"Material Code": "— BATCH SIZE —", "Material Name": ""}
        for pc, info in data["products"].items():
            batch_row[f"{pc}\n{info['name']}"] = info["batch_size"]
        rows.append(batch_row)

        for mc, mn in sorted(mat_index.items()):
            row = {"Material Code": mc, "Material Name": mn}
            for pc, info in data["products"].items():
                q = next(
                    (m["qty_kg"] for m in info["materials"] if m["code"] == mc), 0
                )
                row[f"{pc}\n{info['name']}"] = fmt_kg_gm(q) if q else ""
            rows.append(row)

        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)

        out = BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="BOM")
        out.seek(0)
        st.download_button(
            "⬇️ Download Excel",
            data=out,
            file_name=f"BOM_{datetime.now():%Y%m%d_%H%M}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
