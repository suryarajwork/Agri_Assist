import os
from datetime import datetime, timedelta
import requests
import random
import hashlib
from flask import Flask, request, session, jsonify, render_template, redirect, url_for, flash, g
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user, login_required
from flask_bcrypt import Bcrypt
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect, generate_csrf
from wtforms import StringField, PasswordField, BooleanField, SubmitField, FloatField, SelectField
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError, Length
from dotenv import load_dotenv
from flask_babel import Babel, _, lazy_gettext as _l, format_date, format_datetime, format_time, format_timedelta
from flask_mail import Mail, Message

# --- 1. Configuration and Initialization ---

load_dotenv()
app = Flask(__name__)

# --- Helper to clean environment variables ---
def clean_env(key, default=None):
    val = os.environ.get(key, default)
    if val and isinstance(val, str):
        # Remove literal quotes if they exist in the .env file
        return val.strip("'").strip('"').strip()
    return val

# --- VERCEL FIX 1: SECRET KEY ---
# Ensures sessions don't break on Vercel restart
app.config['SECRET_KEY'] = clean_env('SECRET_KEY', 'fallback_key_for_local_use_only')
app.config['GOOGLE_MAPS_API_KEY'] = clean_env('GOOGLE_MAPS_API_KEY')

# Detect Vercel environment
IS_VERCEL = "VERCEL" in os.environ

# --- VERCEL FIX 2: DATABASE CONNECTION (Neon Postgres) ---
# Get the URL from environment (Vercel sets DATABASE_URL automatically for Neon)
database_url = clean_env('DATABASE_URL') or clean_env('POSTGRES_URL')

if database_url:
    # Fix for SQLAlchemy: It expects 'postgresql://' but Neon sometimes returns 'postgres://'
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    # Fallback to SQLite
    if IS_VERCEL:
        # Vercel filesystem is read-only, use /tmp for ephemeral testing if no DB is provided
        db_path = "/tmp/app.db"
    else:
        db_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'app.db')
    
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- I18N CONFIG ---
app.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
app.config['LANGUAGES'] = {
    'en': 'English',
    'hi': 'हिंदी',
    'kn': 'ಕನ್ನಡ', # Kannada
}

# --- VERCEL FIX 3: SESSIONS ---
# We use standard Flask "Signed Cookies" for Vercel. 
# Filesystem sessions (Flask-Session) crash on Vercel because the disk is read-only.
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=60)
# Secure cookies for HTTPS (only in production)
app.config['SESSION_COOKIE_SECURE'] = not app.debug or IS_VERCEL
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# MAIL CONFIGS
app.config['MAIL_SERVER'] = clean_env('MAIL_SERVER', 'smtp.gmail.com')
try:
    app.config['MAIL_PORT'] = int(clean_env('MAIL_PORT', 587))
except (ValueError, TypeError):
    app.config['MAIL_PORT'] = 587

app.config['MAIL_USE_TLS'] = str(clean_env('MAIL_USE_TLS', 'True')).lower() in ['true', '1', 't']
app.config['MAIL_USERNAME'] = clean_env('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = clean_env('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = clean_env('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
csrf = CSRFProtect(app)
babel = Babel()
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = _('Please log in to access this page.')
mail = Mail(app)

# Ensure a valid default sender exists
if not app.config.get('MAIL_DEFAULT_SENDER'):
    app.config['MAIL_DEFAULT_SENDER'] = app.config.get('MAIL_USERNAME', 'noreply@agriassist.com')


# --- 2. DATA LOADING AND PREPARATION (Rule-Based Model) ---

CROP_OPTIONS = [
    "Rice", "Wheat", "Maize", "Cotton", "Sugarcane", "Groundnut", "Coconut",
    "Millet", "Pulses", "Soybean", "Tea", "Banana", "Potato", "Tomato", "Onion"
]
SEASON_OPTIONS = ["Kharif", "Rabi", "Zaid", "Whole Year"]
STATE_OPTIONS = sorted([
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa",
    "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala",
    "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland",
    "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura",
    "Uttar Pradesh", "Uttarakhand", "West Bengal", "Andaman & Nicobar", "Chandigarh",
    "Dadra & Nagar Haveli", "Daman & Diu", "Delhi", "Jammu & Kashmir", "Ladakh",
    "Lakshadweep", "Puducherry"
])

# Data for Detailed AI Advisories (used for dropdowns)
CROP_ADVISORY_DATA = {
    "Rice": {
        "ideal_temp": (21, 37),
        "stages": {
            "Pre-Planting": "Prepare the main field by puddling 2-3 times to reduce water seepage and control weeds. Ensure nursery beds for seedlings are well-managed and seedlings are 20-25 days old before transplanting.",
            "Germination": "Ensure fields are well-puddled and maintain a shallow water level (2-5 cm). Monitor for pests like snails.",
            "Vegetative": "Top-dress with Nitrogen fertilizer as per local recommendations. Maintain a consistent water level. Watch for early signs of stem borer.",
            "Flowering": "This is a critical stage for water; avoid water stress. High temperatures or strong winds can affect pollination. Scout for fungal diseases like blast, especially in humid conditions.",
            "Fruiting": "Also known as the grain-filling stage. Maintain soil moisture. Protect against birds and rodents. Plan for harvest as grains mature.",
            "Harvest": "Harvest when 80-85% of the panicles have turned golden yellow. Drain the field a week before harvesting to facilitate movement. Thresh the crop as soon as possible to minimize losses."
        }
    },
    "Wheat": {
        "ideal_temp": (12, 25),
        "stages": {
            "Pre-Planting": "Focus on deep ploughing to create a fine tilth. Conduct a soil test to determine nutrient deficiencies and apply basal fertilizers (Phosphorus, Potassium) accordingly. Ensure the field is laser-leveled for uniform water distribution.",
            "Germination": "Sow at the optimal depth in soil with adequate moisture. Protect against birds. A light, early irrigation can ensure uniform germination.",
            "Vegetative": "Apply the first dose of nitrogen fertilizer along with the first irrigation (usually around 21 days after sowing). Control weeds, as they compete for nutrients.",
            "Flowering": "This stage (booting and heading) is sensitive to heat and water stress. Ensure adequate moisture. High temperatures (>30C) can severely impact grain formation.",
            "Fruiting": "The grain filling period requires cool weather. Monitor for aphids and rust disease. Avoid irrigation close to harvest to ensure proper drying.",
            "Harvest": "Harvest when grains are hard and contain less than 20% moisture. Delaying can lead to shattering losses. Use a combine harvester for efficiency and clean the grains before storage to prevent spoilage."
        }
    },
    "Tomato": {
        "ideal_temp": (21, 29),
        "stages": {
            "Pre-Planting": "Select a well-drained field with good sun exposure. Incorporate well-rotted farmyard manure or compost. Prepare raised beds to improve drainage and aeration.",
            "Germination": "Ensure consistent moisture in seedbeds. Protect seedlings from strong sun and pests like cutworms.",
            "Vegetative": "Provide staking or support. Apply a balanced fertilizer. Watch for aphids and whiteflies. Ensure good air circulation to prevent fungal growth.",
            "Flowering": "Crucial period for pollination. Avoid over-watering which can lead to flower drop. Monitor calcium levels to prevent blossom-end rot.",
            "Fruiting": "Maintain consistent watering to prevent fruit cracking. Scout for fruit borers and late blight, especially after rain. Apply potassium-rich fertilizer for better fruit development.",
            "Harvest": "Harvest fruits based on market distance. For local markets, pick at the 'red ripe' stage. For distant markets, harvest at the 'mature green' or 'breaker' stage. Avoid bruising the fruit."
        }
    },
    "Potato": {
        "ideal_temp": (15, 20),
        "stages": {
            "Pre-Planting": "Choose certified, disease-free seed tubers. Prepare the soil to be loose and friable. Apply a basal dose of fertilizer and create ridges and furrows for planting.",
            "Germination": "Plant in well-drained, loose soil. Ensure tubers are not exposed to sunlight to prevent greening.",
            "Vegetative": "Perform 'hilling' or 'earthing up' to cover developing tubers. Apply nitrogen fertilizer. Watch for potato beetles.",
            "Flowering": "This indicates tuber formation is active. Ensure consistent moisture. This is a key time to scout for early and late blight.",
            "Fruiting": "Tubers are bulking up. Reduce nitrogen application but ensure potassium is available. Monitor soil moisture carefully to avoid hollow heart or cracks.",
            "Harvest": "Begin harvesting when the haulms (stems/leaves) start to yellow. Stop irrigation about 10 days before harvesting to allow the skin to set. Cure the tubers in a cool, dark place before storage."
        }
    },
    "Cotton": {
        "ideal_temp": (21, 35),
        "stages": {
            "Pre-Planting": "Ensure deep ploughing to break any hardpan in the soil. Sow high-quality, certified seeds treated with fungicide. Follow proper spacing recommendations for your region.",
            "Germination": "Requires warm soil for germination. Avoid deep sowing. Protect seedlings from sucking pests like thrips.",
            "Vegetative": "Manage weeds effectively. Apply nitrogen in split doses. Ensure good drainage as cotton is sensitive to waterlogging.",
            "Flowering": "Also known as squaring and flowering. This is a critical water-use period. Watch for bollworms and apply control measures as needed.",
            "Fruiting": "This is the boll development stage. Maintain soil moisture to prevent square and boll drop. A potassium application can improve boll weight and fiber quality.",
            "Harvest": "Pick cotton bolls as they mature and open. Picking should be done in the morning to retain moisture and quality. Avoid picking during wet conditions. Dry the seed cotton (kapas) before storing."
        }
    },
    "default": {
        "ideal_temp": (18, 32),
        "stages": {
            "Pre-Planting": "Prepare the field by clearing weeds and old crop residue. Conduct a soil test to understand nutrient requirements and apply a balanced basal fertilizer dose.",
            "Germination": "Ensure good seed-to-soil contact and consistent moisture for successful germination.",
            "Vegetative": "Apply growth-stage appropriate nutrients and monitor for common pests and weeds.",
            "Flowering": "This is a sensitive stage. Ensure adequate water and nutrients, and protect from extreme weather.",
            "Fruiting": "Monitor for pests and diseases that target the fruit/grain. Plan for harvest based on maturity indicators.",
            "Harvest": "Harvest the crop at its peak maturity to ensure maximum yield and quality. Follow recommended post-harvest handling procedures to minimize losses."
        }
    }
}

SOIL_TYPE_OPTIONS = sorted(["Sandy", "Clay", "Loamy", "Silty", "Peaty", "Chalky"])

all_stages = set()
for data in CROP_ADVISORY_DATA.values():
    if "stages" in data:
        all_stages.update(data["stages"].keys())
CROP_STAGE_OPTIONS = sorted(list(all_stages))


CROP_DATA = {
    "Rice":      {'base': 3.0, 'opt_rain': 1400, 'fert_sens': 0.4, 'pest_sens': 0.1, 'rec_fert': 200},
    "Wheat":     {'base': 2.8, 'opt_rain': 800,  'fert_sens': 0.5, 'pest_sens': 0.08, 'rec_fert': 180},
    "Maize":     {'base': 2.5, 'opt_rain': 750,  'fert_sens': 0.45, 'pest_sens': 0.12, 'rec_fert': 220},
    "Cotton":    {'base': 1.2, 'opt_rain': 900,  'fert_sens': 0.3, 'pest_sens': 0.15, 'rec_fert': 150},
    "Sugarcane": {'base': 60.0, 'opt_rain': 2000, 'fert_sens': 0.2, 'pest_sens': 0.05, 'rec_fert': 300},
    "Potato":    {'base': 20.0, 'opt_rain': 700,  'fert_sens': 0.6, 'pest_sens': 0.1, 'rec_fert': 250},
    "Tomato":    {'base': 25.0, 'opt_rain': 800,  'fert_sens': 0.55, 'pest_sens': 0.13, 'rec_fert': 200},
    "Onion":     {'base': 18.0, 'opt_rain': 750,  'fert_sens': 0.5, 'pest_sens': 0.1, 'rec_fert': 170},
    "default":   {'base': 1.0, 'opt_rain': 1000, 'fert_sens': 0.3, 'pest_sens': 0.1, 'rec_fert': 120}
}
for crop in CROP_OPTIONS:
    if crop not in CROP_DATA: CROP_DATA[crop] = CROP_DATA["default"]

STATE_PRODUCTIVITY_FACTOR = {
    "Punjab": 1.15, "Haryana": 1.1, "Uttar Pradesh": 1.05, "Rajasthan": 0.85, "default": 1.0
}
print("Rule-based prediction data loaded successfully.")


# --- 3. Prediction Logic & Farmer Advice Helper ---

WMO_WEATHER_CODES = {
    0: 'Clear sky', 1: 'Mainly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Depositing rime fog',
    51: 'Light drizzle', 53: 'Moderate drizzle', 55: 'Dense drizzle',
    61: 'Slight rain', 63: 'Moderate rain', 65: 'Heavy rain',
    66: 'Light freezing rain', 67: 'Heavy freezing rain',
    71: 'Slight snow fall', 73: 'Moderate snow fall', 75: 'Heavy snow fall',
    80: 'Slight rain showers', 81: 'Moderate rain showers', 82: 'Violent rain showers',
    95: 'Slight or moderate thunderstorm', 96: 'Thunderstorm with slight hail', 99: 'Thunderstorm with heavy hail',
}

def get_historical_yields(crop, state):
    """Simulates historical yield data for the last 4 years."""
    current_year = datetime.now().year
    years = list(range(current_year - 4, current_year))
    params = CROP_DATA.get(crop, CROP_DATA["default"])
    base_yield = params['base']
    state_factor = STATE_PRODUCTIVITY_FACTOR.get(state, STATE_PRODUCTIVITY_FACTOR["default"])
    historical_yields = []
    for _ in years:
        fluctuation = random.uniform(-0.15, 0.15)
        yield_val = base_yield * state_factor * (1 + fluctuation)
        historical_yields.append(round(max(0.1, yield_val), 2))
    return {"years": years, "yields": historical_yields}

def predict_yield_and_advise(crop, season, state, fert_kg, pest_l, rain_mm):
    """Predicts crop yield and generates advice based on input parameters."""
    params = CROP_DATA.get(crop, CROP_DATA["default"])
    base, optimal_rain = params['base'], params['opt_rain']
    rain_deviation = (rain_mm - optimal_rain) / optimal_rain if optimal_rain > 0 else 0
    rain_factor = max(0, 1.0 - 0.5 * abs(rain_deviation))
    fert_factor = 1.0 + (fert_kg / 100.0) * params['fert_sens']
    pest_factor = 1.0 - (pest_l / 100.0) * params['pest_sens']
    season_adj = {"Kharif": 1.05, "Rabi": 1.0, "Zaid": 0.95, "Whole Year": 1.0}.get(season, 1.0)
    state_factor = STATE_PRODUCTIVITY_FACTOR.get(state, STATE_PRODUCTIVITY_FACTOR["default"])
    predicted_yield = base * rain_factor * fert_factor * pest_factor * season_adj * state_factor
    predicted_yield = round(max(predicted_yield, 0.01), 2)

    if rain_deviation < -0.25:
        rain_advice = f"The expected rain ({rain_mm}mm) is low for {crop}. Consider planning for irrigation to ensure a good harvest."
    elif rain_deviation > 0.25:
        rain_advice = f"The expected rain ({rain_mm}mm) is high for {crop}. Ensure your fields have good drainage to avoid waterlogging."
    else:
        rain_advice = f"The expected rainfall ({rain_mm}mm) looks good for {crop} this year!"

    rec_fert = params['rec_fert']
    if fert_kg < rec_fert * 0.8:
        fert_advice = f"You are using {fert_kg} kg/ha of fertilizer. For {crop}, increasing this towards the recommended {rec_fert} kg/ha could boost your yield."
    elif fert_kg > rec_fert * 1.2:
        fert_advice = f"You are using a high amount of fertilizer ({fert_kg} kg/ha). Make sure this is cost-effective for the expected yield."
    else:
        fert_advice = "The amount of fertilizer you are using seems well-balanced for this crop."

    pest_advice = "Pesticides protect your crops from damage. Use them wisely as needed to control pests and manage costs."

    advice = {"rain": rain_advice, "fertilizer": fert_advice, "pesticide": pest_advice}
    return predicted_yield, advice

def generate_ai_advisory(farm, crop_type, crop_stage, soil_type, weather_data):
    """Generates a structured, human-readable advisory as a dictionary."""
    advice_lists = {
        "alerts": [],
        "irrigation": [],
        "nutrients": [],
        "pests_diseases": [],
    }
    priority = "Low"
    weather_outlook = "Weather data is currently unavailable. Please check again later."

    crop_info = CROP_ADVISORY_DATA.get(crop_type, CROP_ADVISORY_DATA["default"])
    stage_key = crop_stage.title()
    if stage_key not in crop_info["stages"]:
        stage_key = next(iter(crop_info["stages"]))

    stage_advice = crop_info["stages"].get(stage_key)
    ideal_min, ideal_max = crop_info["ideal_temp"]
    advice_lists["nutrients"].append(f"For the {crop_stage.lower()} stage, {stage_advice}")

    if weather_data:
        temp = weather_data.get('temperature')
        code = weather_data.get('weathercode')
        weather_desc = WMO_WEATHER_CODES.get(code, "current weather conditions")
        weather_outlook = f"The forecast indicates {weather_desc} with a temperature of {temp}°C."

        if temp > ideal_max + 2:
            priority = "High"
            advice_lists["alerts"].append(f"Heat Alert: Temperature ({temp}°C) is above the ideal maximum ({ideal_max}°C). This can cause heat stress.")
            advice_lists["irrigation"].append("Consider irrigating during cooler parts of the day to reduce evaporation.")
        elif temp < ideal_min - 2:
            priority = "High"
            advice_lists["alerts"].append(f"Cold Alert: Temperature ({temp}°C) is below the ideal minimum ({ideal_min}°C). This could slow growth.")

        if code in [65, 82, 99]:
            priority = "High"
            advice_lists["alerts"].append(f"Severe Weather Alert: {weather_desc} is expected, which may cause crop damage and waterlogging.")
            advice_lists["irrigation"].append("Ensure field drainage is clear. Postpone irrigation.")
        elif code in [61, 63, 80, 81, 95, 96]:
            if priority != "High": priority = "Medium"
            advice_lists["irrigation"].append("Rain is expected, so monitor soil moisture before the next irrigation cycle.")
            advice_lists["pests_diseases"].append("Increased humidity after rain can favor fungal diseases. Scout for signs of blight, mildew, or rust.")

    if soil_type.lower() == "sandy":
        advice_lists["irrigation"].append("Your sandy soil drains quickly. If irrigating, prefer more frequent, shorter cycles to prevent water and nutrient runoff.")
    elif soil_type.lower() == "clay":
        advice_lists["irrigation"].append("Your clay soil retains water well. Check for waterlogging after rain or heavy irrigation.")

    actionable_advice = {}
    if advice_lists["irrigation"]:
        actionable_advice["Irrigation"] = ' '.join(advice_lists["irrigation"])
    if advice_lists["nutrients"]:
        actionable_advice["Crop Management"] = ' '.join(advice_lists["nutrients"])
    if advice_lists["pests_diseases"]:
        actionable_advice["Pest & Disease Watch"] = ' '.join(advice_lists["pests_diseases"])

    if advice_lists["alerts"]:
        alert_text = ' '.join(advice_lists["alerts"])
        if "Crop Management" in actionable_advice:
            actionable_advice["Crop Management"] = f"**Alerts:** {alert_text} " + actionable_advice["Crop Management"]
        else:
            actionable_advice["Alerts"] = alert_text

    return {
        "priority": priority.title(),
        "weather_outlook": weather_outlook,
        "actionable_advice": actionable_advice
    }


# --- 4. Database Models ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=True)
    password_hash = db.Column(db.String(128), nullable=False)
    language = db.Column(db.String(10), default='en')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    is_verified = db.Column(db.Boolean, nullable=False, default=False)
    farms = db.relationship('Farm', backref='owner', lazy=True, cascade='all, delete-orphan')

    @property
    def full_name(self): return f"{self.first_name} {self.last_name}"
    def set_password(self, password): self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    def check_password(self, password): return bcrypt.check_password_hash(self.password_hash, password)
    def update_last_login(self): self.last_login = datetime.utcnow(); db.session.commit()

class Farm(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(200), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    area_hectares = db.Column(db.Float, nullable=True)
    area_geojson = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    advisories = db.relationship('Advisory', backref='farm', lazy=True, cascade='all, delete-orphan')

class Advisory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(50), nullable=False, default='Medium')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    farm_id = db.Column(db.Integer, db.ForeignKey('farm.id'), nullable=False)
    is_read = db.Column(db.Boolean, default=False)

class TranslationCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_hash = db.Column(db.String(64), nullable=False, index=True)
    source_lang = db.Column(db.String(10), nullable=False)
    target_lang = db.Column(db.String(10), nullable=False)
    translated_text = db.Column(db.Text, nullable=False)

    __table_args__ = (db.UniqueConstraint('source_hash', 'target_lang', name='_source_hash_target_lang_uc'),)

    @staticmethod
    def add_translation(text, source_lang, target_lang, translated_text):
        source_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        entry = TranslationCache.query.filter_by(source_hash=source_hash, target_lang=target_lang).first()
        if not entry:
            entry = TranslationCache(
                source_hash=source_hash,
                source_lang=source_lang,
                target_lang=target_lang,
                translated_text=translated_text
            )
            db.session.add(entry)
            db.session.commit()
        return entry

    @staticmethod
    def get_translation(text, target_lang):
        source_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        entry = TranslationCache.query.filter_by(
            source_hash=source_hash,
            target_lang=target_lang
        ).first()
        return entry.translated_text if entry else None

# --- 5. User Loader and i18n ---

@login_manager.user_loader
def load_user(user_id): return User.query.get(int(user_id))

def get_locale():
    if current_user.is_authenticated and current_user.language:
        return current_user.language
    if 'language' in session and session['language'] in app.config['LANGUAGES']:
        return session['language']
    return request.accept_languages.best_match(list(app.config['LANGUAGES'].keys()))

def get_timezone():
    return 'Asia/Kolkata'

babel.init_app(app, locale_selector=get_locale, timezone_selector=get_timezone)

app.jinja_env.filters['format_datetime'] = format_datetime
app.jinja_env.filters['format_date'] = format_date
app.jinja_env.filters['format_time'] = format_time
app.jinja_env.filters['format_timedelta'] = format_timedelta

@app.before_request
def before_request():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(minutes=60) # Increased to 60 mins
    session.modified = True
    g.language = get_locale()

# --- 6. Web Forms ---

class FarmForm(FlaskForm):
    name = StringField(_('Farm Name'), validators=[DataRequired(), Length(max=100)])
    location = StringField(_('Location'), validators=[DataRequired(), Length(max=200)])
    latitude = FloatField(_('Latitude'))
    longitude = FloatField(_('Longitude'))
    submit = SubmitField(_('Save Farm'))

class LoginForm(FlaskForm):
    username = StringField(_l('Username'), validators=[DataRequired()])
    password = PasswordField(_l('Password'), validators=[DataRequired()])
    remember_me = BooleanField(_l('Remember Me'))
    submit = SubmitField(_l('Sign In'))

class RegistrationForm(FlaskForm):
    username = StringField(_l('Username'), validators=[DataRequired(), Length(min=4, max=64)])
    email = StringField(_l('Email'), validators=[DataRequired(), Email()])
    first_name = StringField(_l('First Name'), validators=[DataRequired(), Length(max=50)])
    last_name = StringField(_l('Last Name'), validators=[DataRequired(), Length(max=50)])
    password = PasswordField(_l('Password'), validators=[DataRequired(), Length(min=6)])
    password2 = PasswordField(_l('Repeat Password'), validators=[DataRequired(), EqualTo('password', message=_l('Passwords must match.'))])
    submit = SubmitField(_l('Register'))

    def validate_username(self, username):
        if User.query.filter_by(username=username.data).first():
            raise ValidationError(_('This username is already taken.'))

    def validate_email(self, email):
        if User.query.filter_by(email=email.data, is_verified=True).first():
            raise ValidationError(_('This email address is already registered and verified.'))

class OTPForm(FlaskForm):
    otp = StringField(_l('Email OTP'), validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField(_l('Verify Account'))

class PredictionForm(FlaskForm):
    crop = SelectField(_('Crop Name'), choices=[], validators=[DataRequired()])
    season = SelectField(_('Season'), choices=[], validators=[DataRequired()])
    state = SelectField(_('State'), choices=[], validators=[DataRequired()])
    area = FloatField(_('Area (in Hectares)'), validators=[DataRequired()])
    annual_rainfall = FloatField(_('Annual Rainfall (in mm)'), validators=[DataRequired()])
    fertilizer = FloatField(_('Fertilizer Usage (in kg/Hectare)'), validators=[DataRequired()])
    pesticide = FloatField(_('Pesticide Usage (in Litres/Hectare)'), validators=[DataRequired()])
    submit = SubmitField(_('Predict Yield'))


# --- 7. Application Routes ---

@app.route('/')
@app.route('/home')
def home():
    return render_template('home.html', title=_('Home'), google_maps_api_key=app.config.get('GOOGLE_MAPS_API_KEY'))

@app.route('/dashboard')
@login_required
def dashboard():
    all_user_farms = current_user.farms
    user_farms = [f for f in all_user_farms if f.latitude is not None and f.longitude is not None]
    farms_data = [{
        'id': f.id, 'name': f.name, 'location': f.location, 'latitude': f.latitude,
        'longitude': f.longitude, 'area_geojson': f.area_geojson,
        'area_hectares': f.area_hectares
    } for f in user_farms]
    recent_advisories = Advisory.query.filter(Advisory.farm_id.in_([f.id for f in user_farms])).order_by(Advisory.created_at.desc()).limit(5).all()

    crop_options_translated = [(crop, _(crop)) for crop in CROP_OPTIONS]
    season_options_translated = [(season, _(season)) for season in SEASON_OPTIONS]
    state_options_translated = [(state, _(state)) for state in STATE_OPTIONS]
    crop_stages_options_translated = [(stage, _(stage)) for stage in CROP_STAGE_OPTIONS]
    soil_types_options_translated = [(soil, _(soil)) for soil in SOIL_TYPE_OPTIONS]

    return render_template(
        'dashboard.html', title=_('Dashboard'), farms=user_farms,
        farms_data=farms_data, recent_advisories=recent_advisories,
        crop_options=crop_options_translated,
        season_options=season_options_translated,
        state_options=state_options_translated,
        crop_stages=crop_stages_options_translated,
        soil_types=soil_types_options_translated,
        google_maps_api_key=app.config.get('GOOGLE_MAPS_API_KEY')
    )

@app.route('/farms')
@login_required
def farms():
    crop_options_translated = [(crop, _(crop)) for crop in CROP_OPTIONS]
    soil_types_options_translated = [(soil, _(soil)) for soil in SOIL_TYPE_OPTIONS]
    crop_stages_options_translated = [(stage, _(stage)) for stage in CROP_STAGE_OPTIONS]

    return render_template(
        'farms.html', title=_('My Farms'),
        farms=current_user.farms,
        crop_options=crop_options_translated,
        soil_types=soil_types_options_translated,
        crop_stages=crop_stages_options_translated,
        google_maps_api_key=app.config.get('GOOGLE_MAPS_API_KEY')
    )

@app.route('/advisories')
@login_required
def advisories():
    farm_ids = [farm.id for farm in current_user.farms]
    query = Advisory.query.filter(Advisory.farm_id.in_(farm_ids))

    filter_farm_id = request.args.get('farm_id')
    if filter_farm_id and filter_farm_id.isdigit():
        query = query.filter(Advisory.farm_id == int(filter_farm_id))

    filter_status = request.args.get('status')
    if filter_status == 'read':
        query = query.filter_by(is_read=True)
    elif filter_status == 'unread':
        query = query.filter_by(is_read=False)

    filter_priority = request.args.get('priority', '').lower()
    if filter_priority in ['high', 'medium', 'low']:
        query = query.filter(Advisory.priority.ilike(filter_priority))

    all_advisories = query.order_by(Advisory.created_at.desc()).all()
    
    crop_options_translated = [(c, _(c)) for c in CROP_OPTIONS]
    crop_stages_translated = [(s, _(s)) for s in CROP_STAGE_OPTIONS]
    soil_types_translated = [(s, _(s)) for s in SOIL_TYPE_OPTIONS]

    return render_template(
        'advisories.html', title=_('Advisories'),
        advisories=all_advisories,
        farms=current_user.farms,
        crop_options=crop_options_translated,
        crop_stages=crop_stages_translated,
        soil_types=soil_types_translated
    )

# --- 8. Authentication Routes ---

def send_otp_email(recipient, otp):
    """Helper function to send a redesigned, formatted HTML OTP email."""
    try:
        current_year = datetime.now().year

        html_body = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;700&display=swap');
            </style>
        </head>
        <body style="margin: 0; padding: 0; background-color: #f4f4f7; font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
            <div style="display: none; font-size: 1px; color: #f4f4f7; line-height: 1px; max-height: 0px; max-width: 0px; opacity: 0; overflow: hidden;">
                Your AgriAssist verification code is here!
            </div>
            <table border="0" cellpadding="0" cellspacing="0" width="100%">
                <tr>
                    <td align="center" style="padding: 20px 0;">
                        <table border="0" cellpadding="0" cellspacing="0" width="600" style="background: #ffffff; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); margin: 0 auto;">
                            <tr>
                                <td align="center" style="padding: 40px 0 20px 0; border-bottom: 1px solid #eeeeee;">
                                    <h1 style="color: #1a431a; font-size: 32px; font-weight: 700; margin: 0;">AgriAssist</h1>
                                </td>
                            </tr>
                            <tr>
                                <td align="center" style="padding: 40px 30px;">
                                    <h2 style="color: #333333; font-size: 24px; font-weight: 600; margin-top: 0;">Confirm Your Identity</h2>
                                    <p style="font-size: 16px; color: #555555; line-height: 1.6;">
                                        Please use the following code to complete your registration. For your security, do not share this code with anyone.
                                    </p>
                                    <div style="background: #edf9f0; border: 1px dashed #a2d5ab; border-radius: 8px; margin: 30px auto; padding: 20px 30px;">
                                        <p style="font-size: 44px; font-weight: 700; color: #2e7d32; margin: 0; letter-spacing: 8px; line-height: 1;">
                                            {otp}
                                        </p>
                                    </div>
                                    <p style="font-size: 16px; color: #555555;">
                                        This code will expire in <strong>5 minutes</strong>.
                                    </p>
                                </td>
                            </tr>
                            <tr>
                                <td align="center" style="padding: 30px; background-color: #f9f9f9; border-bottom-left-radius: 12px; border-bottom-right-radius: 12px;">
                                    <p style="font-size: 12px; color: #999999; margin: 0;">
                                        If you didn't request this, you can safely ignore this email.
                                    </p>
                                    <p style="font-size: 12px; color: #999999; margin: 5px 0 0 0;">
                                        &copy; {current_year} AgriAssist. All rights reserved.
                                    </p>
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """

        msg = Message(
            subject=_("Your AgriAssist Verification Code"),
            sender=('No reply - AgriAssist', app.config['MAIL_DEFAULT_SENDER']),
            recipients=[recipient],
            body=_("Your one-time verification code is: {otp}. It will expire in 5 minutes.").format(otp=otp),
            html=html_body
        )
        mail.send(msg)
        return True
    except Exception as e:
        app.logger.error(f"Error sending email to {recipient}: {e}")
        if app.debug:
            flash(_('Mail Error: {error}').format(error=str(e)), 'info')
        return False

def send_welcome_email(recipient, first_name):
    """Sends a beautiful welcome email to a newly registered user."""
    try:
        current_year = datetime.now().year

        html_body = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;700&display=swap');
            </style>
        </head>
        <body style="margin: 0; padding: 0; background-color: #f4f4f7; font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
            <div style="display: none; font-size: 1px; color: #f4f4f7; line-height: 1px; max-height: 0px; max-width: 0px; opacity: 0; overflow: hidden;">
                Welcome to AgriAssist! Your account is ready.
            </div>
            <table border="0" cellpadding="0" cellspacing="0" width="100%">
                <tr>
                    <td align="center" style="padding: 20px 0;">
                        <table border="0" cellpadding="0" cellspacing="0" width="600" style="background: #ffffff; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); margin: 0 auto;">
                            <tr>
                                <td align="center" style="padding: 40px 0 20px 0; border-bottom: 1px solid #eeeeee;">
                                    <h1 style="color: #1a431a; font-size: 32px; font-weight: 700; margin: 0;">AgriAssist</h1>
                                </td>
                            </tr>
                            <tr>
                                <td align="center" style="padding: 40px 30px;">
                                    <h2 style="color: #333333; font-size: 24px; font-weight: 600; margin-top: 0;">Welcome Aboard, {first_name}!</h2>
                                    <p style="font-size: 16px; color: #555555; line-height: 1.6;">
                                        Thank you for joining AgriAssist. Your account has been successfully created. We're excited to help you manage your farm more effectively.
                                    </p>
                                    <p style="font-size: 16px; color: #555555; line-height: 1.6; margin-top: 20px;">
                                        You can now log in to add your farms, get personalized AI-driven advisories, and predict crop yields.
                                    </p>
                                    <a href="{url_for('login', _external=True)}" style="background-color: #2e7d32; color: #ffffff; display: inline-block; padding: 14px 28px; font-size: 16px; font-weight: 600; text-decoration: none; border-radius: 8px; margin-top: 30px;">
                                        Go to Your Dashboard
                                    </a>
                                </td>
                            </tr>
                            <tr>
                                <td align="center" style="padding: 30px; background-color: #f9f9f9; border-bottom-left-radius: 12px; border-bottom-right-radius: 12px;">
                                    <p style="font-size: 12px; color: #999999; margin: 5px 0 0 0;">
                                        &copy; {current_year} AgriAssist. All rights reserved.
                                    </p>
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """

        msg = Message(
            subject=_("Welcome to AgriAssist!"),
            sender=('AgriAssist Team', app.config['MAIL_DEFAULT_SENDER']),
            recipients=[recipient],
            html=html_body
        )
        mail.send(msg)
        return True
    except Exception as e:
        app.logger.error(f"Error sending welcome email to {recipient}: {e}")
        if app.debug:
            flash(_('Welcome Mail Error: {error}').format(error=str(e)), 'info')
        return False

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            if not user.is_verified:
                flash(_('Your account is not verified. Please check your email or register again.'), 'warning')
                return redirect(url_for('login'))
            
            login_user(user, remember=form.remember_me.data)
            user.update_last_login()
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        else:
            flash(_('Invalid username or password.'), 'danger')
    return render_template('login.html', title=_('Sign In'), form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('home'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            existing_unverified = User.query.filter_by(email=form.email.data, is_verified=False).first()
            if existing_unverified:
                db.session.delete(existing_unverified)
                db.session.commit()

            otp = str(random.randint(100000, 999999))
            if not send_otp_email(form.email.data, otp):
                flash(_('Could not send verification email. Please try again later.'), 'danger')
                return render_template('register.html', title=_('Register'), form=form)

            session['registration_form'] = {
                'username': form.username.data,
                'email': form.email.data,
                'first_name': form.first_name.data,
                'last_name': form.last_name.data,
                'password': bcrypt.generate_password_hash(form.password.data).decode('utf-8')
            }
            session['otp_data'] = {
                'otp_hash': bcrypt.generate_password_hash(otp).decode('utf-8'),
                'expires': (datetime.utcnow() + timedelta(minutes=5)).isoformat()
            }
            
            return redirect(url_for('verify_otp'))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Registration error: {e}")
            flash(_('An internal error occurred during registration. Please try again later.'), 'danger')
            if app.debug:
                flash(f"Debug Info: {str(e)}", 'info')
            return render_template('register.html', title=_('Register'), form=form)
        
    return render_template('register.html', title=_('Register'), form=form)

@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    if 'registration_form' not in session or 'otp_data' not in session:
        flash(_('Registration session expired. Please start over.'), 'info')
        return redirect(url_for('register'))

    form = OTPForm()
    if form.validate_on_submit():
        otp_data = session['otp_data']
        
        if datetime.utcnow() > datetime.fromisoformat(otp_data['expires']):
            session.pop('registration_form', None)
            session.pop('otp_data', None)
            flash(_('OTP has expired. Please register again.'), 'danger')
            return redirect(url_for('register'))
        
        if bcrypt.check_password_hash(otp_data['otp_hash'], form.otp.data):
            form_data = session['registration_form']
            user = User(
                username=form_data['username'],
                email=form_data['email'],
                first_name=form_data['first_name'],
                last_name=form_data['last_name'],
                password_hash=form_data['password'],
                is_verified=True
            )
            db.session.add(user)
            db.session.commit()
            
            send_welcome_email(user.email, user.first_name)
            
            session.pop('registration_form', None)
            session.pop('otp_data', None)
            
            flash(_('Account successfully created! Please log in.'), 'success')
            return redirect(url_for('login'))
        else:
            flash(_('Invalid OTP. Please try again.'), 'danger')
            
    return render_template('verify_otp.html', title=_('Verify Email'), form=form)

@app.route('/change-language/<language>')
def change_language(language):
    if language in app.config['LANGUAGES']:
        session['language'] = language
        if current_user.is_authenticated:
            current_user.language = language
            db.session.commit()
    return redirect(request.referrer or url_for('home'))

# --- 9. API Routes and Helpers ---

def get_weather_for_farm(farm):
    if not farm or not farm.latitude or not farm.longitude: return None
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={farm.latitude}&longitude={farm.longitude}"
               "&current=temperature_2m,is_day,weather_code")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json().get('current', {})
        if 'temperature_2m' in data and 'weather_code' in data:
            return { 'temperature': data.get('temperature_2m'), 'weathercode': data.get('weather_code'), 'is_day': data.get('is_day', 1) }
        else:
            print(f"Weather API response for farm {farm.id} missing essential keys.")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching weather data for farm {farm.id}: {e}")
        return None

@app.route('/api/geocode')
@login_required
def geocode():
    query = request.args.get('q')
    if not query: return jsonify({'error': _('Query parameter "q" is required.')}), 400
    url = f"https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=1"
    headers = {'User-Agent': 'AgriAssist/1.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data:
            result = data[0]
            return jsonify({'results': [{'display_name': result.get('display_name'), 'lat': float(result.get('lat')), 'lon': float(result.get('lon'))}]})
        else: return jsonify({'results': []})
    except requests.exceptions.RequestException as e:
        print(f"Geocoding error: {e}")
        return jsonify({'error': _('Failed to connect to geocoding service.')}), 500

@app.route('/api/reverse_geocode')
@login_required
def reverse_geocode():
    lat, lon = request.args.get('lat'), request.args.get('lon')
    if not lat or not lon: return jsonify({'error': _('Latitude and longitude are required.')}), 400
    url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}"
    headers = {'User-Agent': 'AgriAssist/1.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        address = data.get('address', {})
        state = address.get('state')
        if state: return jsonify({'state': state})
        else: return jsonify({'error': _('State not found for this location.')}), 404
    except requests.exceptions.RequestException as e:
        print(f"Reverse geocoding error: {e}")
        return jsonify({'error': _('Failed to connect to geocoding service.')}), 500

@app.route('/api/farms', methods=['POST'])
@login_required
def add_farm():
    data = request.get_json()
    if not all(k in data for k in ['name', 'location', 'latitude', 'longitude', 'area_hectares']):
        return jsonify({'error': _('Missing required farm data.')}), 400
    new_farm = Farm(name=data['name'], location=data['location'], latitude=data['latitude'], longitude=data['longitude'], area_hectares=data['area_hectares'], area_geojson=data.get('area_geojson'), user_id=current_user.id)
    db.session.add(new_farm); db.session.commit()
    flash(_('Farm added successfully!'), 'success')
    return jsonify({'success': True, 'farm_id': new_farm.id}), 201

@app.route('/api/farms/<int:farm_id>', methods=['DELETE'])
@login_required
def manage_farm(farm_id):
    farm = Farm.query.filter_by(id=farm_id, user_id=current_user.id).first_or_404()
    db.session.delete(farm); db.session.commit()
    flash(_('Farm deleted successfully.'), 'success')
    return jsonify({'success': True})

@app.route('/api/advisory', methods=['POST'])
@login_required
def get_advisory():
    data = request.get_json()
    if not data or not all(k in data for k in ['farm_id', 'crop_type', 'crop_stage', 'soil_type']):
        return jsonify({'error': _('Missing required advisory data.')}), 400

    farm = Farm.query.filter_by(id=data.get('farm_id'), user_id=current_user.id).first_or_404()
    weather = get_weather_for_farm(farm)

    advisory_data = generate_ai_advisory(
        farm=farm, crop_type=data['crop_type'],
        crop_stage=data['crop_stage'], soil_type=data['soil_type'], weather_data=weather
    )
    title = _("AgriAssist advisory for {crop_type} at {farm_name}").format(crop_type=_(data['crop_type']), farm_name=farm.name)

    content_parts = []
    if advisory_data.get("weather_outlook"):
        content_parts.append(f"{_('Weather Outlook')}:\n{advisory_data['weather_outlook']}")

    if advisory_data.get("actionable_advice"):
        actionable_advice_parts = []
        for key, value in advisory_data["actionable_advice"].items():
            actionable_advice_parts.append(f"{_(key)}:\n{value}")
        content_parts.append(_("ACTIONABLE ADVICE") + "\n" + "\n\n".join(actionable_advice_parts))

    content = "\n\n".join(content_parts) if content_parts else _("No specific advice at this time. Continue standard monitoring.")

    advisory = Advisory(
        title=title, content=content,
        priority=advisory_data['priority'], farm_id=farm.id
    )
    db.session.add(advisory)
    db.session.commit()

    translated_actionable_advice = {
        _(key): value for key, value in advisory_data.get('actionable_advice', {}).items()
    }

    return jsonify({
        'success': True,
        'advisory': {
            'id': advisory.id,
            'title': advisory.title,
            'content': advisory.content,
            'farm': {'name': farm.name},
            'priority': advisory.priority,
            'farm_name': farm.name,
            'priority_display': _(advisory_data['priority']),
            'weather_outlook': advisory_data['weather_outlook'],
            'actionable_advice': translated_actionable_advice
        }
    }), 201

@app.route('/api/advisories/bulk-delete', methods=['DELETE'])
@login_required
def bulk_delete_advisories():
    data = request.get_json()
    ids_to_delete = data.get('ids', [])
    if not ids_to_delete:
        return jsonify({'error': _('No advisory IDs provided.')}), 400

    farm_ids = [farm.id for farm in current_user.farms]
    advisories_to_delete = Advisory.query.filter(
        Advisory.farm_id.in_(farm_ids),
        Advisory.id.in_(ids_to_delete)
    ).all()

    if not advisories_to_delete:
        return jsonify({'error': _('No valid advisories found to delete.')}), 404

    for advisory in advisories_to_delete:
        db.session.delete(advisory)
    db.session.commit()

    flash(_('Selected advisories deleted successfully.'), 'success')
    return jsonify({'success': True, 'deleted_count': len(advisories_to_delete)})

@app.route('/api/advisories/delete-all', methods=['DELETE'])
@login_required
def delete_all_advisories():
    farm_ids = [farm.id for farm in current_user.farms]

    num_deleted = Advisory.query.filter(Advisory.farm_id.in_(farm_ids)).delete(synchronize_session=False)
    db.session.commit()

    if num_deleted > 0:
        flash(_('All advisories have been deleted successfully.'), 'success')

    return jsonify({'success': True, 'deleted_count': num_deleted})

@app.route('/api/advisories/<int:advisory_id>', methods=['DELETE'])
@login_required
def delete_advisory(advisory_id):
    advisory = Advisory.query.get_or_404(advisory_id)
    if advisory.farm.owner != current_user:
        return jsonify({'error': _('Forbidden')}), 403
    db.session.delete(advisory)
    db.session.commit()
    flash(_('Advisory deleted successfully.'), 'success')
    return jsonify({'success': True})

@app.route('/api/advisories/<int:advisory_id>/read', methods=['PATCH'])
@login_required
def toggle_advisory_read(advisory_id):
    advisory = Advisory.query.get_or_404(advisory_id)
    if advisory.farm.owner != current_user:
        return jsonify({'error': _('Forbidden')}), 403
    data = request.get_json()
    advisory.is_read = data.get('is_read', not advisory.is_read)
    db.session.commit()
    return jsonify({'success': True, 'is_read': advisory.is_read})

@app.route('/api/farms/<int:farm_id>/weather')
@login_required
def farm_weather(farm_id):
    farm = Farm.query.filter_by(id=farm_id, user_id=current_user.id).first_or_404()
    weather = get_weather_for_farm(farm)
    if weather: return jsonify(weather)
    else: return jsonify({'error': _('Could not retrieve weather data.')}), 500

# --- UPDATED: API Endpoints for Dashboard Cards ---

@app.route('/api/farms/<int:farm_id>/nutrient_needs')
@login_required
def get_nutrient_needs(farm_id):
    """Calculates an estimated fertilizer need based on default data, weather, and farm area."""
    farm = Farm.query.filter_by(id=farm_id, user_id=current_user.id).first_or_404()
    weather = get_weather_for_farm(farm)
    
    # Start with a base recommendation rate from our data (e.g., for a default crop)
    base_fert_rate = CROP_DATA.get("default", {}).get("rec_fert", 120)

    # Adjust rate based on simple weather rules
    if weather and 'weathercode' in weather:
        code = weather.get('weathercode')
        # Increase recommendation slightly for heavy rain due to potential leaching
        if code in [63, 65, 81, 82]:
            base_fert_rate *= 1.1 
    
    # Calculate total quantity if area is available, otherwise show the rate
    if farm.area_hectares and farm.area_hectares > 0:
        total_fertilizer = base_fert_rate * farm.area_hectares
        recommendation = f"{total_fertilizer:.1f} Kg"
    else:
        recommendation = f"{base_fert_rate:.1f} kg/ha"
        
    return jsonify({'recommendation': recommendation})

@app.route('/api/farms/<int:farm_id>/pesticide_needs')
@login_required
def get_pesticide_needs(farm_id):
    """Calculates an estimated pesticide need based on default data, weather, and farm area."""
    farm = Farm.query.filter_by(id=farm_id, user_id=current_user.id).first_or_404()
    weather = get_weather_for_farm(farm)
    
    # Start with a base recommendation rate
    base_pest_rate = 2.5 # Litres/Hectare

    # Adjust rate based on simple weather rules
    if weather and 'weathercode' in weather:
        code = weather.get('weathercode')
        # Increase recommendation for rainy/humid conditions that favor fungal growth
        if code in [61, 63, 65, 80, 81, 82, 95, 96, 99]:
            base_pest_rate *= 1.25

    # Calculate total quantity if area is available, otherwise show the rate
    if farm.area_hectares and farm.area_hectares > 0:
        total_pesticide = base_pest_rate * farm.area_hectares
        recommendation = f"{total_pesticide:.1f} Litres"
    else:
        recommendation = f"{base_pest_rate:.1f} L/ha"

    return jsonify({'recommendation': recommendation})


@app.route('/api/annual_rainfall')
@login_required
def get_annual_rainfall():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)

        if lat is None or lon is None:
            return jsonify({
                'error': _('Latitude and longitude are required.')
            }), 400

        current_year = datetime.now().year
        start_year = current_year - 4

        start_date = f"{start_year}-01-01"
        end_date = f"{current_year}-12-31"

        url = "https://archive-api.open-meteo.com/v1/archive"

        params = {
            'latitude': lat,
            'longitude': lon,
            'start_date': start_date,
            'end_date': end_date,
            'daily': 'precipitation_sum',
            'timezone': 'auto'
        }

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        if response.status_code != 200:
            return jsonify({
                'error': 'Open-Meteo API error',
                'status': response.status_code,
                'details': response.text[:1000]
            }), 502

        data = response.json()

        daily = data.get('daily')

        if not daily:
            return jsonify({
                'error': 'No daily rainfall data returned by Open-Meteo.'
            }), 404

        dates = daily.get('time', [])
        rainfall = daily.get('precipitation_sum', [])

        if not dates or not rainfall:
            return jsonify({
                'error': 'Rainfall data is empty.'
            }), 404

        annual_rainfall = {}

        for date_str, rainfall_value in zip(dates, rainfall):
            try:
                year = int(date_str[:4])
                value = float(rainfall_value or 0)

                annual_rainfall[year] = (
                    annual_rainfall.get(year, 0) + value
                )

            except (ValueError, TypeError):
                continue

        result = [
            {
                'year': year,
                'rainfall': round(value, 2)
            }
            for year, value in sorted(annual_rainfall.items())
        ]

        return jsonify({
            'success': True,
            'data': result
        })

    except requests.exceptions.Timeout:
        return jsonify({
            'error': 'Open-Meteo API request timed out.'
        }), 504

    except requests.exceptions.RequestException as e:
        return jsonify({
            'error': 'Failed to connect to Open-Meteo.',
            'details': str(e)
        }), 502

    except Exception as e:
        return jsonify({
            'error': 'Annual rainfall processing failed.',
            'details': str(e)
        }), 500
        
@app.route('/api/predict_yield', methods=['POST'])
@login_required
def api_predict_yield():
    if not CROP_DATA or not STATE_PRODUCTIVITY_FACTOR:
        return jsonify({'error': _('Prediction service is configured incorrectly.')}), 503

    form = PredictionForm(request.form)

    form.crop.choices = [(c, _(c)) for c in CROP_OPTIONS]
    form.season.choices = [(s, _(s)) for s in SEASON_OPTIONS]
    form.state.choices = [(s, _(s)) for s in STATE_OPTIONS]

    if form.validate_on_submit():
        try:
            crop = form.crop.data
            season = form.season.data
            state = form.state.data
            area = float(form.area.data)
            fert_kg = float(form.fertilizer.data)
            pest_l = float(form.pesticide.data)
            rain_mm = float(form.annual_rainfall.data)

            predicted_yield, advice = predict_yield_and_advise(
                crop, season, state, fert_kg, pest_l, rain_mm
            )
            total_yield = round(predicted_yield * area, 2)
            historical_data = get_historical_yields(crop, state)

            return jsonify({
                'success': True,
                'yield_per_ha_tons': predicted_yield,
                'total_tons': total_yield,
                'advice': advice,
                'historical': historical_data
            })
        except Exception as e:
            print(f"Prediction Error: {e}")
            return jsonify({'error': _('An error occurred during prediction calculation.')}), 500

    return jsonify({'error': _('Invalid input data.'), 'details': form.errors}), 400

@app.route('/session-ping', methods=['POST'])
@login_required
def session_ping():
    """An endpoint for the client to hit to keep the session alive."""
    session.modified = True
    return jsonify({'status': 'success'}), 200


# START ===== VOICE ASSISTANT BRAIN =====
@app.route('/api/voice-command', methods=['POST'])
@login_required
def process_voice_command():
    """Processes a transcribed voice command from the user."""
    data = request.get_json()
    transcript = data.get('transcript', '').lower().strip()
    response_text = _("Sorry, I didn't understand that. Please try again.")
    action = {'type': 'speak'} # Default action is to just speak

    if not transcript:
        return jsonify({'speak': response_text, 'action': action})

    # 1. Navigation Intent
    nav_keywords = [_('navigate to'), _('go to'), _('open'), _('show')]
    if any(keyword in transcript for keyword in nav_keywords):
        if _('dashboard') in transcript:
            response_text = _('Navigating to your dashboard.')
            action = {'type': 'navigate', 'url': url_for('dashboard')}
        elif _('farms') in transcript:
            response_text = _('Opening your farms page.')
            action = {'type': 'navigate', 'url': url_for('farms')}
        elif _('home') in transcript:
            response_text = _('Let\'s go to your home page.')
            action = {'type': 'navigate', 'url': url_for('home')}
        elif _('advisories') in transcript:
            response_text = _('Showing your latest advisories.')
            action = {'type': 'navigate', 'url': url_for('advisories')}
        else:
            response_text = _("I'm not sure where you want to go. You can say 'go to dashboard', for example.")

    # 2. Data Query Intent: Farm Count
    elif _('how many farms') in transcript:
        farm_count = len(current_user.farms)
        if farm_count == 0:
            response_text = _("You haven't added any farms yet.")
        elif farm_count == 1:
            response_text = _("You have one farm registered.")
        else:
            response_text = _("You have {count} farms registered.").format(count=farm_count)

    # 3. Data Query Intent: Weather
    elif _('weather') in transcript:
        if not current_user.farms:
            response_text = _("I can't get the weather because you don't have any farms registered.")
        else:
            found_farm = None
            for farm in current_user.farms:
                if farm.name.lower() in transcript:
                    found_farm = farm
                    break

            if found_farm:
                weather_data = get_weather_for_farm(found_farm)
                if weather_data and 'temperature' in weather_data:
                    temp = weather_data['temperature']
                    desc = WMO_WEATHER_CODES.get(weather_data['weathercode'], 'the current conditions')
                    response_text = _("The weather at {farm_name} is {temperature} degrees Celsius with {description}.").format(
                        farm_name=found_farm.name, temperature=temp, description=desc
                    )
                else:
                    response_text = _("Sorry, I couldn't retrieve the weather for {farm_name} at this time.").format(farm_name=found_farm.name)
            else:
                response_text = _("Which farm would you like the weather for? For example, say 'what is the weather at my Main Farm'.")

    # 4. General Knowledge Intent: Crop Info
    elif _('ideal temperature for') in transcript or _('temperature for') in transcript:
        found_crop = None
        for crop in CROP_ADVISORY_DATA:
            if crop.lower() in transcript:
                found_crop = crop
                break
        if found_crop:
            ideal_min, ideal_max = CROP_ADVISORY_DATA[found_crop]['ideal_temp']
            response_text = _("The ideal temperature for growing {crop} is between {min} and {max} degrees Celsius.").format(
                crop=found_crop, min=ideal_min, max=ideal_max
            )
        else:
            response_text = _("I don't have temperature data for that crop. Please be more specific.")

    # 5. Greeting Intent
    elif any(greeting in transcript for greeting in [_('hello'), _('hi'), _('hey')]):
        response_text = _("Hello, {user}! How can I assist you with your farm today?").format(user=current_user.first_name)

    return jsonify({'speak': response_text, 'action': action, 'transcript': transcript})
# END ===== VOICE ASSISTANT BRAIN =====

# --- 9. Global Error Handlers ---

@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', code=404, message=_("Page not found")), 404

@app.errorhandler(500)
def internal_server_error(e):
    db.session.rollback()
    return render_template('error.html', code=500, message=_("An internal server error occurred")), 500


# --- 10. Context Processors and Main Execution ---
@app.context_processor
def inject_globals():
    return {
        'now': datetime.utcnow,
        'get_locale': get_locale,
        'LANGUAGES': app.config['LANGUAGES']
    }

# --- VERCEL FIX: Create tables automatically when app loads ---
# This ensures that when Vercel starts your "Serverless Function",
# it checks if the database tables exist and creates them if they don't.
with app.app_context():
    try:
        db.create_all()

        # Create admin user if it doesn't exist
        if not User.query.filter_by(username='admin').first():
            print("Creating default admin user...")
            admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
            admin = User(username='admin', email='admin@example.com', first_name='Admin', last_name='User', is_verified=True)
            admin.set_password(admin_password)
            db.session.add(admin)
            db.session.commit()
    except Exception as e:
        app.logger.error(f"Error during startup database initialization: {e}")
        # On Vercel, we don't want to crash the whole app if DB is briefly unavailable
        # The app will try again on the next request context.

if __name__ == '__main__':
    app.run(debug=True)
