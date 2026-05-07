"""AgriSmart AI - Intelligent Farmer Welfare & Smart Agriculture Administration Portal.

This Flask application is intentionally written in a beginner-friendly style.
The code keeps most features in one file so it is easier to read end-to-end.
"""

from __future__ import annotations

import io
import os
import random
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_session import Session
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    import cv2
except Exception:  # pragma: no cover - optional dependency fallback
    cv2 = None

try:
    import pytesseract
except Exception:  # pragma: no cover - optional dependency fallback
    pytesseract = None

try:
    import qrcode
except Exception:  # pragma: no cover - optional dependency fallback
    qrcode = None

try:
    import requests
except Exception:  # pragma: no cover - optional dependency fallback
    requests = None

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
except Exception:  # pragma: no cover - optional dependency fallback
    A4 = None
    canvas = None


# ---------------------------------------------------------------------------
# Basic application configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_DIR = BASE_DIR / "uploads"
QR_DIR = BASE_DIR / "static" / "generated_qr"
DATABASE_PATH = INSTANCE_DIR / "agrismart.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "pdf"}
MAX_UPLOAD_SIZE = 5 * 1024 * 1024

DISTRICT_COORDINATES = {
    "chennai": (13.0827, 80.2707),
    "coimbatore": (11.0168, 76.9558),
    "madurai": (9.9252, 78.1198),
    "salem": (11.6643, 78.1460),
    "thanjavur": (10.7867, 79.1378),
    "tiruchirappalli": (10.7905, 78.7047),
    "erode": (11.3410, 77.7172),
    "vellore": (12.9165, 79.1325),
    "cuddalore": (11.7447, 79.7680),
    "tiruvarur": (10.7667, 79.6333),
}


app = Flask(__name__)
app.config["SECRET_KEY"] = "agrismart-ai-secret-key"
app.config["DATABASE"] = str(DATABASE_PATH)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = str(INSTANCE_DIR / "flask_session")
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)

session_manager = Session()


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def ensure_directories() -> None:
    """Create folders used by the project if they do not exist."""
    for path in [
        INSTANCE_DIR,
        UPLOAD_DIR,
        UPLOAD_DIR / "aadhaar",
        UPLOAD_DIR / "patta",
        UPLOAD_DIR / "land",
        QR_DIR,
        INSTANCE_DIR / "flask_session",
    ]:
        path.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    """Return one SQLite connection per request."""
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    """Close the database connection after the request finishes."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """Create tables and insert the default admin account."""
    db = sqlite3.connect(app.config["DATABASE"])
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as schema_file:
            db.executescript(schema_file.read())

        admin_row = db.execute("SELECT id FROM admin WHERE username = ?", ("admin",)).fetchone()
        if not admin_row:
            db.execute(
                "INSERT INTO admin (username, password_hash, full_name) VALUES (?, ?, ?)",
                ("admin", generate_password_hash("admin123"), "Portal Administrator"),
            )
            db.commit()
    finally:
        db.close()


ensure_directories()
session_manager.init_app(app)
init_db()

if pytesseract and os.environ.get("TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]


# ---------------------------------------------------------------------------
# Session and security helpers
# ---------------------------------------------------------------------------

def admin_required(view):
    """Protect admin-only routes."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if session.get("admin_logged_in") is not True:
            flash("Please login as admin to continue.", "warning")
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped_view


def farmer_required(view):
    """Protect farmer-only routes."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("farmer_session_id"):
            flash("Please access the farmer dashboard through approved status tracking.", "warning")
            return redirect(url_for("track_status"))
        return view(*args, **kwargs)

    return wrapped_view


@app.before_request
def handle_session_timeout() -> None:
    """Expire sessions after inactivity and update last activity time."""
    session.permanent = True
    now = datetime.utcnow()
    last_activity = session.get("last_activity")

    if last_activity:
        previous = datetime.fromisoformat(last_activity)
        if now - previous > app.permanent_session_lifetime:
            session.clear()
            flash("Your session expired due to inactivity. Please login again.", "info")

    session["last_activity"] = now.isoformat()


def sanitize_text(value: str, max_length: int | None = None) -> str:
    """Trim and sanitize free-form text values."""
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    cleaned = re.sub(r"[<>]", "", cleaned)
    return cleaned[:max_length] if max_length else cleaned


def allowed_file(filename: str) -> bool:
    """Check whether an uploaded filename is safe and supported."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_file(file_storage, folder_name: str) -> str:
    """Save an uploaded file and return the relative path stored in the database."""
    if not file_storage or not file_storage.filename:
        raise ValueError("Missing file upload.")

    if not allowed_file(file_storage.filename):
        raise ValueError("Unsupported file type. Only JPG, PNG, and PDF are allowed.")

    extension = file_storage.filename.rsplit(".", 1)[1].lower()
    safe_name = secure_filename(file_storage.filename)
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    target_folder = UPLOAD_DIR / folder_name
    target_path = target_folder / unique_name
    file_storage.save(target_path)
    return f"{folder_name}/{unique_name}"


def normalize_name(name: str) -> str:
    """Convert a name to a simplified form for comparison."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


def generate_farmer_id() -> str:
    """Generate an easy-to-read farmer ID."""
    return f"AGR{datetime.now().strftime('%Y%m')}{random.randint(1000, 9999)}"


def generate_qr_code(farmer_id: str) -> str:
    """Generate a QR code for the farmer ID if the qrcode library is available."""
    qr_relative = f"generated_qr/{farmer_id}.png"
    qr_path = QR_DIR / f"{farmer_id}.png"

    if qrcode:
        image = qrcode.make(f"AgriSmart Farmer ID: {farmer_id}")
        image.save(qr_path)
    else:
        # Fallback: create a simple placeholder image when qrcode is unavailable.
        image = Image.new("RGB", (240, 240), color=(255, 255, 255))
        image.save(qr_path)

    return qr_relative


def extract_text_from_document(relative_path: str) -> str:
    """Run OCR on JPG/PNG documents and gracefully handle unavailable OCR tools."""
    absolute_path = UPLOAD_DIR / relative_path
    extension = absolute_path.suffix.lower()

    if extension == ".pdf":
        return "PDF uploaded. OCR on PDF requires manual review or a PDF-to-image converter."

    if pytesseract is None:
        return "pytesseract is not installed. Manual review required."

    try:
        if cv2:
            image = cv2.imread(str(absolute_path))
            if image is not None:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                text = pytesseract.image_to_string(gray)
                return text.strip()

        pil_image = Image.open(absolute_path)
        text = pytesseract.image_to_string(pil_image)
        return text.strip()
    except Exception as exc:  # pragma: no cover - OCR depends on system binary
        return f"Manual review required: {exc}"


def parse_aadhaar_details(text: str) -> dict[str, str]:
    """Extract a possible name and Aadhaar number from OCR text."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    aadhaar_match = re.search(r"\b(\d{4}\s?\d{4}\s?\d{4})\b", text)

    possible_name = ""
    for line in lines[:8]:
        if re.fullmatch(r"[A-Za-z .]{3,50}", line) and "government" not in line.lower():
            possible_name = line
            break

    return {
        "name": possible_name,
        "aadhaar_number": aadhaar_match.group(1).replace(" ", "") if aadhaar_match else "",
    }


def parse_patta_details(text: str) -> dict[str, str]:
    """Extract possible patta owner and land details from OCR text."""
    owner_name = ""
    land_detail = ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for line in lines[:12]:
        lower = line.lower()
        if not owner_name and re.fullmatch(r"[A-Za-z .]{3,50}", line):
            owner_name = line
        if "acre" in lower or "survey" in lower or "land" in lower:
            land_detail = line

    if not land_detail and lines:
        land_detail = lines[min(3, len(lines) - 1)]

    return {"owner_name": owner_name, "land_details": land_detail}


def verify_documents(
    farmer_name: str,
    aadhaar_number: str,
    patta_number: str,
    aadhaar_path: str,
    patta_path: str,
) -> dict[str, str]:
    """Run basic OCR and compare extracted names with the registration form."""
    aadhaar_text = extract_text_from_document(aadhaar_path)
    patta_text = extract_text_from_document(patta_path)

    aadhaar_data = parse_aadhaar_details(aadhaar_text)
    patta_data = parse_patta_details(patta_text)

    form_name = normalize_name(farmer_name)
    aadhaar_name = normalize_name(aadhaar_data["name"])
    patta_name = normalize_name(patta_data["owner_name"])

    verification_status = "Manual Review Required"
    patta_verification = "Pending Review"
    land_ownership_status = "Pending Review"

    aadhaar_matches = bool(aadhaar_name and form_name and (aadhaar_name in form_name or form_name in aadhaar_name))
    patta_matches = bool(patta_name and form_name and (patta_name in form_name or form_name in patta_name))
    aadhaar_number_matches = aadhaar_data["aadhaar_number"] == aadhaar_number if aadhaar_data["aadhaar_number"] else False

    if aadhaar_matches and aadhaar_number_matches:
        verification_status = "Verified"
    elif aadhaar_data["name"] or aadhaar_data["aadhaar_number"]:
        verification_status = "Aadhaar Mismatch"

    if patta_matches:
        patta_verification = "Patta Verified"
        land_ownership_status = "Fully Verified"
    elif patta_data["owner_name"] or patta_number:
        patta_verification = "Mismatch"
        land_ownership_status = "Mismatch"

    if verification_status == "Verified" and patta_verification == "Patta Verified":
        verification_status = "Verified"
        land_ownership_status = "Fully Verified"
    elif "Manual review" in (aadhaar_text + patta_text).lower():
        verification_status = "Manual Review Required"
        patta_verification = "Pending Review"
        land_ownership_status = "Pending Review"

    return {
        "verification_status": verification_status,
        "patta_verification": patta_verification,
        "land_ownership_status": land_ownership_status,
        "ocr_aadhaar_name": aadhaar_data["name"],
        "ocr_aadhaar_number": aadhaar_data["aadhaar_number"],
        "ocr_patta_name": patta_data["owner_name"],
        "ocr_land_details": patta_data["land_details"],
        "ocr_raw_text": f"Aadhaar OCR:\n{aadhaar_text}\n\nPatta OCR:\n{patta_text}",
    }


def recommend_subsidies(crop_type: str, land_area: float, farmer_category: str, district: str) -> list[str]:
    """Suggest subsidy schemes using simple rule-based logic."""
    crop = (crop_type or "").lower()
    category = (farmer_category or "").lower()
    district_name = (district or "").lower()
    suggestions = ["PM-KISAN", "Soil Health Card Scheme"]

    if land_area <= 2:
        suggestions.append("Seed Subsidy")
        suggestions.append("Fertilizer Subsidy")
    if "small" in category or "marginal" in category:
        suggestions.append("Smart Irrigation Subsidy")
    if crop in {"banana", "coconut", "tomato", "onion"}:
        suggestions.append("Drip Irrigation Subsidy")
    if crop in {"cotton", "maize", "millet"}:
        suggestions.append("Organic Farming Subsidy")
    if crop in {"sugarcane", "paddy"}:
        suggestions.append("Solar Pump Subsidy")
    if district_name in {"thanjavur", "tiruvarur", "cuddalore"}:
        suggestions.append("Greenhouse Subsidy")
    if land_area > 5:
        suggestions.append("Tractor Subsidy")

    return list(dict.fromkeys(suggestions))


def assess_insurance(
    crop_type: str,
    season: str,
    district: str,
    land_area: float,
    irrigation_type: str,
    estimated_value: float,
) -> dict[str, Any]:
    """Estimate crop insurance recommendation, premium, subsidy, and risk level."""
    crop = (crop_type or "").lower()
    district_name = (district or "").lower()
    irrigation = (irrigation_type or "").lower()

    scheme = "State Crop Protection Plan"
    subsidy = 35
    risk = "Low"
    premium_rate = 0.018

    if crop == "paddy":
        scheme = "PMFBY"
        subsidy = 55
        premium_rate = 0.02
    elif crop == "sugarcane":
        scheme = "Cash Crop Shield"
        subsidy = 60
        premium_rate = 0.024
    elif crop in {"cotton", "banana"}:
        scheme = "High Value Crop Protection"
        subsidy = 48
        premium_rate = 0.026

    drought_districts = {"madurai", "salem", "vellore"}
    flood_districts = {"cuddalore", "nagapattinam", "tiruvarur"}
    if district_name in drought_districts or irrigation == "rainfed":
        risk = "High"
        premium_rate += 0.01
    elif district_name in flood_districts:
        risk = "Medium"
        premium_rate += 0.005

    if land_area > 5:
        subsidy += 5

    estimated_crop_value = max(estimated_value, 10000)
    premium = round(estimated_crop_value * premium_rate, 2)
    return {
        "scheme_name": scheme,
        "premium": premium,
        "subsidy": min(subsidy, 70),
        "risk_level": risk,
        "season_note": f"{season} season enrolled under {scheme}.",
    }


def assess_loan(
    crop_type: str,
    district: str,
    land_area: float,
    annual_income: float,
    requested_amount: float,
    loan_type: str,
) -> dict[str, Any]:
    """Predict a simple loan decision, scheme, interest, tenure, and EMI."""
    crop = (crop_type or "").lower()
    district_name = (district or "").lower()
    loan_name = (loan_type or "").lower()

    base_limit = annual_income * 0.8 + (land_area * 60000)
    scheme_name = "PM Kisan Loan"
    interest_rate = 7.5
    repayment_period = 24
    approval_probability = 55
    eligibility_status = "Partially Eligible"
    risk_level = "Medium"

    if land_area >= 5:
        base_limit += 150000
        approval_probability += 10
    if crop in {"paddy", "sugarcane"}:
        base_limit += 100000
        interest_rate -= 1
        approval_probability += 12
        scheme_name = "Kisan Credit Card"
    if "tractor" in loan_name or "equipment" in loan_name:
        scheme_name = "Tractor Finance Scheme"
        repayment_period = 48
        interest_rate = 8.2
    if "solar" in loan_name or "irrigation" in loan_name:
        scheme_name = "Solar Irrigation Loan"
        repayment_period = 36
        interest_rate = 6.9
    if "dairy" in loan_name or "poultry" in loan_name:
        scheme_name = "Dairy Entrepreneurship Scheme"
        repayment_period = 42
        interest_rate = 7.8
    if "greenhouse" in loan_name:
        scheme_name = "NABARD Agriculture Loan"
        repayment_period = 60
        approval_probability += 5

    if district_name in {"madurai", "salem"} and crop in {"millet", "groundnut"}:
        risk_level = "High"
        approval_probability -= 15
    elif district_name in {"thanjavur", "tiruvarur"}:
        approval_probability += 8
        risk_level = "Low"

    if requested_amount <= base_limit * 0.75:
        eligibility_status = "Eligible"
        approval_probability += 10
    elif requested_amount > base_limit * 1.25:
        eligibility_status = "Rejected"
        approval_probability -= 25
        risk_level = "High"
    elif requested_amount > base_limit:
        eligibility_status = "High Risk"
        approval_probability -= 10

    approval_probability = max(10, min(95, approval_probability))
    emi_amount = calculate_emi(requested_amount, interest_rate, repayment_period)

    return {
        "scheme_name": scheme_name,
        "interest_rate": round(interest_rate, 2),
        "repayment_period": repayment_period,
        "eligibility_status": eligibility_status,
        "approval_probability": approval_probability,
        "risk_level": risk_level,
        "emi_amount": emi_amount,
    }


def calculate_emi(principal: float, annual_rate: float, months: int) -> float:
    """Calculate monthly EMI for the loan module."""
    if principal <= 0 or months <= 0:
        return 0.0
    monthly_rate = (annual_rate / 12) / 100
    if monthly_rate == 0:
        return round(principal / months, 2)
    emi = principal * monthly_rate * ((1 + monthly_rate) ** months) / (((1 + monthly_rate) ** months) - 1)
    return round(emi, 2)


def recommend_crop(district: str, season: str, land_area: float) -> str:
    """Return a simple crop recommendation for the bonus feature."""
    district_name = (district or "").lower()
    season_name = (season or "").lower()

    if district_name in {"thanjavur", "tiruvarur", "cuddalore"} and "kharif" in season_name:
        return "Paddy is recommended due to strong monsoon suitability."
    if district_name in {"salem", "madurai"} and land_area < 3:
        return "Millet is recommended for lower water dependency and stable risk."
    if "summer" in season_name:
        return "Groundnut is recommended for summer cycle planning."
    return "Maize is recommended as a balanced crop option for the selected profile."


def get_weather_snapshot(district: str) -> dict[str, str]:
    """Fetch current weather from Open-Meteo when requests is available."""
    district_name = (district or "").strip().lower()
    latitude, longitude = DISTRICT_COORDINATES.get(district_name, (11.1271, 78.6569))

    if not requests:
        return {"summary": "Weather service unavailable in this environment.", "temperature": "--", "wind": "--"}

    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}&longitude={longitude}&current=temperature_2m,wind_speed_10m"
        )
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        payload = response.json().get("current", {})
        return {
            "summary": f"Live weather for {district.title()} fetched from Open-Meteo.",
            "temperature": f"{payload.get('temperature_2m', '--')} °C",
            "wind": f"{payload.get('wind_speed_10m', '--')} km/h",
        }
    except Exception:
        return {
            "summary": "Live weather could not be fetched. Please check internet connectivity.",
            "temperature": "--",
            "wind": "--",
        }


def send_sms_notification(phone: str, message: str) -> None:
    """Write SMS alerts to a local log file as a beginner-friendly integration stub."""
    log_file = INSTANCE_DIR / "sms_log.txt"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] SMS to {phone}: {message}\n")


def get_farmer_from_session() -> sqlite3.Row | None:
    """Return the currently logged in farmer from the database."""
    if not session.get("farmer_session_id"):
        return None
    db = get_db()
    return db.execute(
        "SELECT * FROM farmers WHERE farmer_id = ? AND application_status = 'Approved'",
        (session["farmer_session_id"],),
    ).fetchone()


def build_loan_receipt_pdf(loan: sqlite3.Row, farmer: sqlite3.Row) -> bytes:
    """Generate a professional PDF receipt for approved loan applications."""
    if not canvas or not A4:
        raise RuntimeError("reportlab is not installed.")

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    pdf.setTitle(f"Loan Receipt - {loan['loan_type']}")
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(50, height - 60, "AgriSmart AI Loan Receipt")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, height - 80, "AI Powered Digital Governance System for Smart Agriculture Administration")

    y = height - 120
    rows = [
        ("Farmer Name", farmer["name"]),
        ("Farmer ID", farmer["farmer_id"]),
        ("Loan Type", loan["loan_type"]),
        ("Scheme Name", loan["scheme_name"]),
        ("Requested Amount", f"INR {loan['requested_amount']:.2f}"),
        ("Interest Rate", f"{loan['interest_rate']}%"),
        ("Repayment Period", f"{loan['repayment_period']} months"),
        ("EMI", f"INR {loan['emi_amount']:.2f}"),
        ("Eligibility", loan["eligibility_status"]),
        ("Approval Probability", f"{loan['approval_probability']}%"),
        ("Current Status", loan["status"]),
        ("Generated On", datetime.now().strftime("%d-%m-%Y %H:%M")),
    ]

    for label, value in rows:
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(50, y, f"{label}:")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(200, y, str(value))
        y -= 24

    pdf.setFont("Helvetica-Oblique", 10)
    pdf.drawString(50, y - 10, "This is a digitally generated receipt for portal reference.")
    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.read()


def chatbot_response(message: str) -> str:
    """Return rule-based answers for common portal questions."""
    text = (message or "").lower()
    rules = [
        (["apply subsidy", "subsidy"], "Open Farmer Dashboard, choose Apply Subsidy, fill the scheme details, and submit the request."),
        (["check status", "track"], "Use the Track Status page with your phone number or Aadhaar number to view approval progress."),
        (["apply insurance", "insurance"], "Approved farmers can apply from the dashboard under Smart Crop Insurance."),
        (["apply loan", "loan"], "Open the Agriculture Loan module, enter land area, income, loan type, and submit for AI eligibility assessment."),
        (["required documents", "documents"], "Keep Aadhaar image, Patta document, and land photo ready in JPG, PNG, or PDF format."),
        (["pm-kisan", "pm kisan"], "PM-KISAN is a direct income support scheme. The portal may recommend it in the subsidy module."),
        (["eligibility", "loan eligibility"], "Loan eligibility depends on land area, crop type, income, requested amount, and district risk profile."),
        (["hello", "hi"], "Welcome to AgriSmart AI. I can help you with registration, subsidy, insurance, loans, complaints, and tracking."),
    ]

    for keywords, answer in rules:
        if any(keyword in text for keyword in keywords):
            return answer

    return "Please ask about registration, subsidy, insurance, loan, status tracking, required documents, or PM-KISAN."


@app.template_filter("currency")
def currency_filter(value: Any) -> str:
    """Format numbers as Indian Rupee values."""
    try:
        return f"INR {float(value):,.2f}"
    except (TypeError, ValueError):
        return "INR 0.00"


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    """Render the landing page with portal statistics."""
    db = get_db()
    stats = {
        "farmers": db.execute("SELECT COUNT(*) AS count FROM farmers").fetchone()["count"],
        "approved": db.execute(
            "SELECT COUNT(*) AS count FROM farmers WHERE application_status = 'Approved'"
        ).fetchone()["count"],
        "subsidies": db.execute("SELECT COUNT(*) AS count FROM subsidies").fetchone()["count"],
        "loans": db.execute("SELECT COUNT(*) AS count FROM loans").fetchone()["count"],
    }
    return render_template("home.html", stats=stats)


@app.route("/register", methods=["GET", "POST"])
def register_farmer():
    """Register a new farmer and run AI-style document checks."""
    if request.method == "POST":
        try:
            form_data = {
                "name": sanitize_text(request.form.get("name", ""), 100),
                "age": int(request.form.get("age", "0")),
                "gender": sanitize_text(request.form.get("gender", ""), 20),
                "address": sanitize_text(request.form.get("address", ""), 250),
                "district": sanitize_text(request.form.get("district", ""), 50),
                "state": sanitize_text(request.form.get("state", ""), 50),
                "phone": re.sub(r"\D", "", request.form.get("phone", "")),
                "aadhaar": re.sub(r"\D", "", request.form.get("aadhaar", "")),
                "patta_number": sanitize_text(request.form.get("patta_number", ""), 40),
                "farmer_category": sanitize_text(request.form.get("farmer_category", ""), 50),
                "land_area": float(request.form.get("land_area", "0")),
                "crop_type": sanitize_text(request.form.get("crop_type", ""), 50),
                "season": sanitize_text(request.form.get("season", ""), 30),
                "annual_income": float(request.form.get("annual_income", "0")),
                "bank_account": sanitize_text(request.form.get("bank_account", ""), 30),
                "ifsc_code": sanitize_text(request.form.get("ifsc_code", ""), 20),
            }
        except ValueError:
            flash("Please enter valid numeric values for age, land area, and annual income.", "danger")
            return redirect(url_for("register_farmer"))

        if not re.fullmatch(r"\d{10}", form_data["phone"]):
            flash("Phone number must contain exactly 10 digits.", "danger")
            return redirect(url_for("register_farmer"))
        if not re.fullmatch(r"\d{12}", form_data["aadhaar"]):
            flash("Aadhaar number must contain exactly 12 digits.", "danger")
            return redirect(url_for("register_farmer"))
        if not form_data["patta_number"]:
            flash("Patta number is required.", "danger")
            return redirect(url_for("register_farmer"))

        try:
            aadhaar_path = save_uploaded_file(request.files.get("aadhaar_image"), "aadhaar")
            patta_path = save_uploaded_file(request.files.get("patta_document"), "patta")
            land_path = save_uploaded_file(request.files.get("land_image"), "land")
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("register_farmer"))

        farmer_id = generate_farmer_id()
        qr_code_path = generate_qr_code(farmer_id)
        ocr_result = verify_documents(
            form_data["name"],
            form_data["aadhaar"],
            form_data["patta_number"],
            aadhaar_path,
            patta_path,
        )

        db = get_db()
        db.execute(
            """
            INSERT INTO farmers (
                farmer_id, name, age, gender, address, district, state, phone, aadhaar,
                patta_number, farmer_category, land_area, crop_type, season, annual_income,
                bank_account, ifsc_code, aadhaar_image, patta_document, land_image,
                verification_status, patta_verification, application_status, land_ownership_status,
                ocr_aadhaar_name, ocr_aadhaar_number, ocr_patta_name, ocr_land_details,
                ocr_raw_text, qr_code_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                farmer_id,
                form_data["name"],
                form_data["age"],
                form_data["gender"],
                form_data["address"],
                form_data["district"],
                form_data["state"],
                form_data["phone"],
                form_data["aadhaar"],
                form_data["patta_number"],
                form_data["farmer_category"],
                form_data["land_area"],
                form_data["crop_type"],
                form_data["season"],
                form_data["annual_income"],
                form_data["bank_account"],
                form_data["ifsc_code"],
                aadhaar_path,
                patta_path,
                land_path,
                ocr_result["verification_status"],
                ocr_result["patta_verification"],
                "Pending",
                ocr_result["land_ownership_status"],
                ocr_result["ocr_aadhaar_name"],
                ocr_result["ocr_aadhaar_number"],
                ocr_result["ocr_patta_name"],
                ocr_result["ocr_land_details"],
                ocr_result["ocr_raw_text"],
                qr_code_path,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        db.commit()

        send_sms_notification(form_data["phone"], f"AgriSmart AI registration received. Farmer ID: {farmer_id}.")
        flash(
            f"Registration submitted successfully. Your Farmer ID is {farmer_id}. Verification status: {ocr_result['verification_status']}.",
            "success",
        )

        if ocr_result["verification_status"] == "Aadhaar Mismatch" or ocr_result["patta_verification"] == "Mismatch":
            flash("Document mismatch detected. Your application may require manual review.", "warning")

        return redirect(url_for("track_status"))

    return render_template("register.html")


@app.route("/track", methods=["GET", "POST"])
def track_status():
    """Track farmer application status using phone number or Aadhaar number."""
    farmer = None
    service_statuses = {}
    if request.method == "POST":
        identifier = re.sub(r"\D", "", request.form.get("identifier", ""))
        db = get_db()
        farmer = db.execute(
            "SELECT * FROM farmers WHERE phone = ? OR aadhaar = ? ORDER BY id DESC LIMIT 1",
            (identifier, identifier),
        ).fetchone()

        if not farmer:
            flash("No farmer application found for the given phone or Aadhaar number.", "danger")
        else:
            service_statuses = {
                "subsidy": db.execute(
                    "SELECT status FROM subsidies WHERE farmer_id = ? ORDER BY id DESC LIMIT 1",
                    (farmer["farmer_id"],),
                ).fetchone(),
                "insurance": db.execute(
                    "SELECT status FROM insurance WHERE farmer_id = ? ORDER BY id DESC LIMIT 1",
                    (farmer["farmer_id"],),
                ).fetchone(),
                "loan": db.execute(
                    "SELECT status FROM loans WHERE farmer_id = ? ORDER BY id DESC LIMIT 1",
                    (farmer["farmer_id"],),
                ).fetchone(),
            }

    return render_template("track_status.html", farmer=farmer, service_statuses=service_statuses)


@app.route("/farmer/access", methods=["POST"])
def farmer_access():
    """Create a farmer dashboard session when the application is approved."""
    identifier = re.sub(r"\D", "", request.form.get("identifier", ""))
    db = get_db()
    farmer = db.execute(
        """
        SELECT * FROM farmers
        WHERE (phone = ? OR aadhaar = ?) AND application_status = 'Approved'
        ORDER BY id DESC LIMIT 1
        """,
        (identifier, identifier),
    ).fetchone()

    if not farmer:
        flash("Approved farmer account not found for the given identifier.", "danger")
        return redirect(url_for("track_status"))

    session["farmer_session_id"] = farmer["farmer_id"]
    flash(f"Welcome {farmer['name']}. Farmer dashboard access granted.", "success")
    return redirect(url_for("farmer_dashboard"))


@app.route("/privacy-policy")
def privacy_policy():
    """Render the privacy policy page."""
    return render_template("privacy_policy.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    """Render the contact page and handle contact form submissions."""
    if request.method == "POST":
        name = sanitize_text(request.form.get("name", ""), 80)
        email = sanitize_text(request.form.get("email", ""), 120)
        message = sanitize_text(request.form.get("message", ""), 500)
        with open(INSTANCE_DIR / "contact_messages.txt", "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat()} | {name} | {email} | {message}\n")
        flash("Your contact request has been submitted to the agriculture support desk.", "success")
        return redirect(url_for("contact"))

    return render_template("contact.html")


@app.route("/chatbot")
def chatbot_page():
    """Render the chatbot page."""
    return render_template("chatbot.html")


@app.route("/api/chatbot", methods=["POST"])
def chatbot_api():
    """Return a rule-based chatbot answer."""
    message = request.get_json(silent=True, force=True) or {}
    reply = chatbot_response(message.get("message", ""))
    return jsonify({"reply": reply})


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Authenticate the admin using the default seeded account."""
    if request.method == "POST":
        username = sanitize_text(request.form.get("username", ""), 50)
        password = request.form.get("password", "")
        db = get_db()
        admin = db.execute("SELECT * FROM admin WHERE username = ?", (username,)).fetchone()

        if admin and check_password_hash(admin["password_hash"], password):
            session.clear()
            session["admin_logged_in"] = True
            session["admin_username"] = admin["username"]
            session["last_activity"] = datetime.utcnow().isoformat()
            flash("Admin login successful.", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Invalid admin username or password.", "danger")

    return render_template("admin_login.html")


@app.route("/admin/logout")
@admin_required
def admin_logout():
    """Logout the current admin."""
    session.clear()
    flash("Admin logged out successfully.", "info")
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    """Render the admin dashboard with tables and analytics."""
    db = get_db()
    farmers = db.execute("SELECT * FROM farmers ORDER BY created_at DESC").fetchall()
    complaints = db.execute("SELECT * FROM complaints ORDER BY created_at DESC LIMIT 8").fetchall()
    subsidies = db.execute("SELECT * FROM subsidies ORDER BY created_at DESC LIMIT 8").fetchall()
    insurance_rows = db.execute("SELECT * FROM insurance ORDER BY created_at DESC LIMIT 8").fetchall()
    loans = db.execute("SELECT * FROM loans ORDER BY created_at DESC LIMIT 8").fetchall()

    dashboard_counts = {
        "total_farmers": db.execute("SELECT COUNT(*) AS count FROM farmers").fetchone()["count"],
        "pending": db.execute(
            "SELECT COUNT(*) AS count FROM farmers WHERE application_status = 'Pending'"
        ).fetchone()["count"],
        "approved": db.execute(
            "SELECT COUNT(*) AS count FROM farmers WHERE application_status = 'Approved'"
        ).fetchone()["count"],
        "rejected": db.execute(
            "SELECT COUNT(*) AS count FROM farmers WHERE application_status = 'Rejected'"
        ).fetchone()["count"],
        "subsidy": db.execute("SELECT COUNT(*) AS count FROM subsidies").fetchone()["count"],
        "insurance": db.execute("SELECT COUNT(*) AS count FROM insurance").fetchone()["count"],
        "loan": db.execute("SELECT COUNT(*) AS count FROM loans").fetchone()["count"],
        "complaints": db.execute("SELECT COUNT(*) AS count FROM complaints").fetchone()["count"],
    }

    crop_rows = db.execute(
        "SELECT crop_type, COUNT(*) AS count FROM farmers GROUP BY crop_type ORDER BY count DESC"
    ).fetchall()
    analytics = {
        "status_labels": ["Approved", "Rejected", "Pending"],
        "status_counts": [
            dashboard_counts["approved"],
            dashboard_counts["rejected"],
            dashboard_counts["pending"],
        ],
        "crop_labels": [row["crop_type"] or "Unknown" for row in crop_rows],
        "crop_counts": [row["count"] for row in crop_rows],
        "service_labels": ["Subsidy", "Insurance", "Loan", "Complaints"],
        "service_counts": [
            dashboard_counts["subsidy"],
            dashboard_counts["insurance"],
            dashboard_counts["loan"],
            dashboard_counts["complaints"],
        ],
    }

    return render_template(
        "admin_dashboard.html",
        farmers=farmers,
        complaints=complaints,
        subsidies=subsidies,
        insurance_rows=insurance_rows,
        loans=loans,
        counts=dashboard_counts,
        analytics=analytics,
    )


@app.route("/admin/farmer/<int:farmer_db_id>")
@admin_required
def admin_farmer_detail(farmer_db_id: int):
    """Show the full farmer profile and linked applications."""
    db = get_db()
    farmer = db.execute("SELECT * FROM farmers WHERE id = ?", (farmer_db_id,)).fetchone()
    if not farmer:
        abort(404)

    subsidies = db.execute("SELECT * FROM subsidies WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    insurance_rows = db.execute("SELECT * FROM insurance WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    loans = db.execute("SELECT * FROM loans WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    complaints = db.execute("SELECT * FROM complaints WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    return render_template(
        "admin_farmer_detail.html",
        farmer=farmer,
        subsidies=subsidies,
        insurance_rows=insurance_rows,
        loans=loans,
        complaints=complaints,
    )


@app.route("/admin/farmer/<int:farmer_db_id>/approve", methods=["POST"])
@admin_required
def approve_farmer(farmer_db_id: int):
    """Approve a farmer application."""
    db = get_db()
    farmer = db.execute("SELECT * FROM farmers WHERE id = ?", (farmer_db_id,)).fetchone()
    if not farmer:
        abort(404)
    db.execute("UPDATE farmers SET application_status = 'Approved' WHERE id = ?", (farmer_db_id,))
    db.commit()
    send_sms_notification(farmer["phone"], f"AgriSmart AI update: Farmer ID {farmer['farmer_id']} approved.")
    flash("Farmer application approved successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/farmer/<int:farmer_db_id>/reject", methods=["POST"])
@admin_required
def reject_farmer(farmer_db_id: int):
    """Reject a farmer application."""
    db = get_db()
    farmer = db.execute("SELECT * FROM farmers WHERE id = ?", (farmer_db_id,)).fetchone()
    if not farmer:
        abort(404)
    db.execute("UPDATE farmers SET application_status = 'Rejected' WHERE id = ?", (farmer_db_id,))
    db.commit()
    send_sms_notification(farmer["phone"], f"AgriSmart AI update: Farmer ID {farmer['farmer_id']} rejected.")
    flash("Farmer application rejected.", "warning")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/farmer/<int:farmer_db_id>/verify-patta", methods=["POST"])
@admin_required
def verify_patta_manual(farmer_db_id: int):
    """Allow the admin to manually confirm patta verification."""
    db = get_db()
    db.execute(
        """
        UPDATE farmers
        SET patta_verification = 'Patta Verified',
            land_ownership_status = 'Fully Verified'
        WHERE id = ?
        """,
        (farmer_db_id,),
    )
    db.commit()
    flash("Patta document marked as manually verified.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/farmer/<int:farmer_db_id>/delete", methods=["POST"])
@admin_required
def delete_farmer(farmer_db_id: int):
    """Delete a farmer application."""
    db = get_db()
    db.execute("DELETE FROM farmers WHERE id = ?", (farmer_db_id,))
    db.commit()
    flash("Farmer application deleted.", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/subsidy/<int:record_id>/status", methods=["POST"])
@admin_required
def update_subsidy_status(record_id: int):
    """Update the status of a subsidy application."""
    status = sanitize_text(request.form.get("status", "Pending"), 20)
    db = get_db()
    db.execute("UPDATE subsidies SET status = ? WHERE id = ?", (status, record_id))
    db.commit()
    flash("Subsidy application status updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/insurance/<int:record_id>/status", methods=["POST"])
@admin_required
def update_insurance_status(record_id: int):
    """Update the status of an insurance application."""
    status = sanitize_text(request.form.get("status", "Pending"), 20)
    db = get_db()
    db.execute("UPDATE insurance SET status = ? WHERE id = ?", (status, record_id))
    db.commit()
    flash("Insurance application status updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/loan/<int:record_id>/status", methods=["POST"])
@admin_required
def update_loan_status(record_id: int):
    """Update the status of a loan application."""
    status = sanitize_text(request.form.get("status", "Pending"), 20)
    db = get_db()
    db.execute("UPDATE loans SET status = ? WHERE id = ?", (status, record_id))
    db.commit()
    flash("Loan application status updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/complaint/<int:record_id>/respond", methods=["POST"])
@admin_required
def respond_complaint(record_id: int):
    """Update complaint response and current complaint status."""
    response_text = sanitize_text(request.form.get("response", ""), 400)
    status = sanitize_text(request.form.get("status", "Pending"), 20)
    db = get_db()
    db.execute(
        "UPDATE complaints SET response = ?, status = ? WHERE id = ?",
        (response_text, status, record_id),
    )
    db.commit()
    flash("Complaint response saved.", "success")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------------
# Farmer routes
# ---------------------------------------------------------------------------

@app.route("/farmer/dashboard")
@farmer_required
def farmer_dashboard():
    """Render the farmer dashboard for approved farmers."""
    db = get_db()
    farmer = get_farmer_from_session()
    if not farmer:
        session.clear()
        flash("Approved farmer session not found. Please track your status again.", "warning")
        return redirect(url_for("track_status"))

    subsidies = db.execute("SELECT * FROM subsidies WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    insurance_rows = db.execute("SELECT * FROM insurance WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    loans = db.execute("SELECT * FROM loans WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    complaints = db.execute("SELECT * FROM complaints WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()

    weather = get_weather_snapshot(farmer["district"])
    crop_recommendation = recommend_crop(farmer["district"], farmer["season"], farmer["land_area"])

    counts = {
        "total_applications": len(subsidies) + len(insurance_rows) + len(loans),
        "subsidy_status": subsidies[0]["status"] if subsidies else "Not Applied",
        "insurance_status": insurance_rows[0]["status"] if insurance_rows else "Not Applied",
        "loan_status": loans[0]["status"] if loans else "Not Applied",
        "complaints": len(complaints),
    }

    analytics = {
        "labels": ["Subsidy", "Insurance", "Loan", "Complaints"],
        "values": [len(subsidies), len(insurance_rows), len(loans), len(complaints)],
    }

    return render_template(
        "farmer_dashboard.html",
        farmer=farmer,
        subsidies=subsidies,
        insurance_rows=insurance_rows,
        loans=loans,
        complaints=complaints,
        counts=counts,
        analytics=analytics,
        weather=weather,
        crop_recommendation=crop_recommendation,
    )


@app.route("/farmer/logout")
@farmer_required
def farmer_logout():
    """Logout the farmer session."""
    session.clear()
    flash("Farmer session closed successfully.", "info")
    return redirect(url_for("home"))


@app.route("/farmer/subsidy", methods=["GET", "POST"])
@farmer_required
def apply_subsidy():
    """Allow an approved farmer to apply for subsidy schemes."""
    db = get_db()
    farmer = get_farmer_from_session()
    if not farmer:
        return redirect(url_for("track_status"))

    suggestions = recommend_subsidies(farmer["crop_type"], farmer["land_area"], farmer["farmer_category"], farmer["district"])

    if request.method == "POST":
        scheme_name = sanitize_text(request.form.get("scheme_name", ""), 100)
        crop_type = sanitize_text(request.form.get("crop_type", farmer["crop_type"]), 50)
        land_area = float(request.form.get("land_area", farmer["land_area"]))
        farmer_category = sanitize_text(request.form.get("farmer_category", farmer["farmer_category"]), 50)
        district = sanitize_text(request.form.get("district", farmer["district"]), 50)
        amount = float(request.form.get("amount", "0"))
        details = sanitize_text(request.form.get("details", ""), 300)

        db.execute(
            """
            INSERT INTO subsidies (
                farmer_id, scheme_name, crop_type, land_area, farmer_category,
                district, amount, details, recommended_schemes, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                farmer["farmer_id"],
                scheme_name,
                crop_type,
                land_area,
                farmer_category,
                district,
                amount,
                details,
                ", ".join(suggestions),
                "Pending",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        flash("Subsidy application submitted successfully.", "success")
        return redirect(url_for("farmer_dashboard"))

    return render_template("subsidy.html", farmer=farmer, suggestions=suggestions)


@app.route("/farmer/insurance", methods=["GET", "POST"])
@farmer_required
def apply_insurance():
    """Allow an approved farmer to apply for crop insurance."""
    db = get_db()
    farmer = get_farmer_from_session()
    if not farmer:
        return redirect(url_for("track_status"))

    preview = assess_insurance(
        farmer["crop_type"],
        farmer["season"],
        farmer["district"],
        farmer["land_area"],
        "Drip",
        farmer["land_area"] * 50000,
    )

    if request.method == "POST":
        crop_type = sanitize_text(request.form.get("crop_type", ""), 50)
        season = sanitize_text(request.form.get("season", ""), 30)
        district = sanitize_text(request.form.get("district", ""), 50)
        land_area = float(request.form.get("land_area", "0"))
        irrigation_type = sanitize_text(request.form.get("irrigation_type", ""), 40)
        estimated_value = float(request.form.get("estimated_value", "0"))

        result = assess_insurance(crop_type, season, district, land_area, irrigation_type, estimated_value)
        db.execute(
            """
            INSERT INTO insurance (
                farmer_id, crop_type, season, district, land_area, irrigation_type,
                estimated_value, scheme_name, premium, subsidy, risk_level, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                farmer["farmer_id"],
                crop_type,
                season,
                district,
                land_area,
                irrigation_type,
                estimated_value,
                result["scheme_name"],
                result["premium"],
                result["subsidy"],
                result["risk_level"],
                "Pending",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        flash("Insurance application submitted successfully.", "success")
        return redirect(url_for("farmer_dashboard"))

    return render_template("insurance.html", farmer=farmer, preview=preview)


@app.route("/farmer/loan", methods=["GET", "POST"])
@farmer_required
def apply_loan():
    """Allow an approved farmer to apply for agriculture loans."""
    db = get_db()
    farmer = get_farmer_from_session()
    if not farmer:
        return redirect(url_for("track_status"))

    preview = assess_loan(
        farmer["crop_type"],
        farmer["district"],
        farmer["land_area"],
        farmer["annual_income"],
        100000,
        "Crop Loan",
    )

    if request.method == "POST":
        crop_type = sanitize_text(request.form.get("crop_type", ""), 50)
        land_area = float(request.form.get("land_area", "0"))
        annual_income = float(request.form.get("annual_income", "0"))
        loan_type = sanitize_text(request.form.get("loan_type", ""), 50)
        requested_amount = float(request.form.get("requested_amount", "0"))
        bank_account = sanitize_text(request.form.get("bank_account", ""), 30)
        ifsc_code = sanitize_text(request.form.get("ifsc_code", ""), 20)
        purpose = sanitize_text(request.form.get("purpose", ""), 250)
        existing_loan_details = sanitize_text(request.form.get("existing_loan_details", ""), 250)

        result = assess_loan(crop_type, farmer["district"], land_area, annual_income, requested_amount, loan_type)
        db.execute(
            """
            INSERT INTO loans (
                farmer_id, loan_type, crop_type, land_area, annual_income, requested_amount,
                interest_rate, repayment_period, eligibility_status, approval_probability,
                bank_account, ifsc_code, purpose, existing_loan_details, status, scheme_name,
                risk_level, emi_amount, amount_paid, repayment_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                farmer["farmer_id"],
                loan_type,
                crop_type,
                land_area,
                annual_income,
                requested_amount,
                result["interest_rate"],
                result["repayment_period"],
                result["eligibility_status"],
                result["approval_probability"],
                bank_account,
                ifsc_code,
                purpose,
                existing_loan_details,
                "Pending",
                result["scheme_name"],
                result["risk_level"],
                result["emi_amount"],
                0,
                "Not Started",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        flash("Loan application submitted successfully.", "success")
        return redirect(url_for("farmer_dashboard"))

    return render_template("loan.html", farmer=farmer, preview=preview)


@app.route("/farmer/loan/<int:loan_id>/repayment", methods=["GET", "POST"])
@farmer_required
def loan_repayment(loan_id: int):
    """Track and update loan repayment progress."""
    db = get_db()
    farmer = get_farmer_from_session()
    if not farmer:
        return redirect(url_for("track_status"))

    loan = db.execute(
        "SELECT * FROM loans WHERE id = ? AND farmer_id = ?",
        (loan_id, farmer["farmer_id"]),
    ).fetchone()
    if not loan:
        abort(404)

    if request.method == "POST":
        payment_amount = float(request.form.get("payment_amount", "0"))
        new_amount_paid = loan["amount_paid"] + payment_amount
        balance = max(loan["requested_amount"] - new_amount_paid, 0)
        repayment_status = "Completed" if balance == 0 else "In Progress"
        db.execute(
            "UPDATE loans SET amount_paid = ?, repayment_status = ? WHERE id = ?",
            (new_amount_paid, repayment_status, loan_id),
        )
        db.commit()
        flash("Repayment amount recorded successfully.", "success")
        return redirect(url_for("application_history"))

    balance = max(loan["requested_amount"] - loan["amount_paid"], 0)
    return render_template("loan_repayment.html", loan=loan, balance=balance)


@app.route("/farmer/loan/<int:loan_id>/receipt")
@farmer_required
def loan_receipt(loan_id: int):
    """Download a generated PDF receipt for the selected loan application."""
    db = get_db()
    farmer = get_farmer_from_session()
    if not farmer:
        return redirect(url_for("track_status"))

    loan = db.execute(
        "SELECT * FROM loans WHERE id = ? AND farmer_id = ?",
        (loan_id, farmer["farmer_id"]),
    ).fetchone()
    if not loan:
        abort(404)

    try:
        pdf_bytes = build_loan_receipt_pdf(loan, farmer)
    except RuntimeError:
        flash("PDF receipt generation requires reportlab. Please install dependencies and try again.", "warning")
        return redirect(url_for("application_history"))

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"loan_receipt_{loan['id']}.pdf",
    )


@app.route("/farmer/complaint", methods=["GET", "POST"])
@farmer_required
def submit_complaint():
    """Allow an approved farmer to submit service complaints."""
    db = get_db()
    farmer = get_farmer_from_session()
    if not farmer:
        return redirect(url_for("track_status"))

    if request.method == "POST":
        complaint_title = sanitize_text(request.form.get("complaint_title", ""), 120)
        complaint_description = sanitize_text(request.form.get("complaint_description", ""), 500)
        category = sanitize_text(request.form.get("category", ""), 50)

        db.execute(
            """
            INSERT INTO complaints (
                farmer_id, complaint_title, complaint_description, category, response, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                farmer["farmer_id"],
                complaint_title,
                complaint_description,
                category,
                "",
                "Pending",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        flash("Complaint submitted successfully.", "success")
        return redirect(url_for("farmer_dashboard"))

    return render_template("complaint.html", farmer=farmer)


@app.route("/farmer/history")
@farmer_required
def application_history():
    """Show service history for subsidy, insurance, loan, and complaints."""
    db = get_db()
    farmer = get_farmer_from_session()
    if not farmer:
        return redirect(url_for("track_status"))

    subsidies = db.execute("SELECT * FROM subsidies WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    insurance_rows = db.execute("SELECT * FROM insurance WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    loans = db.execute("SELECT * FROM loans WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()
    complaints = db.execute("SELECT * FROM complaints WHERE farmer_id = ? ORDER BY id DESC", (farmer["farmer_id"],)).fetchall()

    return render_template(
        "history.html",
        farmer=farmer,
        subsidies=subsidies,
        insurance_rows=insurance_rows,
        loans=loans,
        complaints=complaints,
    )


# ---------------------------------------------------------------------------
# Shared helper routes
# ---------------------------------------------------------------------------

@app.route("/document/<path:relative_path>")
def uploaded_document(relative_path: str):
    """Serve uploaded documents to admins or the owning farmer."""
    normalized = Path(relative_path)
    if not normalized.parts or normalized.parts[0] not in {"aadhaar", "patta", "land"}:
        abort(404)

    if session.get("admin_logged_in") is True:
        return send_from_directory(UPLOAD_DIR / normalized.parts[0], normalized.name)

    farmer = get_farmer_from_session()
    if farmer and relative_path in {
        farmer["aadhaar_image"],
        farmer["patta_document"],
        farmer["land_image"],
    }:
        return send_from_directory(UPLOAD_DIR / normalized.parts[0], normalized.name)

    abort(403)


@app.route("/health")
def health_check():
    """Simple route to confirm the application is running."""
    return jsonify({"status": "ok", "app": "AgriSmart AI"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
