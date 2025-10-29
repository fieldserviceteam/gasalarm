#!/usr/bin/env python3
import os, time, smtplib, ssl, logging
from email.mime.text import MIMEText
from pathlib import Path
from gpiozero import Button
from twilio.rest import Client
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ----------------------
# Config / ENV
# ----------------------
load_dotenv(dotenv_path=Path.home()/".gasalarm.env")

TWILIO_ENABLED = os.getenv("TWILIO_ENABLED", "1") == "1"
GPIO_PIN       = int(os.getenv("GPIO_PIN", "17"))            # BCM
ACTIVE_HIGH    = os.getenv("ACTIVE_HIGH", "1") == "1"
DEBOUNCE_S     = float(os.getenv("DEBOUNCE_S", "0.2"))
MIN_INTERVAL_S = int(os.getenv("MIN_INTERVAL_S", "300"))

TWILIO_SID   = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_FROM  = os.getenv("TWILIO_FROM", "")
PHONE_LIST   = [p.strip() for p in os.getenv("PHONE_LIST", "").split(",") if p.strip()]

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER or "pi@localhost")
SMS_GATEWAY_LIST = [e.strip() for e in os.getenv("SMS_GATEWAY_LIST","").replace(";",",").split(",") if e.strip()]
EMAIL_TO_LIST    = [e.strip() for e in os.getenv("EMAIL_TO_LIST","").replace(";",",").split(",") if e.strip()]

SITE_NAME  = os.getenv("SITE_NAME", "Hydrogen Room A")
ALARM_MSG  = os.getenv("ALARM_MSG", "HYDROGEN GAS ALARM")
CLEAR_MSG  = os.getenv("CLEAR_MSG", "Hydrogen detector returned to normal")
SEND_CLEAR = os.getenv("SEND_CLEAR", "1") == "1"
LOGFILE    = os.getenv("LOGFILE", str(Path.home()/ "gas_alarm_notifier.log"))

# ----------------------
# Logging
# ----------------------
import os, time
os.environ.setdefault("TZ", "America/Los_Angeles")  # CA local
try:
    time.tzset()    # apply TZ on Linux
except Exception:
    pass
logging.basicConfig(filename=LOGFILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
logging.info("Notifier starting")

# ----------------------
# IO
# ----------------------
button = Button(GPIO_PIN, pull_up=True, bounce_time=DEBOUNCE_S)

def alarm_asserted() -> bool:
    # pull_up=True => pressed == LOW
    logic_high = not button.is_pressed
    return logic_high if ACTIVE_HIGH else not logic_high

# ----------------------
# Senders
# ----------------------
def send_twilio(text: str):
    if not TWILIO_ENABLED:
        return False
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and PHONE_LIST):
        return False
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        for num in PHONE_LIST:
            client.messages.create(to=num, from_=TWILIO_FROM, body=text)
        logging.info("Twilio sent to %d", len(PHONE_LIST))
        return True
    except Exception as e:
        logging.error("Twilio error: %s", e)
        return False

def send_email_sms(text: str):
    if not (SMTP_HOST and (SMS_GATEWAY_LIST or EMAIL_TO_LIST)):
        return False
    try:
        sms = [r for r in SMS_GATEWAY_LIST if r.lower().endswith("@vtext.com")]
        mms = [r for r in SMS_GATEWAY_LIST if not r.lower().endswith("@vtext.com")]
        cc  = EMAIL_TO_LIST[:]

        def _send(to_list, body):
            if not to_list:
                return
            msg = MIMEText(body, _charset="us-ascii")
            msg["Subject"] = ""                   # empty = more SMS-friendly
            msg["From"] = FROM_EMAIL
            msg["To"]   = ", ".join(to_list)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(FROM_EMAIL, to_list, msg.as_string())

        # If you're not using vtext/vzwpix at all, this will just send EMAILs
        sent_lists = []

        if sms:
            _send(sms, text[:160])
            sent_lists.append(("sms", sms))
        if mms:
            _send(mms, text)
            sent_lists.append(("mms", mms))
        if cc:
            _send(cc, text)
            sent_lists.append(("email", cc))

        if not sent_lists:
            logging.warning("No email recipients configured.")
            return False

        # Log detail
        for kind, lst in sent_lists:
            if kind == "email":
                logging.info("EMAIL sent to %s | body='%s...'", lst, text[:80])
            else:
                logging.info("Email→SMS (%s) sent to %s | body='%s...'", kind, lst, text[:80])
        return True

    except Exception as e:
        logging.error("Email SMS error: %s", e, exc_info=True)
        return False


def notify(text: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = f"[{SITE_NAME}] {text} @ {stamp}"
    ok = send_twilio(payload)
    if not ok:
        ok = send_email_sms(payload)
    if not ok:
        logging.error("All notification methods failed.")
    return ok

# ----------------------
# Main loop
# ----------------------
last_alarm_sent = datetime.min
cooldown_until = datetime.min
last_state = alarm_asserted()
logging.info("Initial state: %s", "ALARM" if last_state else "NORMAL")

def check_and_alert():
    global last_alarm_sent, last_state, cooldown_until
    now = datetime.now()
    state = alarm_asserted()

    if state != last_state:
        # state changed
        last_state = state

        if state:
            # NORMAL -> ALARM edge: always send once
            if now >= cooldown_until:
                notify(ALARM_MSG)
                last_alarm_sent = now
                # future ALARM re-triggers (without a clear) are rate-limited
                cooldown_until = now + timedelta(seconds=MIN_INTERVAL_S)
            else:
                logging.info("Alarm asserted but in cooldown; no alert sent.")
        else:
            # ALARM -> NORMAL edge
            if SEND_CLEAR:
                notify(CLEAR_MSG)
            # reset cooldown so a NEW alarm after a clear always sends
            cooldown_until = datetime.min

# GPIO callbacks
button.when_pressed  = lambda: check_and_alert()
button.when_released = lambda: check_and_alert()

try:
    while True:
        # periodic guard
        check_and_alert()
        time.sleep(0.5)
except KeyboardInterrupt:
    logging.info("Notifier stopped by user")
