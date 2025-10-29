#!/usr/bin/env python3
import os, time, smtplib, ssl, logging
from email.mime.text import MIMEText
from pathlib import Path
from gpiozero import Button
from twilio.rest import Client
from datetime import datetime, timedelta
# ----------------------
# Configuration (via .env)
# ----------------------
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path.home()/".gasalarm.env")
# GPIO and timings
GPIO_PIN = int(os.getenv("GPIO_PIN", "17"))          # BCM numbering
ACTIVE_HIGH = os.getenv("ACTIVE_HIGH", "1") == "1"   # 1 if alarm = logic HIGH
DEBOUNCE_S = float(os.getenv("DEBOUNCE_S", "0.2"))   # contact debounce
MIN_INTERVAL_S = int(os.getenv("MIN_INTERVAL_S", "300"))  # cooldown between SMS
# Twilio (optional)
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")           # e.g. "+1415XXXXXXX"
# Phone list (comma-separated, E.164)
PHONE_LIST = [p.strip() for p in os.getenv("PHONE_LIST", "").split(",") if p.strip()]
# Email-to-SMS fallback (optional)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMS_GATEWAY_LIST = [e.strip() for e in os.getenv("SMS_GATEWAY_LIST","").split(",") if e.strip()]
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER or "pi@localhost")
# Message and site metadata
SITE_NAME = os.getenv("SITE_NAME", "Hydrogen Room A")
ALARM_MSG = os.getenv("ALARM_MSG", "HYDROGEN GAS ALARM")
CLEAR_MSG = os.getenv("CLEAR_MSG", "Hydrogen detector returned to normal")
SEND_CLEAR = os.getenv("SEND_CLEAR", "1") == "1"   # send a 'clear' message?
LOGFILE = os.getenv("LOGFILE", str(Path.home()/ "gas_alarm_notifier.log"))

EMAIL_TO_LIST = [e.strip() for e in os.getenv("EMAIL_TO_LIST","").replace(";",",").split(",") if e.strip()]
SMS_GATEWAY_LIST = [e.strip() for e in os.getenv("SMS_GATEWAY_LIST","").replace(";",",").split(",") if e.strip()]

# ----------------------
# Logging
# ----------------------
logging.basicConfig(
    filename=LOGFILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
logging.info("Notifier starting")
# ----------------------
# IO
# ----------------------
# Use Button for clean edge handling; pull_up True makes release = HIGH
button = Button(GPIO_PIN, pull_up=True, bounce_time=DEBOUNCE_S)
# If ACTIVE_HIGH means alarm asserted at HIGH, we read state as:
def alarm_asserted() -> bool:
    level = button.is_pressed  # with pull_up=True, 'pressed' == at GND (LOW)
    # pressed == LOW; not pressed == HIGH
    logic_high = not level
    return logic_high if ACTIVE_HIGH else not logic_high
# ----------------------
# Senders
# ----------------------

def send_email_sms(text: str):
    if not (SMTP_HOST and (SMS_GATEWAY_LIST or EMAIL_TO_LIST)):
        return False
    try:
        # build recipients
        sms = [r for r in SMS_GATEWAY_LIST if r.lower().endswith("@vtext.com")]
        mms = [r for r in SMS_GATEWAY_LIST if not r.lower().endswith("@vtext.com")]  # vzwpix.com or normal emails, etc.
        cc  = EMAIL_TO_LIST[:]

        # SMS-friendly body (≤160 chars, 7-bit)
        sms_body = text[:160]

        def _send(to_list, body):
            if not to_list:
                return
            msg = MIMEText(body, _charset="us-ascii")
            msg["Subject"] = ""                     # IMPORTANT for SMS gateways
            msg["From"] = FROM_EMAIL
            msg["To"]   = ", ".join(to_list + cc)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(FROM_EMAIL, to_list + cc, msg.as_string())

        # try SMS first (fastest), then MMS/CC
        if sms:
            _send(sms, sms_body)
        if mms or cc:
            _send(mms or cc, text)

        logging.info("Email→SMS sent (sms=%d, mms+cc=%d)", len(sms), len(mms)+len(cc))
        return True
    except Exception as e:
        logging.error(f"Email SMS error: {e}", exc_info=True)
        return False
# ----------------------
# Main loop
# ----------------------
last_alarm_sent = datetime.min
last_state = alarm_asserted()
logging.info(f"Initial state: {'ALARM' if last_state else 'NORMAL'}")
def check_and_alert():
    global last_alarm_sent, last_state
    state = alarm_asserted()
    if state != last_state:
        # state changed
        last_state = state
        if state:
            # Alarm asserted
            if datetime.now() - last_alarm_sent > timedelta(seconds=MIN_INTERVAL_S):
                notify(ALARM_MSG)
                last_alarm_sent = datetime.now()
            else:
                logging.info("Alarm asserted but in cooldown; no SMS sent.")
        else:
            # Cleared
            if SEND_CLEAR:
                notify(CLEAR_MSG)
# Poll + edge handlers for belt & suspenders
button.when_pressed = lambda: check_and_alert()
button.when_released = lambda: check_and_alert()
try:
    while True:
        # periodic check in case an edge was missed
        check_and_alert()
        time.sleep(0.5)
except KeyboardInterrupt:
    logging.info("Notifier stopped by user")

