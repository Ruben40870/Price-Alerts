import eventlet
eventlet.monkey_patch()

import threading
import time
import requests
import yfinance as yf
import atexit
import os
from flask import Flask, render_template, request, flash, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from dotenv import load_dotenv
import logging
from flask_socketio import SocketIO, emit

# Load the .env file
load_dotenv()

# Hardcode credentials
VALID_USERNAME = os.getenv("LOGIN_USERNAME")
VALID_PASSWORD = os.getenv("LOGIN_PASSWORD")

# Flask application setup
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PUSHOVER_USER_KEY'] = os.getenv("PUSHOVER_USER_KEY")
app.config['PUSHOVER_API_TOKEN'] = os.getenv("PUSHOVER_API_TOKEN")
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "default_secret_key")

db = SQLAlchemy(app)

# Initialize Flask-SocketIO
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# Configure logging
logging.basicConfig(filename='app.log', level=logging.INFO,
                    format='%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]')
logger = logging.getLogger(__name__)

LOG_FILE = "notifications.log"

# Database model
class Alerts(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    ticker = db.Column(db.String(10), nullable=False)
    price = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(50), nullable=False)
    last_alert_sent = db.Column(db.Float, default=None)
    below_since = db.Column(db.Float, default=None)
    broker = db.Column(db.String(50), nullable=False)

    def __repr__(self):
        return f"<Alert {self.name} - {self.type} for {self.ticker}>"

# Global variables
stop_monitoring = False
monitoring_thread = None

# Initialize alert fields
def initialize_alert_fields():
    """Ensure all alerts have necessary fields initialized."""
    with app.app_context():
        alerts = Alerts.query.all()
        updated = False
        for alert in alerts:
            if alert.last_alert_sent is None:
                alert.last_alert_sent = None
                updated = True
            if alert.below_since is None:
                alert.below_since = None
                updated = True
        if updated:
            db.session.commit()

# Monitoring function
def monitor_alerts():
    """Monitor alerts in the database and send notifications when conditions are met."""
    global stop_monitoring

    # Initialize alert fields
    initialize_alert_fields()

    while not stop_monitoring:
        try:
            with app.app_context():
                alerts = Alerts.query.all()

                now = time.time()
                alerts_changed = False

                for alert in alerts:
                    ticker = alert.ticker
                    set_price = alert.price
                    alert_type = alert.type
                    last_alert_sent = alert.last_alert_sent
                    below_since = alert.below_since
                    broker = alert.broker

                    # Fetch the current price
                    current_price = get_current_price(ticker)
                    if current_price is None:
                        # If we can't get a price, skip this alert
                        continue

                    # Determine if condition is met based on alert type
                    if alert_type == "Take Profit":
                        condition_met = current_price >= set_price
                    elif alert_type == "Stop Loss Alert":
                        condition_met = current_price <= set_price
                    else:
                        # Unknown alert type; skip processing
                        continue

                    if condition_met:
                        # Condition is met
                        if below_since is not None:
                            # Condition was not met before, check how long it wasn't met
                            time_not_met = now - below_since
                            if time_not_met >= 1800:  # 30 minutes
                                # Send immediate alert since condition was re-met after 30 mins
                                success = send_pushover_notification(
                                    ticker, alert.name, set_price, current_price, alert_type, broker
                                )
                                log_notification(ticker, alert.name, set_price, current_price, broker, success, alert_type)

                                # Emit real-time log event
                                status = "SENT" if success else "FAILED"
                                log_data = {
                                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "status": status,
                                    "ticker": ticker,
                                    "name": alert.name,
                                    "price": set_price,
                                    "current_price": current_price,
                                    "type": alert_type,
                                    "broker" : broker
                                }
                                socketio.emit('new_log', log_data)

                                # Update alert fields
                                alert.last_alert_sent = now
                                alert.below_since = None
                                alerts_changed = True

                        else:
                            # Condition is met and we haven't recorded a below_since time
                            if last_alert_sent is None:
                                # Never sent an alert before, send immediately
                                success = send_pushover_notification(
                                    ticker, alert.name, set_price, current_price, alert_type, broker
                                )
                                log_notification(ticker, alert.name, set_price, current_price, broker, success, alert_type)

                                status = "SENT" if success else "FAILED"
                                log_data = {
                                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "status": status,
                                    "ticker": ticker,
                                    "name": alert.name,
                                    "price": set_price,
                                    "current_price": current_price,
                                    "type": alert_type,
                                    "broker" : broker
                                }
                                socketio.emit('new_log', log_data)

                                alert.last_alert_sent = now
                                alerts_changed = True
                            else:
                                # Condition met previously, check if it's time for a periodic alert
                                time_since_last_alert = now - last_alert_sent
                                if time_since_last_alert >= 600:  # 10 minutes
                                    # Send periodic alert
                                    success = send_pushover_notification(
                                        ticker, alert.name, set_price, current_price, alert_type, broker
                                    )
                                    log_notification(ticker, alert.name, set_price, current_price, broker, success, alert_type)

                                    status = "SENT" if success else "FAILED"
                                    log_data = {
                                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "status": status,
                                        "ticker": ticker,
                                        "name": alert.name,
                                        "price": set_price,
                                        "current_price": current_price,
                                        "type": alert_type,
                                        "broker" : broker
                                    }
                                    socketio.emit('new_log', log_data)

                                    alert.last_alert_sent = now
                                    alerts_changed = True
                                # If it's not time yet, do nothing
                    else:
                        # Condition not met
                        if below_since is None:
                            # Start tracking how long the condition is not met
                            alert.below_since = now
                            alerts_changed = True
                        # If below_since is not None, we're already tracking it, do nothing

                # Commit any changes made to alerts this cycle
                if alerts_changed:
                    db.session.commit()

            # Wait 60 seconds before checking again
            time.sleep(60)

        except Exception as e:
            logger.error(f"Error in monitoring thread: {e}")
            # Wait before retrying to prevent tight error loops
            time.sleep(60)


def get_current_price(ticker):
    """Fetch the current price of a ticker using yfinance."""
    try:
        data = yf.download(ticker, period="1d", interval="1m", progress=False)
        if not data.empty:
            # Current price is last Close from the downloaded data
            current_price = data['Close'].iloc[-1].item()
            return float(current_price)
    except Exception as e:
        logger.error(f"Error fetching data for {ticker}: {e}")
    return None

def send_pushover_notification(ticker, alert_name, set_price, current_price, alert_type, broker):
    """Send a Pushover notification."""
    PUSHOVER_USER_KEY = app.config.get("PUSHOVER_USER_KEY")
    PUSHOVER_API_TOKEN = app.config.get("PUSHOVER_API_TOKEN")

    if not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        logger.warning("Pushover credentials not set.")
        return False

    message = (f"Price Alert Triggered!\n"
               f"Ticker: {ticker}\n"
               f"Alert Name: {alert_name}\n"
               f"Alert Type: {alert_type}\n"
               f"Take Profit: ${set_price}\n"
               f"Current Price: ${current_price}\n"
               f"Broker: ${broker}")

    data = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "message": message,
        "title": "Stock Price Alert",
        "priority": 1
    }

    try:
        response = requests.post("https://api.pushover.net/1/messages.json", data=data)
        if response.status_code == 200:
            logger.info(f"Notification sent for {ticker}.")
            return True
        else:
            logger.warning(f"Failed to send notification for {ticker}. Status code: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Error sending Pushover notification: {e}")
        return False

def log_notification(ticker, alert_name, set_price, current_price, broker, success=True, alert_type="Unknown"):
    """Log notifications to a file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "SENT" if success else "FAILED"
    log_line = (
        f"[{timestamp}] {status}: Ticker: {ticker} | "
        f"Alert: {alert_name} | Amount: ${set_price} | Current: ${current_price} | Type: {alert_type} | Broker: {broker}\n"
    )

    try:
        with open(LOG_FILE, "a") as lf:
            lf.write(log_line)
    except Exception as e:
        logger.error(f"Error writing to log file: {e}")


# Start monitoring thread using SocketIO's background task
def start_monitoring():
    global stop_monitoring
    stop_monitoring = False
    socketio.start_background_task(target=monitor_alerts)

# Stop monitoring thread
def stop_monitoring_task():
    global stop_monitoring
    stop_monitoring = True
    # No need to join the thread manually as SocketIO handles it

# Register shutdown handler
atexit.register(stop_monitoring_task)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if username == VALID_USERNAME and password == VALID_PASSWORD:
            session['logged_in'] = True
            flash("Logged in successfully!", "success")
            return redirect(url_for('home'))
        else:
            flash("Invalid credentials. Please try again.", "error")
            return redirect(url_for('login'))

    # If GET request, just render the login form
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop('logged_in', None)
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

# Flask routes
@app.route("/")
def home():
    if 'logged_in' not in session or not session['logged_in']:
        return redirect(url_for('login'))
    
    search_query = request.args.get('search', '')
    alerts = Alerts.query.all()

    if search_query:
        # Filter alerts by name, ticker, or type based on search_query
        # Example:
        alerts = [a for a in alerts if search_query.lower() in a.name.lower() 
                  or search_query.lower() in a.ticker.lower()
                  or search_query.lower() in a.type.lower()]

    # Fetch current prices, etc., if you do that already
    enriched_alerts = []
    for alert in alerts:
        current_price = get_current_price(alert.ticker)
        enriched_alerts.append({
            'id': alert.id,
            'name': alert.name,
            'ticker': alert.ticker,
            'price': alert.price,
            'type': alert.type,
            'current_price': current_price,
            'broker' : alert.broker
        })

    return render_template('home.html', title='Home', alerts=enriched_alerts)

@app.route("/delete_alert/<int:alert_id>", methods=["POST"])
def delete_alert(alert_id):
    alert = Alerts.query.get_or_404(alert_id)
    db.session.delete(alert)
    db.session.commit()
    flash("Alert deleted successfully!", "success")
    return redirect(url_for('home'))

@app.route("/edit_alert/<int:alert_id>", methods=["GET", "POST"])
def edit_alert(alert_id):
    alert = Alerts.query.get_or_404(alert_id)
    if request.method == "POST":
        name = request.form.get("name")
        ticker = request.form.get("ticker")
        price = request.form.get("price")
        alert_type = request.form.get("type")
        broker = request.form.get("broker")

        if not name or not ticker or not price or not alert_type:
            flash("All fields are required.", "error")
            return redirect(url_for('edit_alert', alert_id=alert_id))

        try:
            alert.price = float(price)
        except ValueError:
            flash("Price must be a number.", "error")
            return redirect(url_for('edit_alert', alert_id=alert_id))

        alert.name = name
        alert.ticker = ticker.upper()
        alert.type = alert_type
        alert.broker = broker
        db.session.commit()
        flash("Alert updated successfully!", "success")
        return redirect(url_for('home'))

    return render_template('edit_form.html', alert=alert, title='Edit Alert')

@app.route("/form", methods=["GET", "POST"])
def form():
    if 'logged_in' not in session or not session['logged_in']:
        return redirect(url_for('login'))
    
    if request.method == "POST":
        name = request.form.get("name")
        ticker = request.form.get("ticker")
        price = request.form.get("price")
        alert_type = request.form.get("type")
        broker = request.form.get("broker")

        # Validate form inputs
        if not name or not ticker or not price or not alert_type:
            flash("All fields are required.", "error")
            return redirect(url_for('form'))

        try:
            price = float(price)
        except ValueError:
            flash("Price must be a number.", "error")
            return redirect(url_for('form'))

        new_alert = Alerts(name=name, ticker=ticker.upper(), price=price, type=alert_type, broker=broker)
        db.session.add(new_alert)
        db.session.commit()
        flash("New alert added successfully!", "success")
        return redirect(url_for('form'))

    alerts = Alerts.query.all()
    return render_template('form.html', alerts=alerts, title='Add New')

@app.route("/stop")
def stop():
    stop_monitoring_task()
    flash("Monitoring stopped.", "info")
    return redirect(url_for('home'))

from datetime import datetime

@app.route("/logs")
def logs():
    if 'logged_in' not in session or not session['logged_in']:
        return redirect(url_for('login'))
    
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as lf:
            for line in lf:
                try:
                    parts = line.strip().split('] ')
                    timestamp_str = parts[0][1:]
                    status_and_rest = parts[1].split(': ', 1)
                    status = status_and_rest[0]
                    details = status_and_rest[1]
                    details_parts = details.split(' | ')
                    ticker = details_parts[0].split(': ')[1]
                    name = details_parts[1].split(': ')[1]
                    set_price = float(details_parts[2].split(': ')[1].replace('$', ''))
                    current_price = float(details_parts[3].split(': ')[1].replace('$', ''))
                    alert_type = details_parts[4].split(': ')[1]
                    broker = details_parts[5].split(': ')[1]

                    logs.append({
                        "timestamp": datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S"),
                        "status": status,
                        "ticker": ticker,
                        "name": name,
                        "price": set_price,
                        "current_price": current_price,
                        "type": alert_type,
                        "broker": broker
                    })
                except Exception as e:
                    logger.error(f"Error parsing log line: {line.strip()} | {e}")

    # Sort logs by timestamp descending (latest first)
    logs_sorted = sorted(logs, key=lambda x: x["timestamp"], reverse=True)

    # Filters
    status_filter = request.args.get('status', '')
    ticker_filter = request.args.get('ticker', '').lower()
    name_filter = request.args.get('name', '').lower()
    type_filter = request.args.get('type', '').lower()
    page = int(request.args.get('page', 1))
    page_size = 50

    filtered_logs = []
    for log_entry in logs_sorted:
        if status_filter and log_entry["status"] != status_filter:
            continue
        if ticker_filter and ticker_filter not in log_entry["ticker"].lower():
            continue
        if name_filter and name_filter not in log_entry["name"].lower():
            continue
        if type_filter and type_filter not in log_entry["type"].lower():
            continue
        filtered_logs.append(log_entry)

    # Pagination: slice the filtered_logs to show only a certain page
    total_logs = len(filtered_logs)
    start = (page - 1) * page_size
    end = start + page_size
    page_logs = filtered_logs[start:end]

    # Determine if next/prev pages exist
    next_page = page + 1 if end < total_logs else None
    prev_page = page - 1 if page > 1 else None

    return render_template('logs.html', 
                           initial_logs=page_logs,
                           page=page,
                           next_page=next_page,
                           prev_page=prev_page,
                           total_logs=total_logs)

# Route to list all routes (for debugging)
@app.route("/routes")
def list_routes():
    import urllib
    output = []
    for rule in app.url_map.iter_rules():
        methods = ','.join(sorted(rule.methods))
        line = urllib.parse.unquote(f"{rule.endpoint:30s} {rule.rule:50s} {methods}")
        output.append(line)
    return "<br>".join(output)

# Main
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    start_monitoring()
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)