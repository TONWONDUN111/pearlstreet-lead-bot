import os
import time
import hmac
import hashlib
import logging
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
from datetime import datetime

import requests
import schedule
from flask import Flask, request, jsonify, render_template_string
from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ================= CONFIGURATION =================
BRAND_NAME = os.environ.get("BRAND_NAME", "Pearl Street Insurance")
AGENT_NAME = os.environ.get("AGENT_NAME", "Antoine McDonald")
AGENT_PHONE = os.environ.get("AGENT_PHONE", "720-603-8685")
AGENT_ADDRESS = os.environ.get("AGENT_ADDRESS", "9670 Dallas Street, Henderson, CO")

SMTP_USER = os.environ.get("SMTP_USER", "tonwondun@gmail.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", SMTP_USER)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://pearlstreetinsurance.com")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///pearl_street_leads.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
HOT_LEAD_THRESHOLD = int(os.environ.get("HOT_LEAD_THRESHOLD", "70"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

LOG_FILE = "enterprise.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)

if not SMTP_PASS:
    logging.warning("SMTP_PASS is not set - email sending is disabled until configured.")

# ================= DATABASE (SQLAlchemy ORM) =================
Base = declarative_base()
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
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
    sequence_step = Column(Integer, default=0)
    last_sequence_sent_at = Column(DateTime, nullable=True)
    unsubscribed = Column(Boolean, default=False)


Base.metadata.create_all(bind=engine)

# Lightweight migration for pre-existing SQLite databases that predate the
# sequence_step / last_sequence_sent_at / unsubscribed columns.
if engine.dialect.name == "sqlite":
    with engine.connect() as conn:
        existing_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(leads)"))}
        for col, ddl in [
            ("sequence_step", "ALTER TABLE leads ADD COLUMN sequence_step INTEGER DEFAULT 0"),
            ("last_sequence_sent_at", "ALTER TABLE leads ADD COLUMN last_sequence_sent_at DATETIME"),
            ("unsubscribed", "ALTER TABLE leads ADD COLUMN unsubscribed BOOLEAN DEFAULT 0"),
        ]:
            if col not in existing_cols:
                conn.execute(text(ddl))
                conn.commit()


# ================= LEAD SCORING & ROUTING =================
def calculate_lead_score(lead_data):
    score = 0
    # Urgency
    if lead_data.get('urgency') == 'High':
        score += 40
    elif lead_data.get('urgency') == 'Medium':
        score += 20
    # Insurance type value
    itype = lead_data.get('insurance_type')
    if itype == 'Business':
        score += 35
    elif itype == 'Life':
        score += 30
    elif itype == 'Home':
        score += 25
    elif itype == 'Renters':
        score += 22
    elif itype == 'Auto':
        score += 20
    # Phone completeness
    if lead_data.get('phone') and len(lead_data['phone']) >= 10:
        score += 10
    return min(score, 100)


def is_hot_lead(score):
    return score >= HOT_LEAD_THRESHOLD


# ================= FLASK APP =================
app = Flask(__name__)

LANDING_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ brand_name }} - Free Quote</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f0f2f5; }
        .container { max-width: 500px; margin: auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #2c3e50; text-align: center; margin-bottom: 5px; }
        .subtitle { text-align: center; color: #7f8c8d; margin-bottom: 15px; font-size: 14px; }
        .trust-bar { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-bottom: 20px; }
        .trust-bar span { background: #eaf4fc; color: #2c80b4; font-size: 12px; padding: 4px 10px; border-radius: 12px; }
        input, select { width: 100%; padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        button { background: #3498db; color: white; padding: 12px; border: none; border-radius: 5px; cursor: pointer; width: 100%; font-size: 16px; }
        button:hover { background: #2980b9; }
        .error { color: red; }
        .success { color: green; }
        .contact { text-align: center; margin-top: 18px; font-size: 13px; color: #7f8c8d; }
        /* Chat widget */
        #chat-bubble { position: fixed; bottom: 20px; right: 20px; width: 56px; height: 56px; border-radius: 50%; background: #3498db; color: white; display: flex; align-items: center; justify-content: center; font-size: 26px; cursor: pointer; box-shadow: 0 2px 10px rgba(0,0,0,0.2); z-index: 1000; }
        #chat-panel { position: fixed; bottom: 86px; right: 20px; width: 320px; max-height: 420px; background: white; border-radius: 10px; box-shadow: 0 2px 16px rgba(0,0,0,0.25); display: none; flex-direction: column; overflow: hidden; z-index: 1000; }
        #chat-header { background: #2c3e50; color: white; padding: 12px; font-weight: bold; }
        #chat-messages { flex: 1; padding: 10px; overflow-y: auto; font-size: 14px; }
        #chat-messages .msg { margin-bottom: 8px; line-height: 1.4; }
        #chat-messages .user { text-align: right; color: #2c3e50; }
        #chat-messages .bot { text-align: left; color: #2c80b4; }
        #chat-input-row { display: flex; border-top: 1px solid #eee; }
        #chat-input { flex: 1; border: none; padding: 10px; font-size: 14px; }
        #chat-send { width: auto; border-radius: 0; }
    </style>
</head>
<body>
<div class="container">
    <h1>Free Insurance Quote</h1>
    <div class="subtitle">{{ brand_name }} &middot; Serving Colorado Families &amp; Businesses</div>
    <div class="trust-bar">
        <span>Licensed &amp; Insured</span>
        <span>Multiple Carriers</span>
        <span>Free Consultations</span>
        <span>Based in Henderson, CO</span>
    </div>
    <form id="quoteForm">
        <input type="text" id="first_name" placeholder="First Name" required>
        <input type="text" id="last_name" placeholder="Last Name" required>
        <input type="email" id="email" placeholder="Email" required>
        <input type="tel" id="phone" placeholder="Phone" required>
        <select id="insurance_type" required>
            <option value="">Select Insurance Type</option>
            <option>Auto</option><option>Home</option><option>Renters</option><option>Life</option><option>Business</option>
        </select>
        <select id="urgency" required>
            <option value="">Urgency</option><option>High</option><option>Medium</option><option>Low</option>
        </select>
        <label><input type="checkbox" id="consent" required> I agree to be contacted</label>
        <button type="submit">Get Quote</button>
    </form>
    <div id="message"></div>
    <div class="contact">Or call/text {{ agent_phone }} &middot; {{ agent_address }}</div>
</div>

<div id="chat-bubble">&#128172;</div>
<div id="chat-panel">
    <div id="chat-header">Ask {{ brand_name }}</div>
    <div id="chat-messages"></div>
    <div id="chat-input-row">
        <input id="chat-input" type="text" placeholder="Ask a question...">
        <button id="chat-send">Send</button>
    </div>
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

    // Chat widget
    const chatBubble = document.getElementById('chat-bubble');
    const chatPanel = document.getElementById('chat-panel');
    const chatMessages = document.getElementById('chat-messages');
    const chatInput = document.getElementById('chat-input');
    const chatSend = document.getElementById('chat-send');
    let chatHistory = [];

    chatBubble.addEventListener('click', () => {
        chatPanel.style.display = chatPanel.style.display === 'flex' ? 'none' : 'flex';
        if (chatMessages.children.length === 0) {
            addChatMessage('bot', "Hi! I'm here to help with questions about Auto, Home, Renters, Business, or Life insurance in Colorado. How can I help?");
        }
    });

    function addChatMessage(role, text) {
        const div = document.createElement('div');
        div.className = 'msg ' + (role === 'user' ? 'user' : 'bot');
        div.innerText = text;
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    async function sendChat() {
        const text = chatInput.value.trim();
        if (!text) return;
        addChatMessage('user', text);
        chatHistory.push({role: 'user', content: text});
        chatInput.value = '';
        try {
            const response = await fetch('/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: text, history: chatHistory})
            });
            const result = await response.json();
            addChatMessage('bot', result.reply);
            chatHistory.push({role: 'assistant', content: result.reply});
        } catch (err) {
            addChatMessage('bot', 'Sorry, something went wrong. Please call/text {{ agent_phone }}.');
        }
    }
    chatSend.addEventListener('click', sendChat);
    chatInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendChat(); });
</script>
</body>
</html>
"""


@app.route('/')
def landing():
    return render_template_string(
        LANDING_PAGE,
        brand_name=BRAND_NAME,
        agent_phone=AGENT_PHONE,
        agent_address=AGENT_ADDRESS,
    )


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
            lead_score=score,
        )
        db.add(lead)
        db.commit()
        lead_id = lead.id
        hot = is_hot_lead(score)
        db.close()

        # Email failures should never block lead capture - log and continue.
        try:
            send_confirmation_email(data['email'], data['first_name'])
        except Exception as e:
            logging.error(f"Failed to send confirmation email to {data['email']}: {e}")

        if hot:
            try:
                notify_owner_hot_lead_pending(lead_id, data, score)
            except Exception as e:
                logging.error(f"Failed to send hot-lead alert for {data['email']}: {e}")

        logging.info(f"Lead captured: {data['email']} (ID: {lead_id}, Score: {score}, Hot: {hot})")
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

    try:
        notify_owner_verified(lead)
    except Exception as e:
        logging.error(f"Failed to send owner notification for {lead.email}: {e}")

    return "Thank you! Your lead has been verified. An agent will contact you soon."


@app.route('/unsubscribe')
def unsubscribe():
    email = request.args.get('email', '')
    token = request.args.get('token', '')
    if not email or token != _unsubscribe_token(email):
        return "Invalid unsubscribe link", 400
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.email == email).first()
    if lead:
        lead.unsubscribed = True
        db.commit()
    db.close()
    return "You've been unsubscribed and won't receive further emails from us."


@app.route('/chat', methods=['POST'])
def chat():
    data = request.json or {}
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    if not ANTHROPIC_API_KEY:
        return jsonify({"reply": f"Our chat assistant isn't available right now - call or text us at {AGENT_PHONE} and we'll help right away!"})

    history = data.get("history") or []
    messages = []
    for turn in history[-6:]:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)[:2000]})
    messages.append({"role": "user", "content": user_message[:2000]})

    system_prompt = (
        f"You are a friendly assistant for {BRAND_NAME}, an independent insurance agency "
        f"based in Henderson, Colorado, serving Auto, Home, Renters, Business and Life "
        f"insurance customers across Colorado. "
        f"Answer general insurance questions briefly and helpfully. "
        f"Gently encourage visitors to fill out the quote form on this page for a free, "
        f"personalized quote, or to call/text {AGENT_PHONE}. "
        f"Never quote specific prices or give legal/financial/tax advice. "
        f"If asked about anything unrelated to insurance or this agency, politely redirect "
        f"back to insurance topics. Keep responses to 2-4 short sentences. "
        f"Agent name: {AGENT_NAME}."
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 300,
                "system": system_prompt,
                "messages": messages,
            },
            timeout=20,
        )
        resp.raise_for_status()
        reply = resp.json()["content"][0]["text"]
        return jsonify({"reply": reply})
    except Exception as e:
        logging.error(f"Chat error: {e}")
        return jsonify({"reply": f"Sorry, I'm having trouble right now. Please call/text {AGENT_PHONE}."}), 200


@app.route('/health')
def health():
    return jsonify({"status": "active", "timestamp": datetime.utcnow().isoformat()})


# ================= EMAIL FUNCTIONS =================
def _unsubscribe_token(email):
    return hmac.new(SECRET_KEY.encode(), email.encode(), hashlib.sha256).hexdigest()[:16]


def send_email(to, subject, html_body):
    if not SMTP_PASS:
        logging.warning(f"Skipping email to {to} ('{subject}') - SMTP_PASS not configured")
        return
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


def notify_owner_hot_lead_pending(lead_id, data, score):
    html = f"""
    <h3>New Hot Lead (awaiting email confirmation)</h3>
    <p><strong>Name:</strong> {data['first_name']} {data['last_name']}<br>
    <strong>Email:</strong> {data['email']}<br>
    <strong>Phone:</strong> {data['phone']}<br>
    <strong>Insurance Type:</strong> {data['insurance_type']}<br>
    <strong>Urgency:</strong> {data['urgency']}<br>
    <strong>Lead Score:</strong> {score}/100</p>
    <p>This lead has not yet confirmed their email. You'll get a second notification once they do.</p>
    """
    send_email(OWNER_EMAIL, f"\U0001F525 HOT LEAD (unconfirmed): {data['first_name']} {data['last_name']}", html)


def notify_owner_verified(lead):
    prefix = "\U0001F525 HOT LEAD" if is_hot_lead(lead.lead_score) else "New Verified Lead"
    html = f"""
    <h3>New Verified Lead</h3>
    <p><strong>Name:</strong> {lead.first_name} {lead.last_name}<br>
    <strong>Email:</strong> {lead.email}<br>
    <strong>Phone:</strong> {lead.phone}<br>
    <strong>Insurance Type:</strong> {lead.insurance_type}<br>
    <strong>Urgency:</strong> {lead.urgency}<br>
    <strong>Lead Score:</strong> {lead.lead_score}/100</p>
    """
    send_email(OWNER_EMAIL, f"{prefix}: {lead.first_name} {lead.last_name}", html)


# ================= AUTOMATED FOLLOW-UP SEQUENCE =================
# Based on the welcome (days 1/3/7/14) and follow-up (days 21/35/60) nurture
# sequence. Anchored on the lead's verified_at timestamp.
SEQUENCE_EMAILS = [
    {
        "day": 1,
        "subject": "Welcome to {brand_name} - Let's Get You Protected",
        "body": """
        <p>Hi {first_name},</p>
        <p>Thanks for reaching out to {brand_name}! I'm {agent_name}, and I'm excited to help you find
        the right insurance coverage anywhere in Colorado.</p>
        <p>We've received your quote request and our team is reviewing it. In the meantime, here's what
        you can expect:</p>
        <ul>
            <li>A personalized quote within 24-48 hours</li>
            <li>No pressure sales - just honest advice</li>
            <li>Coverage options tailored to Colorado residents</li>
        </ul>
        <p>If you have any questions, reply to this email or text us at {agent_phone}.</p>
        <p>Best,<br>{agent_name}<br>{brand_name}<br>{agent_address}<br>{agent_phone}</p>
        """,
    },
    {
        "day": 3,
        "subject": "Why {brand_name} Stands Out in Colorado",
        "body": """
        <p>Hi {first_name},</p>
        <p>At {brand_name}, we believe insurance should be straightforward and affordable. Here's why
        families and businesses across Colorado choose us:</p>
        <ul>
            <li>Local expertise: we understand Colorado-specific risks like hail damage and winter driving</li>
            <li>Multiple carriers: we compare rates from top providers to find you the best deal</li>
            <li>Personalized service: real conversations about your coverage needs</li>
        </ul>
        <p>Your quote is being finalized. Ready to discuss options? Call me at {agent_phone} or reply here.</p>
        <p>Best,<br>{agent_name}</p>
        """,
    },
    {
        "day": 7,
        "subject": "Quick Insurance Tips for Living in Colorado",
        "body": """
        <p>Hi {first_name},</p>
        <p>Living in Colorado means dealing with unique insurance considerations. Here are 3 quick tips:</p>
        <ol>
            <li><strong>Auto Insurance:</strong> Colorado requires minimum coverage, but consider adding
            comprehensive for hail protection</li>
            <li><strong>Home Insurance:</strong> Review hail, wildfire, and water-related coverage based
            on your property location</li>
            <li><strong>Umbrella Policy:</strong> Extra liability protection is smart for growing families</li>
        </ol>
        <p>We're still working on your personalized quote. What's your biggest insurance concern right now?</p>
        <p>Text or call: {agent_phone}</p>
        <p>Best,<br>{agent_name}</p>
        """,
    },
    {
        "day": 14,
        "subject": "Your {brand_name} Quote is Ready",
        "body": """
        <p>Hi {first_name},</p>
        <p>Your customized insurance quote from {brand_name} is complete! We compared rates from multiple
        carriers to find the best options for your situation in Colorado.</p>
        <p>Next step: let's review your quote together. This usually takes 15-20 minutes and we can do it
        over the phone or in person.</p>
        <p>When works best for you this week?</p>
        <p>Call: {agent_phone}</p>
        <p>Looking forward to protecting your family!</p>
        <p>Best,<br>{agent_name}</p>
        """,
    },
    {
        "day": 21,
        "subject": "Following Up on Your Insurance Quote Request",
        "body": """
        <p>Hi {first_name},</p>
        <p>I wanted to check in about your insurance quote request. We put together some great options for
        you, and I'd hate for you to miss out on potential savings.</p>
        <p>Have you had a chance to review the quote? Any questions about coverage or rates?</p>
        <p>I'm here to help - just reply or call {agent_phone}.</p>
        <p>Best,<br>{agent_name}</p>
        """,
    },
    {
        "day": 35,
        "subject": "Free Insurance Checklist for Colorado Residents",
        "body": """
        <p>Hi {first_name},</p>
        <p>Even if you're not ready to buy insurance right now, here's a quick checklist we put together
        for Colorado homeowners and drivers:</p>
        <ul>
            <li>Coverage gaps to watch for</li>
            <li>Colorado insurance requirements</li>
            <li>Money-saving tips</li>
        </ul>
        <p>Still interested in that quote? Let's chat - {agent_phone}.</p>
        <p>Best,<br>{agent_name}</p>
        """,
    },
    {
        "day": 60,
        "subject": "Last Chance: Your {brand_name} Quote",
        "body": """
        <p>Hi {first_name},</p>
        <p>This is my final follow-up about your insurance quote. If you're still shopping for coverage,
        we'd love to help.</p>
        <p>We help clients across Colorado and work with multiple carriers to find strong coverage at
        competitive rates.</p>
        <p>Otherwise, call {agent_phone} to discuss your options.</p>
        <p>Best wishes,<br>{agent_name}</p>
        """,
    },
]


def send_sequence_email(lead, template):
    token = _unsubscribe_token(lead.email)
    unsubscribe_link = f"{PUBLIC_URL}/unsubscribe?email={lead.email}&token={token}"
    context = {
        "first_name": lead.first_name,
        "brand_name": BRAND_NAME,
        "agent_name": AGENT_NAME,
        "agent_phone": AGENT_PHONE,
        "agent_address": AGENT_ADDRESS,
    }
    body = template["body"].format(**context)
    body += f'<p style="font-size:12px;color:#999;">Don\'t want these emails? <a href="{unsubscribe_link}">Unsubscribe</a>.</p>'
    send_email(lead.email, template["subject"].format(**context), body)


def run_sequence_emails():
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        leads = db.query(Lead).filter(Lead.verified == True, Lead.unsubscribed == False).all()  # noqa: E712
        for lead in leads:
            step = lead.sequence_step or 0
            if step >= len(SEQUENCE_EMAILS):
                continue
            anchor = lead.verified_at or lead.created_at
            days_elapsed = (now - anchor).days
            template = SEQUENCE_EMAILS[step]
            if days_elapsed >= template["day"]:
                try:
                    send_sequence_email(lead, template)
                    lead.sequence_step = step + 1
                    lead.last_sequence_sent_at = now
                    db.commit()
                    logging.info(f"Sent sequence email #{step + 1} to {lead.email}")
                except Exception as e:
                    logging.error(f"Sequence email error for {lead.email}: {e}")
    finally:
        db.close()


# ================= FACEBOOK AUTO-POST (OPTIONAL) =================
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "")
FACEBOOK_ACCESS_TOKEN = os.environ.get("FACEBOOK_ACCESS_TOKEN", "")


def post_to_facebook():
    if not FACEBOOK_PAGE_ID or not FACEBOOK_ACCESS_TOKEN:
        logging.info("Facebook posting disabled (no credentials)")
        return
    message = f"Get your free insurance quote today from {BRAND_NAME}! {PUBLIC_URL}"
    url = f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/feed"
    data = {"message": message, "access_token": FACEBOOK_ACCESS_TOKEN}
    try:
        resp = requests.post(url, data=data, timeout=20)
        logging.info(f"Facebook post response: {resp.json()}")
    except Exception as e:
        logging.error(f"Facebook post error: {e}")


schedule.every().day.at("09:00").do(post_to_facebook)
schedule.every().day.at("15:00").do(post_to_facebook)
schedule.every().day.at("08:00").do(run_sequence_emails)


def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(60)


# ================= MAIN ENTRY POINT =================
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print(f"{BRAND_NAME.upper()} - LEAD SYSTEM")
    print("=" * 60)
    print(f"Landing page: {PUBLIC_URL}")
    print(f"Webhook endpoint: {PUBLIC_URL}/webhook")
    print("=" * 60)

    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
