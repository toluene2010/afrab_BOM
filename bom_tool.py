import streamlit as st
import pandas as pd
import os
import json
import re
import math
from datetime import datetime
from io import BytesIO

# ============================================================
# BOM DIGITIZER
# Local JSON + optional GitHub Gist storage
# KG and GM are stored and displayed in separate columns.
# OCR-extracted records remain editable before saving.
# ============================================================

st.set_page_config(page_title="BOM Digitizer", page_icon="📋", layout="wide")

APP_TITLE = "BOM Digitizer"
DATA_FILE = os.getenv("BOM_DATA_FILE", "bom_data.json")
GIST_FILENAME = "bom_data.json"
GITHUB_API = "https://api.github.com"

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    requests = None
    REQUESTS_OK = False


# ============================================================
# 1. GENERAL HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "null") else text


def clean_code(value):
    return clean_text(value).upper()


def safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def fmt_number(value, decimals=6):
    text = f"{safe_float(value):.{decimals}f}".rstrip("0").rstrip(".")
    return text or "0"


def is_mass_unit(unit):
    return clean_text(unit).lower() in ("kg", "gm", "g")


def convert_quantity(value, source_unit, target_unit):
    value = safe_float(value)
    source = clean_text(source_unit).lower()
    target = clean_text(target_unit).lower()
    aliases = {"g": "gm"}
    source = aliases.get(source, source)
    target = aliases.get(target, target)
    if source == target:
        return value
    conversions = {
        ("kg", "gm"): 1000, ("gm", "kg"): 0.001,
        ("l", "ml"): 1000, ("ml", "l"): 0.001,
    }
    factor = conversions.get((source, target))
    if factor is None:
        raise ValueError(f"Cannot convert {source_unit} to {target_unit}.")
    return value * factor


def parse_mass_quantity(raw, mode="kg_gm"):
    """Return (kg, gm) from text such as '2 kg 10.5 gm'."""
    text = clean_text(raw).lower().replace(",", "").strip()
    if not text:
        return None

    # Explicit combined quantity: 2 kg 10.5 gm
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*kg(?:\s*"
        r"(\d+(?:\.\d+)?)\s*(?:gm|g))?\s*", text
    )
    if match:
        return safe_float(match.group(1)), safe_float(match.group(2))

    # Grams only
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:gm|g)\s*", text)
    if match:
        return 0.0, safe_float(match.group(1))

    # Kilograms only
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*kg\s*", text)
    if match:
        return safe_float(match.group(1)), 0.0

    # Legacy comma notation. In kg_gm mode, 2,500 means 2.5 kg,
    # preserving the interpretation used by the earlier application.
    raw_text = clean_text(raw).lower().replace(" ", "")
    if re.fullmatch(r"\d+,\d{1,3}", raw_text):
        left, right = raw_text.split(",", 1)
        if mode == "kg_gm":
            return safe_float(left) + safe_float("0." + right), 0.0
        return safe_float(left + right), 0.0

    # A plain decimal number is treated as kilograms.
    if re.fullmatch(r"\d+(?:\.\d+)?", raw_text):
        return safe_float(raw_text), 0.0

    return None


def parse_other_quantity(raw, target_unit):
    text = clean_text(raw).lower().replace(",", "").strip()
    if not text:
        return None
    match = re.fullmatch(r"([+]?\d+(?:\.\d+)?)\s*([a-z]+)?", text)
    if not match:
        return None
    value, source_unit = match.groups()
    value = safe_float(value, -1)
    if value < 0:
        return None
    if not source_unit:
        return value
    source_unit = {"pc": "pcs", "g": "gm"}.get(source_unit, source_unit)
    target_unit = clean_text(target_unit).lower()
    try:
        return convert_quantity(value, source_unit, target_unit)
    except ValueError:
        return value if source_unit == target_unit else None


def format_quantity(value, unit):
    return f"{fmt_number(value)} {clean_text(unit) or 'unit'}"


# ============================================================
# 2. LOCAL STORAGE
# ============================================================

def empty_database():
    return {"products": {}, "materials": {}, "pending": {}}


def _local_load():
    if not os.path.exists(DATA_FILE):
        return empty_database()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            content = file.read().strip()
        result = json.loads(content) if content else empty_database()
        if not isinstance(result, dict):
            raise ValueError("Invalid database structure.")
        return result
    except (json.JSONDecodeError, OSError, ValueError) as error:
        backup = f"{DATA_FILE}.corrupt_{datetime.now():%Y%m%d_%H%M%S}"
        try:
            os.rename(DATA_FILE, backup)
        except OSError:
            pass
        st.session_state["_storage_warning"] = (
            f"Could not read local database: {error}. Backup attempted: {backup}"
        )
        return empty_database()


def _local_save(database):
    directory = os.path.dirname(os.path.abspath(DATA_FILE))
    os.makedirs(directory, exist_ok=True)
    temporary_file = DATA_FILE + ".tmp"
    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(database, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_file, DATA_FILE)
    except Exception:
        try:
            if os.path.exists(temporary_file):
                os.remove(temporary_file)
        except OSError:
            pass
        raise


# ============================================================
# 3. GITHUB GIST STORAGE
# ============================================================

def _gist_config():
    try:
        github = st.secrets["github"]
        token = str(github["token"]).strip()
        gist_id = str(github["gist_id"]).strip()
        if token and gist_id:
            return token, gist_id
    except Exception:
        pass
    return None, None


def _gist_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


def _gist_load(token, gist_id):
    if not REQUESTS_OK:
        raise RuntimeError("Requests is not installed.")
    response = requests.get(
        f"{GITHUB_API}/gists/{gist_id}",
        headers=_gist_headers(token), timeout=15
    )
    response.raise_for_status()
    files = response.json().get("files", {})
    if GIST_FILENAME not in files:
        raise ValueError(f"{GIST_FILENAME} was not found in configured Gist.")
    content = files[GIST_FILENAME].get("content", "")
    result = json.loads(content) if content.strip() else empty_database()
    if not isinstance(result, dict):
        raise ValueError("Invalid Gist database structure.")
    return result


def _gist_save(token, gist_id, database):
    if not REQUESTS_OK:
        raise RuntimeError("Requests is not installed.")
    payload = {"files": {GIST_FILENAME: {
        "content": json.dumps(database, ensure_ascii=False, indent=2, allow_nan=False)
    }}}
    response = requests.patch(
        f"{GITHUB_API}/gists/{gist_id}", headers=_gist_headers(token),
        json=payload, timeout=20
    )
    response.raise_for_status()
    if GIST_FILENAME not in response.json().get("files", {}):
        raise RuntimeError("GitHub did not confirm the expected database file.")
    return True


# ============================================================
# 4. DATABASE NORMALIZATION AND QUANTITY MODEL
# ============================================================

def material_unit(database, code):
    material = database["materials"].get(code, {})
    return clean_text(material.get("unit", "kg")) or "kg"


def get_mass_parts(entry, unit="kg"):
    """Read new KG/GM fields and safely support older saved BOM records."""
    if "qty_gm" in entry or "qty_kg" in entry:
        kg = safe_float(entry.get("qty_kg", 0))
        gm = safe_float(entry.get("qty_gm", 0))
        # Legacy records used qty_kg as the complete decimal KG amount.
        return kg, gm

    if "qty" in entry:
        qty = safe_float(entry.get("qty", 0))
        if clean_text(entry.get("unit", unit)).lower() in ("gm", "g"):
            return 0.0, qty
        return qty, 0.0

    return safe_float(entry.get("qty_kg", 0)), 0.0


def create_bom_entry(code, name, quantity, unit):
    unit = clean_text(unit) or "kg"
    if is_mass_unit(unit):
        if unit.lower() in ("gm", "g"):
            kg, gm = 0.0, safe_float(quantity)
        else:
            kg, gm = safe_float(quantity), 0.0
        return {
            "code": code, "name": name, "unit": "kg",
            "qty_kg": kg, "qty_gm": gm
        }
    return {
        "code": code, "name": name, "unit": unit,
        "qty": safe_float(quantity)
    }


def create_bom_entry_parts(code, name, kg, gm):
    return {
        "code": code, "name": name, "unit": "kg",
        "qty_kg": safe_float(kg), "qty_gm": safe_float(gm)
    }


def _normalize(database):
    if not isinstance(database, dict):
        database = empty_database()
    for key in ("products", "materials", "pending"):
        if not isinstance(database.get(key), dict):
            database[key] = {}

    for section in ("materials", "pending"):
        for code, material in list(database[section].items()):
            if not isinstance(material, dict):
                database[section][code] = {"name": str(material), "unit": "kg"}
            else:
                material.setdefault("name", "")
                material.setdefault("unit", "kg")

    for code, product in list(database["products"].items()):
        if not isinstance(product, dict):
            database["products"].pop(code)
            continue
        product.setdefault("name", "")
        product.setdefault("batch_size", 1)
        product.setdefault("source", "legacy")
        if not isinstance(product.get("materials"), list):
            product["materials"] = []

        valid = []
        for item in product["materials"]:
            if not isinstance(item, dict):
                continue
            item.setdefault("code", "")
            item.setdefault("name", "")
            unit = clean_text(item.get("unit", "kg")) or "kg"

            # Convert old mass records to the new two-column representation.
            if is_mass_unit(unit):
                if "qty_gm" not in item:
                    if "qty" in item:
                        qty = safe_float(item.get("qty", 0))
                        if unit.lower() in ("gm", "g"):
                            item["qty_kg"], item["qty_gm"] = 0.0, qty
                        else:
                            item["qty_kg"], item["qty_gm"] = qty, 0.0
                    else:
                        item["qty_kg"] = safe_float(item.get("qty_kg", 0))
                        item["qty_gm"] = 0.0
                item["unit"] = "kg"
                item["qty_kg"] = safe_float(item.get("qty_kg", 0))
                item["qty_gm"] = safe_float(item.get("qty_gm", 0))
                item.pop("qty", None)
            else:
                if "qty" not in item:
                    item["qty"] = safe_float(item.get("qty_kg", 0))
                item["unit"] = unit
            valid.append(item)
        product["materials"] = valid
    return database


def load_data(force=False):
    if st.session_state.get("_bom_loaded") and not force:
        return st.session_state["_bom_database"]

    token, gist_id = _gist_config() if REQUESTS_OK else (None, None)
    database = None
    if token and gist_id:
        try:
            database = _gist_load(token, gist_id)
            if not any(database.get(key) for key in ("products", "materials", "pending")):
                local = _local_load()
                if any(local.get(key) for key in ("products", "materials", "pending")):
                    database = local
                    try:
                        _gist_save(token, gist_id, _normalize(database))
                        st.session_state["_migrated"] = True
                    except Exception as error:
                        st.session_state["_gist_err"] = str(error)
            st.session_state["_storage_mode"] = "Gist"
            st.session_state["_gist_connected"] = True
        except Exception as error:
            st.session_state["_gist_err"] = str(error)
            database = _local_load()
            st.session_state["_storage_mode"] = "Local fallback"
            st.session_state["_gist_connected"] = False
    else:
        database = _local_load()
        st.session_state["_storage_mode"] = "Local"
        st.session_state["_gist_connected"] = False

    database = _normalize(database)
    st.session_state["_bom_database"] = database
    st.session_state["_bom_loaded"] = True
    return database


def save_data(database):
    database = _normalize(database)
    try:
        _local_save(database)
    except Exception as error:
        st.error(f"Local database save failed: {error}")
        return False

    st.session_state["_bom_database"] = database
    st.session_state["_bom_loaded"] = True
    token, gist_id = _gist_config() if REQUESTS_OK else (None, None)
    if token and gist_id:
        try:
            _gist_save(token, gist_id, database)
            st.session_state["_storage_mode"] = "Gist"
            st.session_state["_gist_connected"] = True
            st.session_state.pop("_gist_err", None)
        except Exception as error:
            st.session_state["_storage_mode"] = "Local fallback"
            st.session_state["_gist_connected"] = False
            st.session_state["_gist_err"] = str(error)
            st.warning(
                "Local save succeeded, but GitHub synchronization failed. "
                "Your local data has been preserved."
            )
    return True


# ============================================================
# 5. OCR SMART EXTRACTION
# ============================================================

MATERIAL_CODE_PATTERN = re.compile(
    r"^[A-Z]{1,5}[-/]?\d{2,6}[A-Z]?$", re.IGNORECASE
)
NUMBER_PATTERN = re.compile(
    r"^(?:[+-]?\d[\d,.]*\s*kg(?:\s*\d[\d,.]*\s*(?:gm|g))?"
    r"|[+-]?\d[\d,.]*\s*(?:gm|g|l|ml|pcs))$",
    re.IGNORECASE
)
STOP_WORDS = [
    "PRODUCTION MANAGER", "STORE KEEPER", "SIGNATURE",
    "ORIGINAL", "APPROVED BY", "CHECKED BY", "DATE:"
]


def extract_ocr_materials(raw):
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    extracted = []
    current_code, current_name, current_numbers = None, [], []

    def save_current():
        if current_code:
            name = " ".join(current_name).strip() or "Unnamed material"
            quantity = " ".join(current_numbers) if current_numbers else "0"
            extracted.append({
                "Material Code": current_code,
                "Material Name": name,
                "Quantity Text": quantity
            })

    for line in lines:
        if any(word in line.upper() for word in STOP_WORDS):
            continue
        first_part = line.split(maxsplit=1)[0].strip()
        if MATERIAL_CODE_PATTERN.fullmatch(first_part):
            save_current()
            current_code = clean_code(first_part)
            current_name, current_numbers = [], []
            remaining = line[len(first_part):].strip()
            if remaining:
                current_name.append(remaining)
            continue
        if not current_code:
            continue
        if NUMBER_PATTERN.fullmatch(line):
            current_numbers.append(line)
        else:
            current_name.append(line)

    save_current()
    return extracted


def extraction_rows_from_records(records, database, mode):
    rows = []
    for record in records:
        code = clean_code(record.get("Material Code"))
        name = clean_text(record.get("Material Name"))
        raw_qty = clean_text(record.get("Quantity Text"))
        unit = material_unit(database, code)
        if is_mass_unit(unit):
            parts = parse_mass_quantity(raw_qty, mode)
            kg, gm = parts if parts is not None else (0.0, 0.0)
            rows.append({
                "Material Code": code, "Material Name": name,
                "KG": kg, "GM": gm, "Other Quantity": 0.0,
                "Unit": "kg", "Quantity Text": raw_qty
            })
        else:
            qty = parse_other_quantity(raw_qty, unit)
            rows.append({
                "Material Code": code, "Material Name": name,
                "KG": 0.0, "GM": 0.0,
                "Other Quantity": qty if qty is not None else 0.0,
                "Unit": unit, "Quantity Text": raw_qty
            })
    return rows


# ============================================================
# 6. BULK IMPORT HELPERS
# ============================================================

def read_material_file(uploaded_file):
    filename = uploaded_file.name.lower()
    if filename.endswith(".csv"):
        dataframe = pd.read_csv(uploaded_file, dtype=str, keep_default_na=False)
    elif filename.endswith(".xlsx"):
        dataframe = pd.read_excel(uploaded_file, dtype=str)
    else:
        raise ValueError("Unsupported file format.")
    dataframe.columns = [clean_text(column).lower() for column in dataframe.columns]
    return dataframe


def prepare_material_import(dataframe):
    columns = {column.lower(): column for column in dataframe.columns}
    if "code" not in columns or "name" not in columns:
        raise ValueError("File must contain Code and Name columns.")
    records = []
    for _, row in dataframe.iterrows():
        code = clean_code(row[columns["code"]])
        name = clean_text(row[columns["name"]])
        unit = clean_text(row[columns["unit"]]) if "unit" in columns else "kg"
        unit = unit or "kg"
        if code and name:
            records.append({"code": code, "name": name, "unit": unit})
    return records


# ============================================================
# 7. SESSION INITIALIZATION AND DATABASE LOAD
# ============================================================

if "draft" not in st.session_state:
    st.session_state["draft"] = [{"id": 1, "code": None, "quantity": 0.0}]
if "draft_next_id" not in st.session_state:
    st.session_state["draft_next_id"] = 2
if "paste_mode" not in st.session_state:
    st.session_state["paste_mode"] = "kg_gm"
if "_extraction_rows" not in st.session_state:
    st.session_state["_extraction_rows"] = []
if "_extraction_revision" not in st.session_state:
    st.session_state["_extraction_revision"] = 0

force_reload = st.session_state.pop("_force_reload", False)
data = load_data(force=force_reload)


# ============================================================
# 8. HEADER AND SIDEBAR
# ============================================================

st.title("📋 BOM Digitizer")
st.caption(
    "Bill of Materials management, editable OCR extraction, "
    "separate KG/GM quantities and Excel export."
)

if st.session_state.pop("_migrated", False):
    st.success("Local database successfully migrated to GitHub Gist.")
if "_storage_warning" in st.session_state:
    st.warning(st.session_state.pop("_storage_warning"))
if "_gist_err" in st.session_state:
    st.warning("GitHub Gist synchronization issue: " + st.session_state.pop("_gist_err"))

with st.sidebar:
    st.subheader("⚙️ Settings")
    unit_mode = st.radio(
        "Interpret legacy comma quantities:",
        ["Decimal KG (2,500 = 2.5 kg)", "Thousands (2,500 = 2500)"],
        index=0 if st.session_state["paste_mode"] == "kg_gm" else 1
    )
    MODE = "kg_gm" if unit_mode.startswith("Decimal") else "decimal"
    st.session_state["paste_mode"] = MODE
    st.markdown("---")

    token, gist_id = _gist_config() if REQUESTS_OK else (None, None)
    if token and gist_id:
        st.success("☁️ GitHub Gist connected") if st.session_state.get("_gist_connected") else st.warning("GitHub configured – local fallback active")
    else:
        st.warning("Local storage only")
        st.caption("Configure GitHub credentials in Streamlit Secrets to enable cloud synchronization.")

    st.caption(f"Storage mode: {st.session_state.get('_storage_mode', 'Local')}")
    st.markdown("---")
    if st.button("🔄 Refresh database from storage"):
        st.session_state["_force_reload"] = True
        st.rerun()
    st.markdown("---")
    st.metric("Registered Materials", len(data["materials"]))
    st.metric("Registered Products", len(data["products"]))
    st.metric("Pending Materials", len(data["pending"]))


# ============================================================
# 9. APPLICATION TABS
# ============================================================

tabs = st.tabs([
    "📷 Paste / OCR", "🧱 Materials", "🛠️ Build",
    "⏳ Pending", "✏️ Manage", "📥 Export"
])


# ============================================================
# TAB 1: PASTE AND EDIT OCR EXTRACTION
# ============================================================

with tabs[0]:
    st.header("Add BOM from Text or OCR")
    st.caption(
        "Copy text from a BOM photograph using Google Lens, paste it, "
        "extract the rows, then correct every field in the editable table."
    )

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        pcode = st.text_input("Product Code *", key="pc1", placeholder="e.g. AFRABORN")
    with c2:
        pname = st.text_input("Product Name *", key="pn1", placeholder="e.g. AFRABORN SYRUP")
    with c3:
        bsize = st.number_input("Batch Size", min_value=1, value=1, key="bs1")

    raw = st.text_area(
        "1. Paste raw Google Lens text here",
        height=200, placeholder="Paste copied BOM text here...", key="paste_box"
    )

    if st.button("⚙️ Auto-Extract Data", key="extract_bom"):
        if not raw.strip():
            st.warning("Paste the BOM text first.")
        else:
            records = extract_ocr_materials(raw)
            rows = extraction_rows_from_records(records, data, MODE)
            st.session_state["_extraction_rows"] = rows
            st.session_state["_extraction_revision"] += 1
            if rows:
                st.success(f"Extraction completed: {len(rows)} material rows found.")
            else:
                st.warning(
                    "No recognizable material codes were found. "
                    "You can still add rows manually in the review table."
                )
            st.rerun()

    st.markdown("### 2. Verify and correct extracted quantities")
    st.info(
        "KG and GM are independent columns. GM accepts decimal values "
        "(for example 10.50 or 300.75). You can edit names, codes and figures, "
        "add missing rows, or delete incorrect rows before saving."
    )

    if not st.session_state["_extraction_rows"]:
        st.session_state["_extraction_rows"] = [{
            "Material Code": "", "Material Name": "",
            "KG": 0.0, "GM": 0.0, "Other Quantity": 0.0,
            "Unit": "kg", "Quantity Text": ""
        }]

    editor_key = f"extraction_editor_{st.session_state['_extraction_revision']}"
    extraction_df = pd.DataFrame(st.session_state["_extraction_rows"])
    edited_df = st.data_editor(
        extraction_df,
        key=editor_key,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Material Code": st.column_config.TextColumn("Material Code", required=True),
            "Material Name": st.column_config.TextColumn("Material Name", required=True),
            "KG": st.column_config.NumberColumn("Quantity (KG)", min_value=0.0, step=0.001, format="%.6f"),
            "GM": st.column_config.NumberColumn("Quantity (GM)", min_value=0.0, step=0.01, format="%.6f"),
            "Other Quantity": st.column_config.NumberColumn("Other Quantity", min_value=0.0, step=0.01, format="%.6f"),
            "Unit": st.column_config.TextColumn("Unit"),
            "Quantity Text": st.column_config.TextColumn("Original OCR Text")
        }
    )

    if st.button("➕ Add blank material row", key="add_extraction_row"):
        new_rows = edited_df.to_dict("records")
        new_rows.append({
            "Material Code": "", "Material Name": "",
            "KG": 0.0, "GM": 0.0, "Other Quantity": 0.0,
            "Unit": "kg", "Quantity Text": ""
        })
        st.session_state["_extraction_rows"] = new_rows
        st.session_state["_extraction_revision"] += 1
        st.rerun()

    if st.button("💾 Save Verified BOM", key="save_pasted_bom"):
        product_code = clean_code(pcode)
        product_name = clean_text(pname)
        errors = []
        materials = []
        pending_materials = []

        if not product_code or not product_name:
            st.error("Product code and product name are required.")
        else:
            for index, row in edited_df.iterrows():
                mc = clean_code(row.get("Material Code"))
                mn = clean_text(row.get("Material Name"))
                if not mc and not mn:
                    continue
                if not mc or not mn:
                    errors.append(f"Row {index + 1}: Material code or name is missing.")
                    continue

                registered_unit = material_unit(data, mc)
                row_unit = clean_text(row.get("Unit")) or registered_unit

                if is_mass_unit(registered_unit) or is_mass_unit(row_unit):
                    kg = safe_float(row.get("KG"))
                    gm = safe_float(row.get("GM"))
                    if kg < 0 or gm < 0:
                        errors.append(f"Row {index + 1}: KG and GM cannot be negative.")
                        continue
                    materials.append(create_bom_entry_parts(mc, mn, kg, gm))
                    selected_unit = "kg"
                else:
                    qty = safe_float(row.get("Other Quantity"))
                    if qty < 0:
                        errors.append(f"Row {index + 1}: Quantity cannot be negative.")
                        continue
                    selected_unit = registered_unit
                    materials.append(create_bom_entry(mc, mn, qty, selected_unit))

                if mc not in data["materials"] and mc not in data["pending"]:
                    data["pending"][mc] = {"name": mn, "unit": selected_unit}
                    pending_materials.append(mc)

            if errors:
                with st.expander(f"⚠️ Review {len(errors)} issue(s)", expanded=True):
                    for error in errors:
                        st.write(error)

            if not materials:
                st.error("No valid material rows found. Correct the table before saving.")
            elif not errors:
                if product_code in data["products"]:
                    st.warning(f"{product_code} already exists. Saving replaces its current BOM.")
                data["products"][product_code] = {
                    "name": product_name,
                    "batch_size": bsize,
                    "materials": materials,
                    "source": "verified_ocr_or_paste"
                }
                if save_data(data):
                    st.success(
                        f"Saved {product_code} with {len(materials)} verified material rows."
                    )
                    if pending_materials:
                        st.info(f"{len(pending_materials)} new materials added to Pending.")
                    st.session_state["_extraction_rows"] = []
                    st.session_state["_extraction_revision"] += 1
                    st.rerun()


# ============================================================
# TAB 2: MATERIALS MASTER
# ============================================================

with tabs[1]:
    st.header("🧱 Materials Master")
    sub1, sub2 = st.tabs(["➕ Add One", "📤 Bulk Import"])

    with sub1:
        with st.form("add_mat", clear_on_submit=True):
            c1, c2, c3 = st.columns([1, 2, 1])
            with c1:
                mc = st.text_input("Material Code *", placeholder="MAT-001")
            with c2:
                mn = st.text_input("Material Name *", placeholder="Sugar")
            with c3:
                mu = st.selectbox("Unit", ["kg", "gm", "L", "ml", "pcs"])
            submitted = st.form_submit_button("Add Material")
            if submitted:
                mc, mn = clean_code(mc), clean_text(mn)
                if not mc or not mn:
                    st.error("Code and name are required.")
                elif mc in data["materials"]:
                    st.error(f"{mc} already exists.")
                elif mc in data["pending"]:
                    st.error(f"{mc} is already pending approval.")
                else:
                    data["materials"][mc] = {"name": mn, "unit": mu}
                    if save_data(data):
                        st.success(f"Added {mc}.")
                        st.rerun()

    with sub2:
        template = pd.DataFrame([{"Code": "MAT-001", "Name": "Sugar", "Unit": "kg"}])
        st.download_button(
            "⬇️ Download CSV Template",
            template.to_csv(index=False).encode("utf-8"),
            "materials_template.csv", "text/csv", key="material_template"
        )
        uploaded = st.file_uploader(
            "Upload CSV or Excel", type=["csv", "xlsx"], key="bulk_mat"
        )
        if uploaded:
            signature = (uploaded.name, uploaded.size, getattr(uploaded, "file_id", None))
            if st.session_state.get("_processed_material_upload") != signature:
                try:
                    records = prepare_material_import(read_material_file(uploaded))
                    added = skipped = updated_pending = 0
                    for record in records:
                        code = record["code"]
                        if code in data["materials"]:
                            skipped += 1
                        elif code in data["pending"]:
                            data["pending"][code] = {
                                "name": record["name"], "unit": record["unit"]
                            }
                            updated_pending += 1
                        else:
                            data["materials"][code] = {
                                "name": record["name"], "unit": record["unit"]
                            }
                            added += 1
                    if added or updated_pending:
                        if save_data(data):
                            st.session_state["_processed_material_upload"] = signature
                            st.success(
                                f"Imported {added}; updated pending {updated_pending}; "
                                f"skipped existing {skipped}."
                            )
                            st.rerun()
                    else:
                        st.session_state["_processed_material_upload"] = signature
                        st.info(f"No new records. Skipped {skipped} existing records.")
                except Exception as error:
                    st.error(f"Import failed: {error}")

    st.markdown("---")
    st.subheader(f"Registered Materials: {len(data['materials'])}")
    if data["materials"]:
        material_rows = [
            {"Code": code, "Name": item["name"], "Unit": item["unit"]}
            for code, item in sorted(data["materials"].items())
        ]
        st.dataframe(pd.DataFrame(material_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No registered materials yet.")


# ============================================================
# TAB 3: BUILD PRODUCT
# ============================================================

with tabs[2]:
    st.header("🛠️ Build Product")
    if not data["materials"]:
        st.warning("Register materials before building a product.")
    else:
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            bp_code = st.text_input("Product Code *", key="bpc")
        with c2:
            bp_name = st.text_input("Product Name *", key="bpn")
        with c3:
            bp_batch = st.number_input("Batch Size", min_value=1, value=1, key="bpb")

        st.markdown("---")
        st.subheader("BOM Materials")
        material_codes = sorted(data["materials"].keys())

        for row in list(st.session_state["draft"]):
            row_id = row["id"]
            c1, c2, c3 = st.columns([3, 2, 1])
            with c1:
                selected = st.selectbox(
                    f"Material {row_id}", ["—"] + material_codes,
                    index=(material_codes.index(row["code"]) + 1
                           if row["code"] in material_codes else 0),
                    format_func=lambda code: (
                        "— Select Material —" if code == "—"
                        else f"{code} — {data['materials'][code]['name']}"
                    ),
                    key=f"draft_material_{row_id}"
                )
                row["code"] = None if selected == "—" else selected

            if selected != "—" and is_mass_unit(material_unit(data, selected)):
                kg_col, gm_col, remove_col = st.columns([1, 1, 1])
                with kg_col:
                    row["kg"] = st.number_input(
                        f"Quantity (KG) – row {row_id}", min_value=0.0,
                        value=safe_float(row.get("kg", 0.0)),
                        step=0.001, format="%.6f", key=f"draft_kg_{row_id}"
                    )
                with gm_col:
                    row["gm"] = st.number_input(
                        f"Quantity (GM) – row {row_id}", min_value=0.0,
                        value=safe_float(row.get("gm", 0.0)),
                        step=0.01, format="%.6f", key=f"draft_gm_{row_id}"
                    )
                with remove_col:
                    st.write("")
                    if st.button("✖ Remove", key=f"remove_draft_{row_id}"):
                        st.session_state["draft"] = [
                            item for item in st.session_state["draft"] if item["id"] != row_id
                        ]
                        st.rerun()
            else:
                qty_col, remove_col = st.columns([2, 1])
                with qty_col:
                    unit = material_unit(data, selected) if selected != "—" else "kg"
                    row["quantity"] = st.number_input(
                        f"Quantity ({unit}) – row {row_id}", min_value=0.0,
                        value=safe_float(row.get("quantity", 0.0)),
                        step=0.001, format="%.6f", key=f"draft_quantity_{row_id}"
                    )
                with remove_col:
                    st.write("")
                    if st.button("✖ Remove", key=f"remove_draft_{row_id}"):
                        st.session_state["draft"] = [
                            item for item in st.session_state["draft"] if item["id"] != row_id
                        ]
                        st.rerun()

        c1, c2 = st.columns(2)
        with c1:
            if st.button("➕ Add Material Row"):
                new_id = st.session_state["draft_next_id"]
                st.session_state["draft_next_id"] += 1
                st.session_state["draft"].append({
                    "id": new_id, "code": None, "quantity": 0.0, "kg": 0.0, "gm": 0.0
                })
                st.rerun()
        with c2:
            if st.button("🧹 Clear Draft"):
                new_id = st.session_state["draft_next_id"]
                st.session_state["draft_next_id"] += 1
                st.session_state["draft"] = [{
                    "id": new_id, "code": None, "quantity": 0.0, "kg": 0.0, "gm": 0.0
                }]
                st.rerun()

        st.markdown("---")
        if st.button("💾 Save Product", key="save_manual_product"):
            product_code, product_name = clean_code(bp_code), clean_text(bp_name)
            if not product_code or not product_name:
                st.error("Product code and name are required.")
            else:
                combined = {}
                for row in st.session_state["draft"]:
                    code = row.get("code")
                    if not code:
                        continue
                    unit = material_unit(data, code)
                    if is_mass_unit(unit):
                        kg, gm = safe_float(row.get("kg")), safe_float(row.get("gm"))
                        if kg == 0 and gm == 0:
                            continue
                        key = (code, "mass")
                        combined.setdefault(key, {
                            "name": data["materials"][code]["name"], "kg": 0.0, "gm": 0.0
                        })
                        combined[key]["kg"] += kg
                        combined[key]["gm"] += gm
                    else:
                        qty = safe_float(row.get("quantity"))
                        if qty <= 0:
                            continue
                        key = (code, unit)
                        combined.setdefault(key, {
                            "name": data["materials"][code]["name"], "quantity": 0.0, "unit": unit
                        })
                        combined[key]["quantity"] += qty

                if not combined:
                    st.error("Select at least one material and enter a quantity greater than zero.")
                else:
                    materials = []
                    for (code, kind), item in combined.items():
                        if kind == "mass":
                            materials.append(create_bom_entry_parts(
                                code, item["name"], item["kg"], item["gm"]
                            ))
                        else:
                            materials.append(create_bom_entry(
                                code, item["name"], item["quantity"], item["unit"]
                            ))
                    data["products"][product_code] = {
                        "name": product_name, "batch_size": bp_batch,
                        "materials": materials, "source": "manual"
                    }
                    if save_data(data):
                        new_id = st.session_state["draft_next_id"]
                        st.session_state["draft_next_id"] += 1
                        st.session_state["draft"] = [{
                            "id": new_id, "code": None, "quantity": 0.0, "kg": 0.0, "gm": 0.0
                        }]
                        st.success(f"Successfully saved {product_code} with {len(materials)} unique materials.")
                        st.rerun()


# ============================================================
# TAB 4: PENDING MATERIALS
# ============================================================

with tabs[3]:
    st.header("⏳ Pending Material Approval")
    if not data["pending"]:
        st.success("No pending materials.")
    else:
        pending_rows = [
            {"Code": code, "Name": item["name"], "Unit": item["unit"]}
            for code, item in sorted(data["pending"].items())
        ]
        st.dataframe(pd.DataFrame(pending_rows), use_container_width=True, hide_index=True)
        st.markdown("---")
        approval_code = st.selectbox(
            "Select Material to Approve", ["—"] + sorted(data["pending"].keys()),
            key="approve_pending"
        )
        if approval_code != "—" and st.button(f"✅ Approve {approval_code}", key="approve_one"):
            if approval_code not in data["materials"]:
                data["materials"][approval_code] = data["pending"][approval_code]
            del data["pending"][approval_code]
            if save_data(data):
                st.success(f"{approval_code} approved.")
                st.rerun()

        st.markdown("---")
        if st.button("✅ Approve ALL Pending Materials", key="approve_all"):
            for code, item in list(data["pending"].items()):
                if code not in data["materials"]:
                    data["materials"][code] = item
                del data["pending"][code]
            if save_data(data):
                st.success("All pending materials approved.")
                st.rerun()


# ============================================================
# TAB 5: MANAGE PRODUCTS
# ============================================================

with tabs[4]:
    st.header("✏️ Manage Products")
    if not data["products"]:
        st.info("No products have been registered.")
    else:
        search_product = st.text_input(
            "Search Product Code or Name", key="product_search"
        ).strip().lower()

        for product_code, info in sorted(data["products"].items()):
            searchable = (product_code + " " + info["name"]).lower()
            if search_product and search_product not in searchable:
                continue
            with st.expander(
                f"📦 {product_code} — {info['name']} (Batch {info['batch_size']})"
            ):
                st.write(f"**Product Code:** {product_code}")
                st.write(f"**Product Name:** {info['name']}")
                st.write(f"**Batch Size:** {info['batch_size']}")
                st.write(f"**Source:** {info.get('source', 'legacy')}")

                rows = []
                for item in info["materials"]:
                    code = item.get("code", "")
                    unit = item.get("unit", material_unit(data, code))
                    if is_mass_unit(unit):
                        kg, gm = get_mass_parts(item, unit)
                        rows.append({
                            "Material Code": code, "Material Name": item.get("name", ""),
                            "Quantity (KG)": kg, "Quantity (GM)": gm
                        })
                    else:
                        rows.append({
                            "Material Code": code, "Material Name": item.get("name", ""),
                            f"Quantity ({unit})": safe_float(item.get("qty", 0))
                        })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                if st.button(f"🗑️ Delete {product_code}", key=f"delete_product_{product_code}"):
                    del data["products"][product_code]
                    if save_data(data):
                        st.success(f"{product_code} deleted.")
                        st.rerun()


# ============================================================
# TAB 6: EXPORT EXCEL
# ============================================================

with tabs[5]:
    st.header("📥 Export BOM to Excel")
    if not data["products"]:
        st.info("No products available for export.")
    else:
        products = data["products"]
        material_index = {}
        for product in products.values():
            for item in product["materials"]:
                code = clean_code(item.get("code"))
                if code:
                    material_index.setdefault(code, item.get("name", ""))

        rows = []
        batch_row = {"Material Code": "BATCH SIZE", "Material Name": ""}
        for product_code, info in products.items():
            batch_row[f"{product_code}\n{info['name']} – KG"] = ""
            batch_row[f"{product_code}\n{info['name']} – GM"] = ""
            batch_row[f"{product_code}\n{info['name']} – OTHER"] = info["batch_size"]
        rows.append(batch_row)

        for material_code in sorted(material_index):
            row = {
                "Material Code": material_code,
                "Material Name": material_index[material_code]
            }
            for product_code, info in products.items():
                kg_total = gm_total = other_total = 0.0
                other_unit = ""
                for item in info["materials"]:
                    if clean_code(item.get("code")) != material_code:
                        continue
                    unit = clean_text(item.get("unit", material_unit(data, material_code)))
                    if is_mass_unit(unit):
                        kg, gm = get_mass_parts(item, unit)
                        kg_total += kg
                        gm_total += gm
                    else:
                        other_total += safe_float(item.get("qty", 0))
                        other_unit = unit

                prefix = f"{product_code}\n{info['name']}"
                row[f"{prefix} – KG"] = kg_total if kg_total else ""
                row[f"{prefix} – GM"] = gm_total if gm_total else ""
                row[f"{prefix} – OTHER"] = (
                    f"{fmt_number(other_total)} {other_unit}" if other_total else ""
                )
            rows.append(row)

        export_df = pd.DataFrame(rows)
        st.subheader("BOM Matrix Preview")
        st.dataframe(export_df, use_container_width=True, hide_index=True)
        st.markdown("---")

        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            export_df.to_excel(writer, index=False, sheet_name="BOM")
            worksheet = writer.sheets["BOM"]
            worksheet.freeze_panes = "C2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for column_cells in worksheet.columns:
                max_length = 0
                column_letter = column_cells[0].column_letter
                for cell in column_cells:
                    if cell.value is not None:
                        max_length = max(max_length, len(str(cell.value)))
                worksheet.column_dimensions[column_letter].width = min(max(max_length + 3, 14), 35)

        output.seek(0)
        st.download_button(
            "⬇️ Download Excel BOM",
            data=output.getvalue(),
            file_name=f"BOM_{datetime.now():%Y%m%d_%H%M}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_bom_excel"
        )
        st.caption(
            f"Export includes {len(products)} products and "
            f"{len(material_index)} unique materials. KG and GM are exported separately."
        )


# ============================================================
# 10. FOOTER
# ============================================================

st.markdown("---")
st.caption(
    f"{APP_TITLE} | Products: {len(data['products'])} | "
    f"Materials: {len(data['materials'])} | Pending: {len(data['pending'])} | "
    f"Last opened: {datetime.now():%d-%m-%Y %H:%M}"
)