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
# ============================================================

st.set_page_config(
    page_title="BOM Digitizer",
    page_icon="📋",
    layout="wide"
)

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


def fmt_kg_gm(value):
    if value is None:
        return ""
    total = round(safe_float(value) * 1000, 4)
    kg = int(total // 1000)
    gm = round(total - kg * 1000, 4)
    if abs(gm - 1000) < 1e-6:
        kg += 1
        gm = 0

    def fmt(number):
        return f"{number:.3f}".rstrip("0").rstrip(".") or "0"

    parts = []
    if kg:
        parts.append(f"{kg} kg")
    if gm > 0 or not kg:
        parts.append(f"{fmt(gm)} gm")
    return " ".join(parts)


def format_quantity(value, unit):
    value = safe_float(value)
    unit = clean_text(unit) or "kg"
    if unit.lower() == "kg":
        return fmt_kg_gm(value)
    return f"{value:g} {unit}"


def convert_quantity(value, source_unit, target_unit):
    value = safe_float(value)
    source = clean_text(source_unit).lower()
    target = clean_text(target_unit).lower()
    if source == target:
        return value

    conversions = {
        ("kg", "gm"): 1000,
        ("gm", "kg"): 0.001,
        ("g", "kg"): 0.001,
        ("kg", "g"): 1000,
        ("g", "gm"): 1,
        ("gm", "g"): 1,
        ("l", "ml"): 1000,
        ("ml", "l"): 0.001,
    }
    factor = conversions.get((source, target))
    if factor is None:
        raise ValueError(f"Cannot convert {source_unit} to {target_unit}.")
    return value * factor


# ============================================================
# 2. QUANTITY PARSING
# ============================================================

def parse_qty(raw, mode, target_unit="kg"):
    if raw is None:
        return None
    text = clean_text(raw).lower().replace(" ", "")
    if not text:
        return None

    match = re.fullmatch(
        r"([+-]?\d[\d,.]*)(kg|gm|ml|pcs|pc|g|l)?",
        text
    )
    if not match:
        return None

    number_text, source_unit = match.groups()

    try:
        if "," in number_text and mode == "kg_gm" and not source_unit:
            parts = number_text.split(",")
            if len(parts) == 2:
                whole = float(parts[0] or 0)
                fractional = parts[1].ljust(3, "0")[:3]
                value = whole + float(fractional) / 1000
            else:
                value = float(number_text.replace(",", ""))
        else:
            value = float(number_text.replace(",", ""))

        if not math.isfinite(value) or value < 0:
            return None

        if source_unit:
            source_unit = {"g": "gm", "pc": "pcs"}.get(
                source_unit, source_unit
            )
            return convert_quantity(value, source_unit, target_unit)

        if mode == "kg_gm" and target_unit.lower() == "gm":
            return value * 1000
        return value

    except (ValueError, TypeError, OverflowError):
        return None


# ============================================================
# 3. LOCAL STORAGE
# ============================================================

def empty_database():
    return {"products": {}, "materials": {}, "pending": {}}


def _local_load():
    if not os.path.exists(DATA_FILE):
        return empty_database()

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            content = file.read().strip()
        if not content:
            return empty_database()
        result = json.loads(content)
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
            f"Could not read local database: {error}. "
            f"A backup attempt was made using {backup}."
        )
        return empty_database()


def _local_save(database):
    directory = os.path.dirname(os.path.abspath(DATA_FILE))
    os.makedirs(directory, exist_ok=True)
    temporary_file = DATA_FILE + ".tmp"

    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(
                database, file, ensure_ascii=False, indent=2,
                allow_nan=False
            )
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
# 4. GITHUB GIST STORAGE
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
        headers=_gist_headers(token),
        timeout=15
    )
    response.raise_for_status()
    files = response.json().get("files", {})
    if GIST_FILENAME not in files:
        raise ValueError(f"{GIST_FILENAME} was not found in configured Gist.")
    content = files[GIST_FILENAME].get("content", "")
    if not content.strip():
        return empty_database()
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Invalid Gist database structure.")
    return result


def _gist_save(token, gist_id, database):
    if not REQUESTS_OK:
        raise RuntimeError("Requests is not installed.")
    payload = {
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(
                    database, ensure_ascii=False, indent=2,
                    allow_nan=False
                )
            }
        }
    }
    response = requests.patch(
        f"{GITHUB_API}/gists/{gist_id}",
        headers=_gist_headers(token),
        json=payload,
        timeout=20
    )
    response.raise_for_status()
    if GIST_FILENAME not in response.json().get("files", {}):
        raise RuntimeError("GitHub did not confirm the expected database file.")
    return True


# ============================================================
# 5. DATABASE NORMALIZATION
# ============================================================

def _normalize(database):
    if not isinstance(database, dict):
        database = empty_database()

    for key in ("products", "materials", "pending"):
        if not isinstance(database.get(key), dict):
            database[key] = {}

    for section in ("materials", "pending"):
        for code, material in list(database[section].items()):
            if not isinstance(material, dict):
                database[section][code] = {
                    "name": str(material), "unit": "kg"
                }
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
        for material in product["materials"]:
            if not isinstance(material, dict):
                continue
            material.setdefault("code", "")
            material.setdefault("name", "")
            material.setdefault("qty_kg", 0)
            valid.append(material)
        product["materials"] = valid

    return database


# ============================================================
# 6. LOAD AND SAVE DATABASE
# ============================================================

def load_data(force=False):
    if st.session_state.get("_bom_loaded") and not force:
        return st.session_state["_bom_database"]

    token, gist_id = _gist_config() if REQUESTS_OK else (None, None)
    database = None

    if token and gist_id:
        try:
            database = _gist_load(token, gist_id)

            if not any(database.get(key) for key in (
                "products", "materials", "pending"
            )):
                local = _local_load()
                if any(local.get(key) for key in (
                    "products", "materials", "pending"
                )):
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
# 7. PRODUCT / MATERIAL QUANTITY HELPERS
# ============================================================

def material_unit(database, code):
    material = database["materials"].get(code, {})
    return clean_text(material.get("unit", "kg")) or "kg"


def get_material_quantity(entry, unit="kg"):
    if "qty" in entry:
        return safe_float(entry["qty"])
    old_value = safe_float(entry.get("qty_kg", 0))
    return old_value * 1000 if unit.lower() == "gm" else old_value


def create_bom_entry(code, name, quantity, unit):
    quantity = safe_float(quantity)
    unit = clean_text(unit) or "kg"
    entry = {
        "code": code, "name": name,
        "qty": quantity, "unit": unit
    }
    if unit.lower() == "kg":
        entry["qty_kg"] = quantity
    elif unit.lower() == "gm":
        entry["qty_kg"] = quantity / 1000
    return entry


# ============================================================
# 8. OCR SMART EXTRACTION
# ============================================================

MATERIAL_CODE_PATTERN = re.compile(
    r"^[A-Z]{1,5}[-/]?\d{2,6}[A-Z]?$", re.IGNORECASE
)
NUMBER_PATTERN = re.compile(
    r"^[+-]?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:kg|gm|g|l|ml|pcs))?$",
    re.IGNORECASE
)
STOP_WORDS = [
    "PRODUCTION MANAGER", "STORE KEEPER", "SIGNATURE",
    "ORIGINAL", "APPROVED BY", "CHECKED BY", "DATE:"
]


def extract_ocr_materials(raw):
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    extracted = []
    current_code = None
    current_name = []
    current_numbers = []

    def save_current():
        if not current_code:
            return
        name = " ".join(current_name).strip() or "Unnamed material"
        if not current_numbers:
            quantity = "0"
        elif len(current_numbers) == 1:
            quantity = current_numbers[0]
        else:
            quantity = ", ".join(current_numbers)
        extracted.append(f"{current_code} | {name} | {quantity}")

    for line in lines:
        if any(word in line.upper() for word in STOP_WORDS):
            continue

        first_part = line.split(maxsplit=1)[0].strip()
        if MATERIAL_CODE_PATTERN.fullmatch(first_part):
            save_current()
            current_code = clean_code(first_part)
            current_name = []
            current_numbers = []
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


# ============================================================
# 9. BULK IMPORT HELPERS
# ============================================================

def read_material_file(uploaded_file):
    filename = uploaded_file.name.lower()
    if filename.endswith(".csv"):
        dataframe = pd.read_csv(
            uploaded_file, dtype=str, keep_default_na=False
        )
    elif filename.endswith(".xlsx"):
        dataframe = pd.read_excel(uploaded_file, dtype=str)
    else:
        raise ValueError("Unsupported file format.")

    dataframe.columns = [
        clean_text(column).lower() for column in dataframe.columns
    ]
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
# 10. SESSION INITIALIZATION
# ============================================================

if "draft" not in st.session_state:
    st.session_state["draft"] = [
        {"id": 1, "code": None, "quantity": 0.0}
    ]

if "draft_next_id" not in st.session_state:
    st.session_state["draft_next_id"] = 2

if "final_box" not in st.session_state:
    st.session_state["final_box"] = ""

if "paste_mode" not in st.session_state:
    st.session_state["paste_mode"] = "kg_gm"

# FIX: perform widget state changes before the widget is instantiated.
if st.session_state.pop("_clear_final_box", False):
    st.session_state["final_box"] = ""

if st.session_state.pop("_load_final_box", False):
    st.session_state["final_box"] = st.session_state.pop(
        "_pending_final_box_value", ""
    )


# ============================================================
# 11. LOAD DATABASE
# ============================================================

force_reload = st.session_state.pop("_force_reload", False)
data = load_data(force=force_reload)


# ============================================================
# 12. HEADER AND STORAGE STATUS
# ============================================================

st.title("📋 BOM Digitizer")
st.caption(
    "Bill of Materials management, OCR extraction, "
    "material registration and Excel export."
)

if st.session_state.pop("_migrated", False):
    st.success("Local database successfully migrated to GitHub Gist.")

if "_storage_warning" in st.session_state:
    st.warning(st.session_state.pop("_storage_warning"))

if "_gist_err" in st.session_state:
    st.warning(
        "GitHub Gist synchronization issue: "
        + st.session_state.pop("_gist_err")
    )

with st.sidebar:
    st.subheader("⚙️ Settings")

    unit_mode = st.radio(
        "Commas in OCR mean:",
        ["kg + gm (2, 500 = 2.5 kg)", "thousands (2,500 = 2500)"],
        index=0 if st.session_state["paste_mode"] == "kg_gm" else 1
    )
    MODE = "kg_gm" if unit_mode.startswith("kg") else "decimal"
    st.session_state["paste_mode"] = MODE

    st.markdown("---")
    token, gist_id = _gist_config() if REQUESTS_OK else (None, None)

    if token and gist_id:
        if st.session_state.get("_gist_connected"):
            st.success("☁️ GitHub Gist connected")
        else:
            st.warning("GitHub configured – local fallback active")
    else:
        st.warning("Local storage only")
        st.caption(
            "Configure GitHub credentials in Streamlit Secrets "
            "to enable cloud synchronization."
        )

    st.caption(
        f"Storage mode: {st.session_state.get('_storage_mode', 'Local')}"
    )
    st.markdown("---")

    if st.button("🔄 Refresh database from storage"):
        st.session_state["_force_reload"] = True
        st.rerun()

    st.markdown("---")
    st.metric("Registered Materials", len(data["materials"]))
    st.metric("Registered Products", len(data["products"]))
    st.metric("Pending Materials", len(data["pending"]))


# ============================================================
# 13. APPLICATION TABS
# ============================================================

tabs = st.tabs([
    "📷 Paste", "🧱 Materials", "🛠️ Build",
    "⏳ Pending", "✏️ Manage", "📥 Export"
])


# ============================================================
# TAB 1: SMART PASTE
# ============================================================

with tabs[0]:
    st.header("Add BOM from Text")
    st.caption(
        "Use Google Lens to copy text from a BOM photograph. "
        "Paste the extracted text below."
    )

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        pcode = st.text_input(
            "Product Code *", key="pc1", placeholder="e.g. AFRABORN"
        )
    with c2:
        pname = st.text_input(
            "Product Name *", key="pn1", placeholder="e.g. AFRABORN SYRUP"
        )
    with c3:
        bsize = st.number_input(
            "Batch Size", min_value=1, value=1, key="bs1"
        )

    raw = st.text_area(
        "1. Paste raw Google Lens text here",
        height=220,
        placeholder="Paste the copied BOM text here...",
        key="paste_box"
    )

    if st.button("⚙️ Auto-Extract Data", key="extract_bom"):
        if not raw.strip():
            st.warning("Paste the BOM text first.")
        else:
            parsed_lines = extract_ocr_materials(raw)
            st.session_state["_pending_final_box_value"] = "\n".join(
                parsed_lines
            )
            st.session_state["_load_final_box"] = True

            if parsed_lines:
                st.success(
                    f"Extraction completed: {len(parsed_lines)} "
                    "material lines found."
                )
            else:
                st.warning(
                    "No recognizable material codes were found. "
                    "Check the OCR text and code formats."
                )
            # The pending value is applied before the text widget
            # is created on the next run.
            st.rerun()

    final_text = st.text_area(
        "2. Review and edit (Format: CODE | NAME | QTY)",
        height=260,
        key="final_box"
    )
    st.caption("Example: RA132 | SODIUM BENZOATE | 2, 500")

    if st.button("💾 Save BOM", key="save_pasted_bom"):
        product_code = clean_code(pcode)
        product_name = clean_text(pname)

        if not product_code or not product_name:
            st.error("Product code and name are required.")
        else:
            materials = []
            pending_materials = []
            errors = []

            for line_number, line in enumerate(
                final_text.splitlines(), start=1
            ):
                if not line.strip():
                    continue

                parts = [part.strip() for part in line.split("|")]
                if len(parts) < 3:
                    errors.append(f"Line {line_number}: Invalid format.")
                    continue

                mc = clean_code(parts[0])
                mn = clean_text(parts[1])
                qraw = "|".join(parts[2:]).strip()

                if not mc or not mn:
                    errors.append(
                        f"Line {line_number}: Code or name missing."
                    )
                    continue

                unit = material_unit(data, mc)
                quantity = parse_qty(qraw, MODE, target_unit=unit)

                if quantity is None:
                    errors.append(
                        f"Line {line_number}: Invalid quantity for {mc}."
                    )
                    continue

                materials.append(create_bom_entry(mc, mn, quantity, unit))

                if mc not in data["materials"] and mc not in data["pending"]:
                    data["pending"][mc] = {"name": mn, "unit": unit}
                    pending_materials.append(mc)

            if not materials:
                st.error(
                    "No valid BOM materials found. "
                    "Correct the entries before saving."
                )
            else:
                if product_code in data["products"]:
                    st.warning(
                        f"Product {product_code} already exists. "
                        "Saving will replace its existing BOM."
                    )

                data["products"][product_code] = {
                    "name": product_name,
                    "batch_size": bsize,
                    "materials": materials,
                    "source": "paste"
                }

                if save_data(data):
                    # FIX: defer clearing until the next script run.
                    st.session_state["_clear_final_box"] = True

                    st.success(
                        f"Saved {product_code} with "
                        f"{len(materials)} material entries."
                    )
                    if pending_materials:
                        st.info(
                            f"{len(pending_materials)} new materials "
                            "have been added to Pending."
                        )
                    if errors:
                        with st.expander(f"⚠️ {len(errors)} lines skipped"):
                            for error in errors:
                                st.write(error)
                    st.rerun()


# ============================================================
# TAB 2: MATERIALS
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
                mc = clean_code(mc)
                mn = clean_text(mn)

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
        template = pd.DataFrame([
            {"Code": "MAT-001", "Name": "Sugar", "Unit": "kg"}
        ])
        st.download_button(
            "⬇️ Download CSV Template",
            template.to_csv(index=False).encode("utf-8"),
            "materials_template.csv",
            "text/csv",
            key="material_template"
        )

        uploaded = st.file_uploader(
            "Upload CSV or Excel",
            type=["csv", "xlsx"],
            key="bulk_mat"
        )

        # Process each uploaded file only once per session.
        if uploaded:
            upload_signature = (
                uploaded.name, uploaded.size,
                getattr(uploaded, "file_id", None)
            )
            if st.session_state.get("_processed_material_upload") != upload_signature:
                try:
                    df = read_material_file(uploaded)
                    records = prepare_material_import(df)

                    added = 0
                    skipped = 0
                    updated_pending = 0

                    for record in records:
                        code = record["code"]
                        if code in data["materials"]:
                            skipped += 1
                            continue
                        if code in data["pending"]:
                            data["pending"][code] = {
                                "name": record["name"],
                                "unit": record["unit"]
                            }
                            updated_pending += 1
                            continue
                        data["materials"][code] = {
                            "name": record["name"],
                            "unit": record["unit"]
                        }
                        added += 1

                    if added or updated_pending:
                        if save_data(data):
                            st.session_state["_processed_material_upload"] = (
                                upload_signature
                            )
                            st.success(
                                f"Imported {added} materials. "
                                f"Updated {updated_pending} pending records. "
                                f"Skipped {skipped} existing records."
                            )
                            st.rerun()
                    else:
                        st.session_state["_processed_material_upload"] = (
                            upload_signature
                        )
                        st.info(
                            f"No new records imported. "
                            f"Skipped {skipped} existing records."
                        )
                except Exception as error:
                    st.error(f"Import failed: {error}")

    st.markdown("---")
    st.subheader(f"Registered Materials: {len(data['materials'])}")

    if data["materials"]:
        material_rows = [
            {"Code": code, "Name": material["name"], "Unit": material["unit"]}
            for code, material in sorted(data["materials"].items())
        ]
        st.dataframe(
            pd.DataFrame(material_rows),
            use_container_width=True,
            hide_index=True
        )
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
            bp_batch = st.number_input(
                "Batch Size", min_value=1, value=1, key="bpb"
            )

        st.markdown("---")
        st.subheader("BOM Materials")
        material_codes = sorted(data["materials"].keys())

        for row in list(st.session_state["draft"]):
            row_id = row["id"]
            c1, c2, c3 = st.columns([3, 2, 1])

            with c1:
                selected = st.selectbox(
                    f"Material {row_id}",
                    ["—"] + material_codes,
                    index=(
                        material_codes.index(row["code"]) + 1
                        if row["code"] in material_codes else 0
                    ),
                    format_func=lambda code: (
                        "— Select Material —" if code == "—"
                        else f"{code} — {data['materials'][code]['name']}"
                    ),
                    key=f"draft_material_{row_id}"
                )
                row["code"] = None if selected == "—" else selected

            with c2:
                selected_unit = (
                    material_unit(data, selected)
                    if selected != "—" else "kg"
                )
                row["quantity"] = st.number_input(
                    f"Quantity ({selected_unit})",
                    min_value=0.0,
                    value=safe_float(row["quantity"]),
                    step=0.001,
                    format="%.3f",
                    key=f"draft_quantity_{row_id}"
                )

            with c3:
                st.write("")
                if st.button("✖ Remove", key=f"remove_draft_{row_id}"):
                    st.session_state["draft"] = [
                        item for item in st.session_state["draft"]
                        if item["id"] != row_id
                    ]
                    st.rerun()

        c1, c2 = st.columns(2)
        with c1:
            if st.button("➕ Add Material Row"):
                new_id = st.session_state["draft_next_id"]
                st.session_state["draft_next_id"] += 1
                st.session_state["draft"].append({
                    "id": new_id, "code": None, "quantity": 0.0
                })
                st.rerun()

        with c2:
            if st.button("🧹 Clear Draft"):
                new_id = st.session_state["draft_next_id"]
                st.session_state["draft_next_id"] += 1
                st.session_state["draft"] = [{
                    "id": new_id, "code": None, "quantity": 0.0
                }]
                st.rerun()

        st.markdown("---")
        if st.button("💾 Save Product", key="save_manual_product"):
            product_code = clean_code(bp_code)
            product_name = clean_text(bp_name)

            if not product_code or not product_name:
                st.error("Product code and name are required.")
            else:
                combined = {}
                for row in st.session_state["draft"]:
                    code = row["code"]
                    if not code:
                        continue
                    quantity = safe_float(row["quantity"])
                    if quantity <= 0:
                        continue
                    unit = material_unit(data, code)
                    if code not in combined:
                        combined[code] = {
                            "name": data["materials"][code]["name"],
                            "quantity": 0.0,
                            "unit": unit
                        }
                    combined[code]["quantity"] += quantity

                if not combined:
                    st.error(
                        "Select at least one material with a quantity "
                        "greater than zero."
                    )
                else:
                    materials = [
                        create_bom_entry(
                            code, item["name"], item["quantity"], item["unit"]
                        )
                        for code, item in combined.items()
                    ]
                    data["products"][product_code] = {
                        "name": product_name,
                        "batch_size": bp_batch,
                        "materials": materials,
                        "source": "manual"
                    }

                    if save_data(data):
                        new_id = st.session_state["draft_next_id"]
                        st.session_state["draft_next_id"] += 1
                        st.session_state["draft"] = [{
                            "id": new_id, "code": None, "quantity": 0.0
                        }]
                        st.success(
                            f"Successfully saved {product_code} with "
                            f"{len(materials)} unique materials."
                        )
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
            {"Code": code, "Name": material["name"], "Unit": material["unit"]}
            for code, material in sorted(data["pending"].items())
        ]
        st.dataframe(
            pd.DataFrame(pending_rows),
            use_container_width=True,
            hide_index=True
        )
        st.markdown("---")

        approval_code = st.selectbox(
            "Select Material to Approve",
            ["—"] + sorted(data["pending"].keys()),
            key="approve_pending"
        )

        if approval_code != "—":
            if st.button(f"✅ Approve {approval_code}", key="approve_one"):
                if approval_code not in data["materials"]:
                    data["materials"][approval_code] = data["pending"][approval_code]
                del data["pending"][approval_code]
                if save_data(data):
                    st.success(f"{approval_code} approved.")
                    st.rerun()

        st.markdown("---")
        if st.button("✅ Approve ALL Pending Materials", key="approve_all"):
            for code, material in list(data["pending"].items()):
                if code not in data["materials"]:
                    data["materials"][code] = material
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
                f"📦 {product_code} — {info['name']} "
                f"(Batch {info['batch_size']})"
            ):
                st.write(f"**Product Code:** {product_code}")
                st.write(f"**Product Name:** {info['name']}")
                st.write(f"**Batch Size:** {info['batch_size']}")
                st.write(f"**Source:** {info.get('source', 'legacy')}")

                rows = []
                for item in info["materials"]:
                    code = item.get("code", "")
                    unit = item.get("unit", material_unit(data, code))
                    quantity = get_material_quantity(item, unit)
                    rows.append({
                        "Material Code": code,
                        "Material Name": item.get("name", ""),
                        "Quantity": format_quantity(quantity, unit),
                        "Unit": unit
                    })

                st.dataframe(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    hide_index=True
                )

                if st.button(
                    f"🗑️ Delete {product_code}",
                    key=f"delete_product_{product_code}"
                ):
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

        batch_row = {"Material Code": "BATCH SIZE", "Material Name": ""}
        for product_code, info in products.items():
            column_name = f"{product_code}\n{info['name']}"
            batch_row[column_name] = info["batch_size"]

        rows = [batch_row]

        for material_code in sorted(material_index):
            material_name = material_index[material_code]
            row = {
                "Material Code": material_code,
                "Material Name": material_name
            }

            for product_code, info in products.items():
                column_name = f"{product_code}\n{info['name']}"
                total_quantity = 0.0
                unit = material_unit(data, material_code)

                for item in info["materials"]:
                    if clean_code(item.get("code")) != material_code:
                        continue
                    item_unit = item.get("unit", unit)
                    quantity = get_material_quantity(item, item_unit)
                    try:
                        quantity = convert_quantity(
                            quantity, item_unit, unit
                        )
                    except ValueError:
                        quantity = get_material_quantity(item, unit)
                    total_quantity += quantity

                row[column_name] = (
                    format_quantity(total_quantity, unit)
                    if total_quantity else ""
                )
            rows.append(row)

        export_df = pd.DataFrame(rows)
        st.subheader("BOM Matrix Preview")
        st.dataframe(
            export_df,
            use_container_width=True,
            hide_index=True
        )
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
                worksheet.column_dimensions[column_letter].width = min(
                    max(max_length + 3, 14), 35
                )

        output.seek(0)
        st.download_button(
            "⬇️ Download Excel BOM",
            data=output.getvalue(),
            file_name=f"BOM_{datetime.now():%Y%m%d_%H%M}.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            key="download_bom_excel"
        )
        st.caption(
            f"Export includes {len(products)} products and "
            f"{len(material_index)} unique materials."
        )


# ============================================================
# 14. FOOTER
# ============================================================

st.markdown("---")
st.caption(
    f"{APP_TITLE} | "
    f"Products: {len(data['products'])} | "
    f"Materials: {len(data['materials'])} | "
    f"Pending: {len(data['pending'])} | "
    f"Last opened: {datetime.now():%d-%m-%Y %H:%M}"
)
