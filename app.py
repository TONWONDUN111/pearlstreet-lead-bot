import sqlite3
import smtplib
import time
import threading
import logging
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify, render_template_string
from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import schedule
import requests

# ================= CONFIGURATION =================
SMTP_USER = "tonwondun@gmail.com"
SMTP_PASS = "***REMOVED***"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://pearlstreetinsurance.com")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///pearl_street_leads.db")
LOG_FILE = "enterprise.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

# ================= DATABASE (SQLAlchemy ORM) =================
Base = declarative_base()
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)

class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    phone = Column(String, nullable=False)
    insurance_type = Column(String, nullable=False)
    urgency = Column(String, nullable=False)
    consent = Column(Boolean, default=True)
    verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)
    lead_score = Column(Integer, default=0)

Base.metadata.create_all(bind=engine)

# ================= LEAD SCORING ENGINE =================
def calculate_lead_score(lead_data):
    score = 0
    if lead_data.get('urgency') == 'High':
        score += 40
    elif lead_data.get('urgency') == 'Medium':
        score += 20
    if lead_data.get('insurance_type') == 'Life':
        score += 30
    elif lead_data.get('insurance_type') == 'Home':
        score += 25
    elif lead_data.get('insurance_type') == 'Auto':
        score += 20
    if lead_data.get('phone') and len(lead_data['phone']) >= 10:
        score += 10
    return min(score, 100)

# ================= FLASK APP =================
app = Flask(__name__)

LANDING_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Pearl Street Insurance - Free Quote</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f0f2f5; }
        .container { max-width: 500px; margin: auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #2c3e50; text-align: center; }
        input, select { width: 100%; padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        button { background: #3498db; color: white; padding: 12px; border: none; border-radius: 5px; cursor: pointer; width: 100%; font-size: 16px; }
        button:hover { background: #2980b9; }
        .error { color: red; }
        .success { color: green; }
    </style>
</head>
<body>
<div class="container">
    <h1>Free Insurance Quote</h1>
    <form id="quoteForm">
        <input type="text" id="first_name" placeholder="First Name" required>
        <input type="text" id="last_name" placeholder="Last Name" required>
        <input type="email" id="email" placeholder="Email" required>
        <input type="tel" id="phone" placeholder="Phone" required>
        <select id="insurance_type" required>
            <option value="">Select Insurance Type</option>
            <option>Auto</option><option>Home</option><option>Life</option><option>Business</option>
        </select>
        <select id="urgency" required>
            <option value="">Urgency</option><option>High</option><option>Medium</option><option>Low</option>
        </select>
        <label><input type="checkbox" id="consent" required> I agree to be contacted</label>
        <button type="submit">Get Quote</button>
    </form>
    <div id="message"></div>
</div>
<script>
    document.getElementById('quoteForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const data = {
            first_name: document.getElementById('first_name').value,
            last_name: document.getElementById('last_name').value,
            email: document.getElementById('email').value,
            phone: document.getElementById('phone').value,
            insurance_type: document.getElementById('insurance_type').value,
            urgency: document.getElementById('urgency').value,
            consent: document.getElementById('consent').checked
        };
        const response = await fetch('/webhook', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        const result = await response.json();
        const msgDiv = document.getElementById('message');
        if (response.ok) {
            msgDiv.className = 'success';
            msgDiv.innerText = 'Thank you! Please check your email to confirm.';
            document.getElementById('quoteForm').reset();
        } else {
            msgDiv.className = 'error';
            msgDiv.innerText = result.error || 'Something went wrong.';
        }
    });
</script>
</body>
</html>
"""

@app.route('/')
def landing():
    return render_template_string(LANDING_PAGE)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data.get('consent'):
            return jsonify({"error": "Consent is required"}), 400
        db = SessionLocal()
        existing = db.query(Lead).filter(Lead.email == data['email']).first()
        if existing:
            db.close()
            return jsonify({"error": "Email already registered"}), 409
        score = calculate_lead_score(data)
        lead = Lead(
            first_name=data['first_name'],
            last_name=data['last_name'],
            email=data['email'],
            phone=data['phone'],
            insurance_type=data['insurance_type'],
            urgency=data['urgency'],
            consent=True,
            lead_score=score
        )
        db.add(lead)
        db.commit()
        lead_id = lead.id
        db.close()
        send_confirmation_email(data['email'], data['first_name'])
        logging.info(f"Lead captured: {data['email']} (ID: {lead_id}, Score: {score})")
        return jsonify({"message": "Lead received. Check your email to confirm.", "lead_id": lead_id}), 200
    except Exception as e:
        logging.error(f"Webhook error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/confirm')
def confirm():
    email = request.args.get('email')
    if not email:
        return "Missing email parameter", 400
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.email == email).first()
    if not lead:
        db.close()
        return "Lead not found", 404
    if lead.verified:
        db.close()
        return "Already verified", 200
    lead.verified = True
    lead.verified_at = datetime.utcnow()
    db.commit()
    db.close()
    notify_owner(lead)
    return "Thank you! Your lead has been verified. An agent will contact you soon."

@app.route('/health')
def health():
    return jsonify({"status": "active", "timestamp": datetime.utcnow().isoformat()})

# ================= EMAIL FUNCTIONS =================
def send_email(to, subject, html_body):
    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = SMTP_USER
    msg['To'] = to
    msg.attach(MIMEText(html_body, 'html'))
    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

def send_confirmation_email(to_email, first_name):
    link = f"{PUBLIC_URL}/confirm?email={to_email}"
    html = f"""
    <h2>Confirm Your Quote Request</h2>
    <p>Hi {first_name},</p>
    <p>Please click the link below to confirm your insurance quote request:</p>
    <p><a href="{link}">Confirm Now</a></p>
    <p>If you did not request this, ignore this email.</p>
    """
    send_email(to_email, "Confirm Your Insurance Quote", html)

def notify_owner(lead):
    html = f"""
    <h3>New Verified Lead</h3>
    <p><strong>Name:</strong> {lead.first_name} {lead.last_name}<br>
    <strong>Email:</strong> {lead.email}<br>
    <strong>Phone:</strong> {lead.phone}<br>
    <strong>Insurance Type:</strong> {lead.insurance_type}<br>
    <strong>Urgency:</strong> {lead.urgency}<br>
    <strong>Lead Score:</strong> {lead.lead_score}/100</p>
    """
    send_email(SMTP_USER, f"NEW LEAD: {lead.first_name} {lead.last_name}", html)

# ================= FACEBOOK AUTO-POST (OPTIONAL) =================
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "YOUR_PAGE_ID")
FACEBOOK_ACCESS_TOKEN = os.environ.get("FACEBOOK_ACCESS_TOKEN", "YOUR_TOKEN")

def post_to_facebook():
    if FACEBOOK_PAGE_ID == "YOUR_PAGE_ID":
        logging.info("Facebook posting disabled (no credentials)")
        return
    message = "Get your free auto insurance quote today! https://pearlstreetinsurance.com"
    url = f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/feed"
    data = {"message": message, "access_token": FACEBOOK_ACCESS_TOKEN}
    try:
        resp = requests.post(url, data=data)
        logging.info(f"Facebook post response: {resp.json()}")
    except Exception as e:
        logging.error(f"Facebook post error: {e}")

schedule.every().day.at("09:00").do(post_to_facebook)
schedule.every().day.at("15:00").do(post_to_facebook)

def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(60)

# ================= MAIN ENTRY POINT =================
if __name__ == "__main__":
    print("\n" + "="*60)
    print("PEARL STREET INSURANCE - LEAD SYSTEM")
    print("="*60)
    print(f"Landing page: {PUBLIC_URL}")
    print(f"Webhook endpoint: {PUBLIC_URL}/webhook")
    print("="*60)
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
